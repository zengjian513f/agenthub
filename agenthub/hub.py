"""Federated AgentHub: one web app, local execution on registered nodes.

Run behind the existing authenticated reverse proxy. Only configured private
networks may be registered; upstream connections never follow redirects.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import federation as fed, server

JSON_LIMIT = 64 * 1024 * 1024
BODY_LIMIT = 4 * 1024 * 1024


class Registry:
    def __init__(self, path: Path, networks):
        self.path = path
        self.networks = [ipaddress.ip_network(x) for x in networks]
        self.lock = threading.RLock()
        self.cache = {}
        self.health = {}
        self.nodes = json.loads(path.read_text()) if path.exists() else []
        for node in self.nodes:
            self.validate_url(node["url"])

    def validate_url(self, value):
        u = urlparse(value)
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username
                or u.password or u.query or u.fragment or u.path not in {"", "/"}):
            raise ValueError("节点地址必须是 http(s)://私网IP:端口，不含路径或凭据")
        # Literal IPs eliminate DNS rebinding and make the allowlist auditable.
        address = ipaddress.ip_address(u.hostname)
        if not any(address in network for network in self.networks):
            raise ValueError("节点地址不在 Hub 允许的网络内")
        if u.port is not None and not 1 <= u.port <= 65535:
            raise ValueError("invalid port")
        return u

    def all(self):
        with self.lock:
            return copy.deepcopy(self.nodes)

    def get(self, nid):
        return next((n for n in self.all() if n["id"] == nid), None)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(self.nodes, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        temp.replace(self.path)

    def register(self, body):
        name = str(body.get("name") or "").strip()
        token = str(body.get("token") or "").strip()
        url = str(body.get("url") or "").rstrip("/")
        if not name or len(name) > 80 or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,256}", token):
            raise ValueError("请输入机器名称、地址和至少 32 字符的节点凭据")
        self.validate_url(url)
        candidate = {"url": url, "token": token}
        status, meta = self.request(candidate, "/api/meta")
        if (status != 200 or meta.get("mode") != "local"
                or meta.get("protocol") != fed.PROTOCOL
                or not re.fullmatch(r"[a-f0-9]{32}", str(meta.get("node_id", "")))):
            raise ValueError("节点认证或协议检查失败，请先升级节点并配置凭据")
        node = {**candidate, "id": meta["node_id"], "name": name}
        with self.lock:
            if body.get("id") and body["id"] != node["id"]:
                raise ValueError("地址对应另一台机器，不能覆盖原节点身份")
            self.nodes = [n for n in self.nodes if n["id"] != node["id"]] + [node]
            self.cache = {k: v for k, v in self.cache.items() if k[0] != node["id"]}
            self.save()
        return {"id": node["id"], "name": name}

    def remove(self, nid):
        with self.lock:
            self.nodes = [n for n in self.nodes if n["id"] != nid]
            self.cache = {k: v for k, v in self.cache.items() if k[0] != nid}
            self.health.pop(nid, None)
            self.save()

    def connection(self, node, timeout=5):
        u = self.validate_url(node["url"])
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.hostname, u.port, timeout=timeout)

    @staticmethod
    def headers(node):
        return {"X-AgentHub-Node-Token": node["token"],
                "X-AgentHub-Protocol": str(fed.PROTOCOL), "Accept-Encoding": "identity"}

    def request(self, node, path, method="GET", body=None, timeout=5):
        conn = self.connection(node, timeout)
        headers = self.headers(node)
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            raw = response.read(JSON_LIMIT + 1)
            if len(raw) > JSON_LIMIT:
                raise ValueError("节点响应过大")
            return response.status, json.loads(raw)
        finally:
            conn.close()

    def query(self, node, path, query):
        key = (node["id"], path, urlencode(query, doseq=True))
        stamp = time.time()
        try:
            status, data = self.request(node, path + "?" + key[2],
                                        timeout=15 if path == "/api/search" else 5)
            if status != 200:
                raise ValueError(data.get("error") or f"HTTP {status}")
            data = fed.public_payload(data, node, path)
            with self.lock:
                if path != "/api/search":
                    self.cache[key] = (stamp, copy.deepcopy(data))
                    # Bound variant caches (debug views / forced refreshes).
                    while len(self.cache) > 128:
                        self.cache.pop(next(iter(self.cache)))
                self.health[node["id"]] = {"online": True, "last_seen": stamp}
            return node, data, None
        except (OSError, ValueError, http.client.HTTPException):
            # Errors intentionally omit URL / token / upstream exception text.
            with self.lock:
                prior = self.health.get(node["id"], {})
                self.health[node["id"]] = {**prior, "online": False}
                cached = self.cache.get(key) if path in {"/api/sessions", "/api/term/list"} else None
            data = copy.deepcopy(cached[1]) if cached else {}
            for row in data.get("sessions", []) + data.get("pending", []):
                row["stale"] = True
                row["last_seen"] = cached[0]
            if path == "/api/term/list":
                data["enabled"] = False
                data["sources"] = {}
            return node, data, {"node_id": node["id"], "name": node["name"],
                                "error": "连接失败或请求超时", "last_seen": prior.get("last_seen")}

    def public(self):
        with self.lock:
            return [{"id": n["id"], "name": n["name"],
                     **self.health.get(n["id"], {"online": None})} for n in self.nodes]


class HubHandler(server.Handler):
    @property
    def registry(self):
        return self.server.registry

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def log_message(self, fmt, *args):
        # Query strings may contain terminal lease tokens.
        if self.command != "GET" or self.headers.get("Upgrade", "").lower() == "websocket":
            print(f"[hub] {self.command} {urlparse(self.path).path}")

    def dispatch(self):
        if not self._allowed():
            self.close_connection = True
            return self._json({"error": "forbidden"}, 403)
        if self.command != "GET" or self.headers.get("Upgrade", "").lower() == "websocket":
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                self.close_connection = True
                return self._json({"error": "cross-origin write rejected"}, 403)
        try:
            u = urlparse(self.path)
            path, query = u.path, parse_qs(u.query, keep_blank_values=True)
            if self.command == "GET" and not path.startswith("/api/"):
                return self._static(path)
            if self.command == "GET" and path == "/api/meta":
                return self._json({"mode": "hub", "protocol": fed.PROTOCOL,
                                   "build": server.ASSET_VERSION, "hostname": "AgentHub"})
            if path == "/api/nodes":
                if self.command == "GET":
                    return self._json({"mode": "hub", "nodes": self.registry.public()})
                self.close_connection = True
                return self._json({"error": "machine management is not available over HTTP"}, 405)
            if re.fullmatch(r"/api/nodes/[a-f0-9]{32}", path):
                self.close_connection = True
                return self._json({"error": "machine management is not available over HTTP"},
                                  404 if self.command == "GET" else 405)
            explicit = None
            match = re.fullmatch(r"/api/nodes/([a-f0-9]{32})(/api/.*)", path)
            if match:
                explicit, path = match.groups()
            if (self.command == "GET" and not explicit and path in
                    {"/api/sessions", "/api/search", "/api/live", "/api/term/list", "/api/trash"}):
                return self.aggregate(path, query)
            attachment = path == "/api/session/attachment"
            body = self.read_body() if self.command == "POST" and not attachment else None
            if path == "/api/sessions/delete" and not explicit:
                return self.bulk_delete(body)
            if path == "/api/trash/purge" and body and body.get("all") and not explicit:
                return self.purge_all(body, query)
            if path == "/api/audit/browser" and not explicit:
                return self.browser_audit(body)
            nid, path, query, body = self.resolve(explicit, path, query, body)
            node = self.registry.get(nid)
            if not node:
                return self._json({"error": "机器未注册或已移除"}, 404)
            if self.command == "POST" and path in {
                    "/api/session/send", "/api/session/outbox/retry", "/api/term/send", "/api/term/create"}:
                if body.get("_build") != server.ASSET_VERSION:
                    return self._json({"error": "页面版本已过期，请重新加载", "reload": True,
                                       "build": server.ASSET_VERSION}, 409)
            return self.proxy(node, path, query, body, attachment)
        except (ValueError, TypeError, KeyError) as error:
            self.close_connection = True
            return self._json({"error": str(error)}, 400)
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            return self._json({"error": "机器连接中断；写操作可能已执行，请核对目标机器状态"}, 502)

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked requests are not supported")
        size = int(self.headers.get("Content-Length", 0))
        if not 0 <= size <= BODY_LIMIT:
            raise ValueError("request too large")
        body = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected JSON object")
        return body

    def selected(self, query):
        ids = query.get("nodes", [None])[0]
        nodes = self.registry.all()
        if ids is None:
            return nodes
        wanted = set(ids.split(",")) - {""}
        if wanted - {n["id"] for n in nodes}:
            raise ValueError("筛选包含未注册的机器")
        return [n for n in nodes if n["id"] in wanted]

    def aggregate(self, path, query):
        nodes = self.selected(query)
        upstream = {k: v for k, v in query.items() if k not in {"nodes", "sig", "progress"}}
        # Never wait sequentially for a slow node. Each node has a bounded timeout.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(nodes)) or 1) as pool:
            results = list(pool.map(lambda n: self.registry.query(n, path, upstream), nodes))
        errors = [err for _, _, err in results if err]
        result = {"errors": errors, "partial": bool(errors), "nodes": self.registry.public()}
        if path in {"/api/sessions", "/api/search"}:
            key = "sessions" if path == "/api/sessions" else "results"
            rows = [row for _, data, _ in results for row in data.get(key, [])]
            rows.sort(key=lambda row: (row.get("updated", ""), row["uid"]), reverse=True)
            result[key] = rows
            result["truncated"] = any(d.get("truncated") for _, d, _ in results)
            result["truncated_nodes"] = [n["id"] for n, d, _ in results if d.get("truncated")]
            result["total_pool"] = sum(d.get("total_pool", 0) for _, d, _ in results)
            if path == "/api/sessions":
                stable = {**result, "nodes": [{k: n.get(k) for k in ("id", "name", "online")}
                                              for n in result["nodes"]]}
                sig = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24]
                if query.get("sig", [""])[0] == sig:
                    return self._json({"unchanged": True, "sig": sig,
                                       "nodes": result["nodes"], "errors": errors})
                result.update(sig=sig, built_at=time.time())
        elif path == "/api/live":
            for key in ("uids", "tmux_uids"):
                result[key] = [v for _, d, err in results if not err for v in d.get(key, [])]
            result["started_at"] = {k: v for _, d, err in results if not err
                                     for k, v in d.get("started_at", {}).items()}
        elif path == "/api/term/list":
            result.update(enabled=any(d.get("enabled") for _, d, _ in results), home="", sources={})
            for key in ("sessions", "pending"):
                result[key] = [v for _, d, _ in results for v in d.get(key, [])]
            result["capabilities"] = {}
            for n, d, err in results:
                result["capabilities"][n["id"]] = {
                    "enabled": bool(d.get("enabled")) and not err,
                    "sources": d.get("sources", {}), "home": d.get("home", "")}
                if not err:
                    for source, available in d.get("sources", {}).items():
                        result["sources"][source] = result["sources"].get(source, False) or available
        elif path == "/api/trash":
            result["items"] = [v for _, d, _ in results for v in d.get("items", [])]
            result["size"] = sum(d.get("size", 0) for _, d, _ in results)
            result["dir"] = "所选机器的本地回收站"
        return self._json(result)

    def resolve(self, explicit, path, query, body):
        candidates = {explicit} if explicit else set()
        body = dict(body) if body is not None else None
        query = dict(query)
        def decode(value, uid=False):
            nid, local = fed.split(str(value), uid)
            candidates.add(nid)
            return local
        # Explicit routes carry local references; aggregate routes carry scoped ones.
        if not explicit:
            for prefix in ("/api/messages/", "/api/session/"):
                if path.startswith(prefix) and (prefix == "/api/messages/" or self.command == "DELETE"):
                    path = prefix + quote(decode(unquote(path[len(prefix):]), True), safe=":")
            for key in ("uid", "name"):
                if query.get(key) and (key == "uid" or path.startswith("/api/term/")):
                    query[key] = [decode(query[key][0], key == "uid")]
            if body:
                for key in ("uid", "name"):
                    if body.get(key) and (key == "uid" or path.startswith("/api/term/")
                                          or path in {"/api/session/draft-status", "/api/session/rewind", "/api/session/send"}):
                        body[key] = decode(body[key], key == "uid")
                if path == "/api/bug-report" and body.get("terminal_name"):
                    body["terminal_name"] = decode(body["terminal_name"])
                if path.startswith("/api/trash/") and body.get("id"):
                    body["id"] = decode(body["id"])
        nid = (body or {}).pop("_node", None) or query.pop("node", [None])[0]
        if nid:
            candidates.add(nid)
        if len(candidates) != 1:
            raise ValueError("操作必须明确指定同一台机器")
        nid = next(iter(candidates))
        if body and isinstance(body.get("media"), list):
            clean = []
            for item in body["media"]:
                item = dict(item)
                src = item.get("src", "")
                match = re.fullmatch(r"/api/nodes/([a-f0-9]{32})(/api/media/[a-f0-9]{32})", src)
                if match:
                    if match[1] != nid:
                        raise ValueError("附件来自另一台机器")
                    item["src"] = match[2]
                clean.append(item)
            body["media"] = clean
        query.pop("nodes", None)
        return nid, path, query, body

    def bulk_delete(self, body):
        groups = {}
        for uid in body.get("uids", []):
            nid, local = fed.split(uid, True)
            groups.setdefault(nid, []).append(local)
        if not groups:
            raise ValueError("没有选中任何会话")
        result = {"ok": True, "deleted": [], "errors": []}
        for nid, uids in groups.items():
            node = self.registry.get(nid)
            try:
                if not node:
                    raise ValueError("机器未注册")
                status, data = self.registry.request(node, "/api/sessions/delete", "POST", {"uids": uids})
                if status != 200:
                    raise ValueError("删除失败")
                data = fed.public_payload(data, node, "/api/sessions/delete")
                result["deleted"].extend(data.get("deleted", []))
                result["errors"].extend(data.get("errors", []))
            except (OSError, ValueError, http.client.HTTPException):
                result["errors"].extend({"uid": fed.qualify(nid, uid, True),
                                         "error": "机器请求失败，请核对结果"} for uid in uids)
        return self._json(result)

    def purge_all(self, body, query):
        result = {"ok": True, "removed": 0, "freed": 0, "errors": []}
        for node in self.selected(query):
            try:
                status, data = self.registry.request(node, "/api/trash/purge", "POST", {"all": True})
                if status != 200:
                    raise ValueError("purge failed")
                for key in ("removed", "freed"):
                    result[key] += data.get(key, 0)
                result["errors"].extend(f'{node["name"]}: {e}' for e in data.get("errors", []))
            except (OSError, ValueError, http.client.HTTPException):
                result["errors"].append(f'{node["name"]}: 请求失败，请核对结果')
        return self._json(result)

    def browser_audit(self, body):
        groups = {}
        for event in body.get("events", []):
            try:
                nid, uid = fed.split(event.get("uid") or body.get("uid") or "", True)
            except ValueError:
                continue
            groups.setdefault(nid, []).append({**event, "uid": uid})
        for nid, events in groups.items():
            node = self.registry.get(nid)
            if node:
                try:
                    self.registry.request(node, "/api/audit/browser", "POST",
                                          {**body, "uid": events[0]["uid"], "events": events}, timeout=2)
                except (OSError, ValueError, http.client.HTTPException):
                    pass
        return self._json({"ok": True})

    def proxy(self, node, path, query, body, attachment=False):
        headers = self.registry.headers(node)
        for key in ("Content-Type", "X-AgentHub-Page", "X-AgentHub-Trace", "X-AgentHub-Build"):
            if self.headers.get(key):
                headers[key] = self.headers[key]
        headers["X-Real-IP"] = self._display_ip()
        websocket = path == "/api/term/attach"
        if websocket:
            for key in ("Upgrade", "Connection", "Sec-WebSocket-Key", "Sec-WebSocket-Version"):
                if self.headers.get(key):
                    headers[key] = self.headers[key]
        # Native SSE heartbeats are 20s apart. HTTPConnection may release its
        # socket reference for an unbounded response, so set this before reading.
        conn = self.registry.connection(node, timeout=45 if path == "/api/watch" else 10)
        target = path + ("?" + urlencode(query, doseq=True) if query else "")
        started = False
        response = None
        try:
            if attachment:
                size = int(self.headers.get("Content-Length", -1))
                if self.headers.get("Transfer-Encoding") or not 0 <= size <= server.ATTACHMENT_MAX_BYTES:
                    raise ValueError("invalid attachment size")
                headers["Content-Length"] = str(size)
                conn.putrequest(self.command, target)
                for key, value in headers.items():
                    conn.putheader(key, value)
                conn.endheaders()
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    conn.send(chunk)
                    remaining -= len(chunk)
            else:
                payload = json.dumps(body).encode() if body is not None else None
                conn.request(self.command, target, payload, headers)
            response = conn.getresponse()
            ctype = response.getheader("Content-Type", "application/octet-stream")
            if response.status == 101 and websocket:
                self.send_response(101)
                for key in ("Upgrade", "Connection", "Sec-WebSocket-Accept", "Sec-WebSocket-Protocol"):
                    if response.getheader(key):
                        self.send_header(key, response.getheader(key))
                self.end_headers()
                self.wfile.flush()
                started = True
                self.close_connection = True
                # HTTPResponse uses a buffered reader; after the 101 no frame has
                # been consumed. read1 drains any prefetched bytes before select.
                upstream = conn.sock
                upstream.settimeout(None)
                self.connection.settimeout(None)
                stop = threading.Event()
                def incoming():
                    try:
                        while not stop.is_set():
                            chunk = response.fp.read1(65536)
                            if not chunk:
                                break
                            self.connection.sendall(chunk)
                    except OSError:
                        pass
                    finally:
                        stop.set()
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                worker = threading.Thread(target=incoming, daemon=True)
                worker.start()
                try:
                    while not stop.is_set():
                        chunk = self.rfile.read1(65536) if hasattr(self.rfile, "read1") else self.connection.recv(65536)
                        if not chunk:
                            break
                        upstream.sendall(chunk)
                finally:
                    stop.set()
                    upstream.shutdown(socket.SHUT_RDWR)
                    worker.join(2)
                return
            if "application/json" in ctype:
                raw = response.read(JSON_LIMIT + 1)
                if len(raw) > JSON_LIMIT:
                    raise ValueError("节点响应过大")
                return self._json(fed.public_payload(json.loads(raw), node, path), response.status)
            if "text/event-stream" in ctype:
                # Preserve stream ordering and native cursors; only rewrite wire references.
                self.send_response(response.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                started = True
                if conn.sock:
                    conn.sock.settimeout(45)
                for line in response.fp:
                    if line.startswith(b"data: "):
                        data = fed.public_payload(json.loads(line[6:]), node, path)
                        line = b"data: " + json.dumps(data, ensure_ascii=False).encode() + b"\n"
                    self.wfile.write(line)
                    self.wfile.flush()
                return
            self.send_response(response.status)
            self.send_header("Content-Type", ctype)
            for key in ("Content-Length", "Content-Disposition", "X-Content-Type-Options", "Cache-Control",
                        "Content-Security-Policy"):
                if response.getheader(key):
                    self.send_header(key, response.getheader(key))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            started = True
            while chunk := response.read(65536):
                self.wfile.write(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            if not started:
                raise
        finally:
            if response is not None:
                response.close()
            conn.close()


def main():
    ap = argparse.ArgumentParser(description="AgentHub 多机器聚合服务（放在已鉴权的反代后）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8720)
    ap.add_argument("--allow", default="", help="允许连接 Hub 的反代 IP/CIDR")
    ap.add_argument("--nodes-file", type=Path, default=Path.home() / ".local/share/agenthub/hub-nodes.json")
    ap.add_argument("--node-networks", default="127.0.0.0/8,::1/128,10.0.0.0/24",
                    help="可注册的节点 IP/CIDR，逗号分隔")
    args = ap.parse_args()
    server.ALLOWED_IPS.update({"127.0.0.1", "::1"})
    for value in args.allow.split(","):
        server._add_allowed(value)
    registry = Registry(args.nodes_file, args.node_networks.split(","))
    server.HOSTNAME = "AgentHub"
    server.HUB_MODE = True
    srv = ThreadingHTTPServer((args.host, args.port), HubHandler)
    srv.daemon_threads = True
    srv.registry = registry
    print(f"[hub] http://{args.host}:{args.port} ({len(registry.all())} nodes)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
