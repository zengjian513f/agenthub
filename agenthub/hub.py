"""Federated AgentHub: one web app, local execution on registered nodes.

Run behind the existing authenticated reverse proxy. Only configured private
networks may be registered; upstream connections never follow redirects.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import errno
import hashlib
import http.client
import ipaddress
import json
import os
import queue
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
SEARCH_IDLE_TIMEOUT = 60
# The hub owns node state. A monitor thread checks every registered node each
# PROBE_INTERVAL seconds (and immediately when nudged by a user action); page
# requests only consult that state and never wait on a node known to be down.
# Machines being switched off is normal, so the last session list of each node
# is persisted and shown as an offline cache until the node is back.
# 机器配色在注册表里按机器配置：加机器只改配置，不用改代码。没配的机器没有颜色。
NODE_PALETTE = ("blue", "violet", "amber", "teal", "rose", "lime", "cyan", "fuchsia")
PROBE_INTERVAL = 10
RECHECK_TIMEOUT = 5
# A node that is currently online is only painted offline after this many
# consecutive failed checks. One slow answer from a busy machine, or a service
# restart during deployment, must not gray out its console; a machine that is
# really gone still shows up within PROBE_INTERVAL * OFFLINE_STRIKES seconds.
OFFLINE_STRIKES = 2
CACHED_PATHS = {"/api/sessions", "/api/term/list"}


def request_failure(error, status=None, timeout=5):
    """Public diagnostics must not contain upstream bodies, URLs or credentials."""
    if status is not None and status != 200:
        detail = {401: "节点认证失败", 403: "节点拒绝访问，请检查认证或访问权限",
                  404: "节点接口不存在", 429: "节点请求过于频繁",
                  503: "节点服务暂不可用"}.get(status, "节点返回错误响应")
        return "http_error", f"{detail}（HTTP {status}）"
    if isinstance(error, TimeoutError):
        return "timeout", f"节点连接或响应超时（等待超过 {timeout} 秒）"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused", "节点拒绝连接，目标端口未接受请求"
    if isinstance(error, OSError) and error.errno in {errno.EHOSTUNREACH, errno.ENETUNREACH}:
        return "unreachable", "节点网络不可达"
    if isinstance(error, ConnectionError):
        return "connection_closed", "节点连接中断，未收到完整响应"
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError, http.client.HTTPException)):
        return "invalid_response", "节点返回无效或不完整的响应"
    return "connection_failed", "无法建立节点连接"


def enabled(node):
    """注册表里没写 enabled 的机器都算启用；只有设置页明确停用过才是 False。"""
    return node.get("enabled", True) is not False


class Registry:
    def __init__(self, path: Path, networks, monitor=True):
        self.path = path
        self.snapshot_dir = path.parent / "hub-cache"
        self.networks = [ipaddress.ip_network(x) for x in networks]
        self.lock = threading.RLock()
        self.cache = {}
        self.health = {}
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.snapshot_sigs = {}
        self.nodes = json.loads(path.read_text()) if path.exists() else []
        for node in self.nodes:
            self.validate_url(node["url"])
            if enabled(node):
                self.load_snapshot(node)
        if monitor:
            self.start_monitor()

    # ---- node state -------------------------------------------------------

    def snapshot_path(self, nid):
        return self.snapshot_dir / f"{nid}.sessions.json"

    def load_snapshot(self, node):
        """Seed the offline cache from disk so a switched-off machine still lists
        its sessions after a hub restart. State stays unknown until the monitor
        has checked the node."""
        try:
            raw = json.loads(self.snapshot_path(node["id"]).read_text())
            stamp, data = float(raw["stamp"]), raw["data"]
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
                raise ValueError("bad snapshot")
        except (OSError, ValueError, TypeError, KeyError):
            return
        with self.lock:
            self.cache[(node["id"], "/api/sessions", "")] = (stamp, data)
            self.health.setdefault(node["id"], {"online": None, "last_seen": stamp})

    def save_snapshot(self, node, stamp, data):
        try:
            sig = data.get("sig")
            if sig and self.snapshot_sigs.get(node["id"]) == sig and self.snapshot_path(node["id"]).exists():
                return
            self.snapshot_sigs[node["id"]] = sig
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            temp = self.snapshot_path(node["id"]).with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({"stamp": stamp, "data": data}, out, ensure_ascii=False)
            temp.replace(self.snapshot_path(node["id"]))
        except OSError:
            pass

    def drop_snapshot(self, nid):
        try:
            self.snapshot_path(nid).unlink()
        except OSError:
            pass

    def state(self, nid):
        with self.lock:
            return dict(self.health.get(nid, {}))

    def offline(self, nid):
        return self.state(nid).get("online") is False

    def sessions_sig(self, nid):
        with self.lock:
            cached = self.cache.get((nid, "/api/sessions", ""))
        return cached[1].get("sig") if cached else None

    def check(self, node):
        """Conditional session fetch: the node answers a tiny `unchanged` when its
        list signature still matches, so heartbeats cost bytes, not megabytes."""
        sig = self.sessions_sig(node["id"])
        return self.query(node, "/api/sessions", {"sig": [sig]} if sig else {})

    def check_all(self):
        """One monitor pass: refresh state and the session snapshot of every node."""
        nodes = self.all()
        if not nodes:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(nodes))) as pool:
            list(pool.map(self.check, nodes))

    def monitor(self):
        while not self.stopped.is_set():
            try:
                self.check_all()
            except Exception as error:  # keep the monitor alive no matter what
                print(f"[hub] monitor: {type(error).__name__}")
            self.wake.wait(PROBE_INTERVAL)
            self.wake.clear()

    def start_monitor(self):
        self.stopped.clear()
        self.wake.clear()
        threading.Thread(target=self.monitor, name="hub-monitor", daemon=True).start()

    def stop_monitor(self):
        self.stopped.set()
        self.wake.set()

    def nudge(self):
        """Ask the monitor to re-check now (a user just acted on an offline node)."""
        self.wake.set()

    def recheck(self, node):
        """Quick inline re-check before refusing an explicit action on an offline
        node, so a machine that just came back is usable at once."""
        self.query(node, "/api/live", {}, timeout=RECHECK_TIMEOUT)
        self.nudge()
        return not self.offline(node["id"])

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
        """只含启用的机器：聚合、监控、代理和 uid 解析都从这里取，停用的机器对它们
        不存在（双系统的两台机器有一台开着另一台必然关着，不必反复去探）。"""
        with self.lock:
            return copy.deepcopy([n for n in self.nodes if enabled(n)])

    def get(self, nid):
        return next((n for n in self.all() if n["id"] == nid), None)

    def find(self, nid):
        """包括停用的机器；只有设置页改属性（含重新启用）用它。"""
        with self.lock:
            return next((copy.deepcopy(n) for n in self.nodes if n["id"] == nid), None)

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
        color = str(body.get("color") or "").strip().lower()
        if color:
            if color not in NODE_PALETTE:
                raise ValueError(f"机器颜色只能取 {'、'.join(NODE_PALETTE)}")
            node["color"] = color
        with self.lock:
            if body.get("id") and body["id"] != node["id"]:
                raise ValueError("地址对应另一台机器，不能覆盖原节点身份")
            self.nodes = [n for n in self.nodes if n["id"] != node["id"]] + [node]
            self.cache = {k: v for k, v in self.cache.items() if k[0] != node["id"]}
            self.health.pop(node["id"], None)
            self.drop_snapshot(node["id"])
            self.save()
        self.nudge()
        return {"id": node["id"], "name": name, "color": node.get("color", "")}

    def update_display(self, nid, name=None, color=None, enabled_flag=None):
        """改机器的名称、配色和启用状态。地址和凭据不在这里，它们只在服务器端注册时设定。"""
        wake = False
        with self.lock:
            node = next((n for n in self.nodes if n["id"] == nid), None)
            if not node:
                raise KeyError(nid)
            if name is not None:
                clean = str(name).strip()
                if not clean or len(clean) > 80:
                    raise ValueError("机器名称不能为空，且不超过 80 个字符")
                if any(n["id"] != nid and n["name"] == clean for n in self.nodes):
                    raise ValueError(f"已有机器叫 {clean}")
                node["name"] = clean
            if color is not None:
                clean = str(color).strip().lower()
                if clean and clean not in NODE_PALETTE:
                    raise ValueError(f"机器颜色只能取 {'、'.join(NODE_PALETTE)}")
                if clean:
                    node["color"] = clean
                else:
                    node.pop("color", None)
            if enabled_flag is not None and bool(enabled_flag) != enabled(node):
                if enabled_flag:
                    node.pop("enabled", None)
                    self.load_snapshot(node)   # 磁盘快照没删，重新启用后先把上次的列表拿回来
                    wake = True
                else:
                    # 停用即视同不存在：内存里的健康状态和缓存一并清掉，监控不再探它
                    node["enabled"] = False
                    self.cache = {k: v for k, v in self.cache.items() if k[0] != nid}
                    self.health.pop(nid, None)
            self.save()
            row = {"id": node["id"], "name": node["name"], "color": node.get("color", ""),
                   "enabled": enabled(node)}
        if wake:
            self.nudge()
        return row

    def remove(self, nid):
        with self.lock:
            self.nodes = [n for n in self.nodes if n["id"] != nid]
            self.cache = {k: v for k, v in self.cache.items() if k[0] != nid}
            self.health.pop(nid, None)
            self.drop_snapshot(nid)
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

    def search_request(self, node, query, progress=None, matches=None):
        """Keep reading while the node makes progress, including cold scans."""
        conn = self.connection(node, timeout=5)
        try:
            conn.request("GET", "/api/search?" + urlencode(
                {**query, "progress": ["1"]}, doseq=True), headers=self.headers(node))
            conn.sock.settimeout(SEARCH_IDLE_TIMEOUT)
            response = conn.getresponse()
            if "application/x-ndjson" not in response.getheader("Content-Type", ""):
                raw = response.read(JSON_LIMIT + 1)
                if len(raw) > JSON_LIMIT:
                    raise ValueError("节点响应过大")
                return response.status, json.loads(raw)
            remaining = JSON_LIMIT
            while True:
                line = response.readline(remaining + 1)
                remaining -= len(line)
                if remaining < 0:
                    raise ValueError("节点响应过大")
                if not line:
                    raise ValueError("搜索响应不完整")
                event = json.loads(line)
                if event.get("type") == "progress" and progress:
                    progress(int(event["done"]), int(event["total"]))
                elif event.get("type") == "matches" and matches:
                    matches(fed.public_payload({"results": event["results"]},
                                               node, "/api/search")["results"])
                elif event.get("type") == "result":
                    return response.status, event["data"]
                elif event.get("type") == "error":
                    raise ValueError("节点搜索失败")
        finally:
            conn.close()

    def query(self, node, path, query, progress=None, timeout=5, matches=None):
        key = (node["id"], path, urlencode(query, doseq=True))
        stamp = time.time()
        status = None
        found = {}

        def received(rows):
            found.update((row["uid"], row) for row in rows)
            if matches:
                matches(rows)

        try:
            status, data = (self.search_request(node, query, progress, received) if path == "/api/search"
                            else self.request(node, path + "?" + key[2], timeout=timeout))
            if status != 200:
                raise ValueError(data.get("error") or f"HTTP {status}")
            unchanged = path == "/api/sessions" and bool(data.get("unchanged"))
            if not unchanged:
                data = fed.public_payload(data, node, path)
            with self.lock:
                if path != "/api/search" and not unchanged:
                    self.cache[key] = (stamp, copy.deepcopy(data))
                    # Bound variant caches (debug views / forced refreshes).
                    while len(self.cache) > 128:
                        self.cache.pop(next(iter(self.cache)))
                if path == "/api/sessions" and not unchanged and set(query) <= {"force", "sig"}:
                    self.save_snapshot(node, stamp, data)
                    self.cache[(node["id"], path, "")] = (stamp, copy.deepcopy(data))
                self.health[node["id"]] = {"online": True, "last_seen": stamp, "checked_at": stamp}
            return node, data, None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, http.client.HTTPException) as error:
            # Errors intentionally omit URL / token / upstream exception text.
            code, reason = request_failure(error, status, SEARCH_IDLE_TIMEOUT if path == "/api/search" else timeout)
            with self.lock:
                prior = self.health.get(node["id"], {})
                # A failed search says nothing about the node's live/terminal APIs.
                if path != "/api/search":
                    now = time.time()
                    strikes = int(prior.get("strikes") or 0) + 1
                    failed_since = float(prior.get("failed_since") or now)
                    # Hold a previously reachable node visible for one more
                    # probe; its own requests still report their real error.
                    tentative = prior.get("online") is True and strikes < OFFLINE_STRIKES
                    state = {
                        **prior, "online": tentative, "error": reason, "error_code": code,
                        "failed_path": path, "checked_at": now,
                        "strikes": strikes, "failed_since": failed_since}
                    if tentative:
                        state.pop("offline_since", None)
                    else:
                        # Report the outage from its first failure, not from the
                        # probe that finally gave up on the node.
                        state["offline_since"] = prior.get("offline_since") or failed_since
                    self.health[node["id"]] = state
                cached = self.cache.get(key) if path in CACHED_PATHS else None
                if cached is None and path == "/api/sessions":
                    cached = self.cache.get((node["id"], path, ""))
            data = self.stale_payload(path, cached)
            if path == "/api/search" and found:
                data["results"] = list(found.values())
            return node, data, {
                "node_id": node["id"], "name": node["name"],
                "error": reason, "error_code": code, "last_seen": prior.get("last_seen")}

    @staticmethod
    def stale_payload(path, cached):
        data = copy.deepcopy(cached[1]) if cached else {}
        for row in data.get("sessions", []) + data.get("pending", []):
            row["stale"] = True
            row["last_seen"] = cached[0]
        if path == "/api/term/list":
            data["enabled"] = False
            data["sources"] = {}
        return data

    def fetch(self, node, path, query, progress=None, matches=None):
        """Aggregate-side query: a node the monitor knows to be offline is never
        waited on. Its last failure and offline cache are returned instead."""
        nid = node["id"]
        with self.lock:
            health = dict(self.health.get(nid, {}))
            key = (nid, path, urlencode(query, doseq=True))
            cached = self.cache.get(key) if path in CACHED_PATHS else None
            if cached is None and path == "/api/sessions":
                cached = self.cache.get((nid, path, ""))
        # Never hold registry state while waiting for network I/O. A cold search
        # otherwise serializes every node, heartbeat, list and creation response.
        if health.get("online") is not False:
            if path == "/api/sessions" and not query and cached and cached[1].get("sig"):
                node, data, failure = self.check(node)
                if failure or not data.get("unchanged"):
                    return node, data, failure
                with self.lock:
                    latest = self.cache.get((nid, path, ""), cached)
                    return node, copy.deepcopy(latest[1]), None
            return self.query(node, path, query, progress, matches=matches)
        return node, self.stale_payload(path, cached), {
            "node_id": nid, "name": node["name"],
            "error": health.get("error", "节点暂时离线"),
            "error_code": health.get("error_code", "connection_failed"),
            "last_seen": health.get("last_seen"), "offline_since": health.get("offline_since")}

    def public(self):
        with self.lock:
            return [{"id": n["id"], "name": n["name"], "color": n.get("color", ""),
                     **self.health.get(n["id"], {"online": None})}
                    for n in self.nodes if enabled(n)]

    def machines(self):
        """设置页用的完整名单：按注册表顺序，含停用的机器；顺序不随启用状态变，
        只由用户在设置页拖动决定。"""
        with self.lock:
            return [{"id": n["id"], "name": n["name"], "color": n.get("color", ""),
                     "enabled": enabled(n),
                     **(self.health.get(n["id"], {"online": None}) if enabled(n) else {"online": None})}
                    for n in self.nodes]

    def reorder(self, ids):
        """按设置页拖出来的顺序重排注册表；必须是全部机器（含停用的）的一个排列。"""
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise ValueError("ids 必须是机器 id 列表")
        with self.lock:
            known = [n["id"] for n in self.nodes]
            if sorted(ids) != sorted(known) or len(set(ids)) != len(ids):
                raise ValueError("顺序必须包含每台机器各一次")
            if ids != known:
                by_id = {n["id"]: n for n in self.nodes}
                self.nodes = [by_id[x] for x in ids]
                self.save()
            return [n["id"] for n in self.nodes]


class HubHandler(server.Handler):
    # page_id → 最近一次成功从 uid 解析出的节点；满了淘汰最老的条目。
    _page_nodes = {}
    _page_nodes_lock = threading.Lock()
    _PAGE_NODES_MAX = 512

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
            if path == "/api/nodes" and self.command == "GET":
                return self._json({"mode": "hub", "nodes": self.registry.public(),
                                   "machines": self.registry.machines()})
            if path == "/api/nodes/order" and self.command == "POST":
                return self.set_order(self.read_body())
            display = re.fullmatch(r"/api/nodes/([a-f0-9]{32})/display", path)
            if display and self.command == "POST":
                return self.set_display(display[1], self.read_body())
            explicit = None
            match = re.fullmatch(r"/api/nodes/([a-f0-9]{32})(/api/.*)", path)
            if match:
                explicit, path = match.groups()
            if (self.command == 'GET' and path == '/api/session/file'
                    and self._file_navigation(query, explicit)):
                return
            if (self.command == "GET" and not explicit and path in
                    {"/api/sessions", "/api/search", "/api/live", "/api/term/list", "/api/trash"}):
                return self.aggregate(path, query)
            attachment = path in {"/api/session/attachment", "/api/session/files/upload"}
            body = self.read_body() if self.command == "POST" and not attachment else None
            if path == "/api/sessions/delete" and not explicit:
                return self.bulk_delete(body)
            if path == "/api/sessions/fork-visibility" and not explicit:
                return self.bulk_fork_visibility(body)
            if path == "/api/trash/purge" and body and body.get("all") and not explicit:
                return self.purge_all(body, query)
            if path == "/api/audit/browser" and not explicit:
                return self.browser_audit(body)
            nid, path, query, body = self.resolve(explicit, path, query, body)
            node = self.registry.get(nid)
            if not node:
                return self._json({"error": "机器未注册或已移除"}, 404)
            if self.registry.offline(nid) and not self.registry.recheck(node):
                state = self.registry.state(nid)
                self.close_connection = True
                return self._json({"error": f"{node['name']} 离线：{state.get('error', '中央站未能连接该机器')}",
                                   "node_offline": True, "node_id": nid,
                                   "offline_since": state.get("offline_since")}, 503)
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
        if path == "/api/search" and query.get("progress", [""])[0] == "1":
            return self.search_aggregate(nodes, upstream)
        # Never wait sequentially for a slow node. Each node has a bounded timeout,
        # and a node the monitor knows to be offline is skipped entirely.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(nodes)) or 1) as pool:
            results = list(pool.map(lambda n: self.registry.fetch(n, path, upstream), nodes))
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
                    "unavailable_reason": err["error"] if err else d.get("unavailable_reason", ""),
                    "sources": d.get("sources", {}), "home": d.get("home", ""),
                    # 终端后端是每台机器各自的设置，网页按机器分别展示和切换。
                    "backend": d.get("backend", ""),
                    "backends": d.get("backends", []) if not err else []}
                if not err:
                    for source, available in d.get("sources", {}).items():
                        result["sources"][source] = result["sources"].get(source, False) or available
        elif path == "/api/trash":
            result["items"] = [v for _, d, _ in results for v in d.get("items", [])]
            result["size"] = sum(d.get("size", 0) for _, d, _ in results)
            result["dir"] = "所选机器的本地回收站"
        return self._json(result)

    def search_aggregate(self, nodes, upstream):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        events = queue.Queue(maxsize=64)
        cancelled = threading.Event()

        def emit(event):
            self.wfile.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()

        def put(event):
            while not cancelled.is_set():
                try:
                    events.put(event, timeout=.2)
                    return
                except queue.Full:
                    continue
            raise ConnectionAbortedError("search cancelled")

        def scan(node):
            try:
                result = self.registry.fetch(node, "/api/search", upstream,
                    progress=lambda done, total: put(("progress", node["id"], done, total)),
                    matches=lambda rows: put(("matches", rows)))
            except Exception:
                # Always finish this node, even if its payload is malformed.
                result = (node, {}, {"node_id": node["id"], "name": node["name"],
                                     "error": "节点搜索失败"})
            try:
                put(("result", result))
            except ConnectionAbortedError:
                pass

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(nodes)) or 1)
        results = []
        states = {n["id"]: {"id": n["id"], "name": n["name"], "done": 0,
                            "total": None, "state": "preparing"} for n in nodes}
        for node in nodes:
            if self.registry.offline(node["id"]):
                states[node["id"]].update(total=0, state="offline")

        def report_progress():
            rows = list(states.values())
            emit({"type": "progress", "done": sum(row["done"] for row in rows),
                  "total": sum(row["total"] or 0 for row in rows),
                  "total_known": all(row["total"] is not None for row in rows),
                  "nodes": rows})

        try:
            report_progress()
            for node in nodes:
                pool.submit(scan, node)
            while len(results) < len(nodes):
                try:
                    event = events.get(timeout=1)
                except queue.Empty:
                    # Keep the browser/proxy alive while a node parses a large file.
                    emit({"type": "heartbeat"})
                    continue
                if event[0] == "progress":
                    _, nid, done, total = event
                    states[nid].update(done=done, total=total, state="scanning")
                    report_progress()
                elif event[0] == "matches":
                    emit({"type": "matches", "results": event[1]})
                else:
                    results.append(event[1])
                    node, data, error = event[1]
                    state = states[node["id"]]
                    if error:
                        state["state"] = "offline" if self.registry.offline(node["id"]) else "error"
                        if state["state"] == "offline" and state["total"] is None:
                            state["total"] = 0
                    else:
                        total = state["total"] if state["total"] is not None else data.get("total_pool", 0)
                        state.update(total=total, state="limited" if data.get("truncated") else "done",
                                     done=data.get("scanned", state["done"] if data.get("truncated") else total))
                    report_progress()
                    if data.get("results"):
                        emit({"type": "matches", "results": data["results"]})
            errors = [err for _, _, err in results if err]
            rows = [row for _, data, _ in results for row in data.get("results", [])]
            rows.sort(key=lambda row: (row.get("updated", ""), row["uid"]), reverse=True)
            emit({"type": "result", "data": {
                "errors": errors, "partial": bool(errors), "nodes": self.registry.public(),
                "results": rows, "truncated": any(d.get("truncated") for _, d, _ in results),
                "truncated_nodes": [n["id"] for n, d, _ in results if d.get("truncated")],
                "total_pool": sum(d.get("total_pool", 0) for _, d, _ in results)}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            cancelled.set()
            pool.shutdown(wait=False, cancel_futures=True)

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
                # Bug-report uploads have no session yet; the browser names the
                # machine with ?node= instead of a scoped uid.
                if (key == "uid" and path == "/api/session/attachment"
                        and query.get(key) == [server.bug_report.BUG_REPORT_UPLOAD_UID]):
                    continue
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

    def bulk_fork_visibility(self, body):
        visible = body.get("visible")
        if not isinstance(visible, bool):
            raise ValueError("需要布尔值 visible")
        groups = {}
        for uid in body.get("uids", []):
            nid, local = fed.split(uid, True)
            groups.setdefault(nid, []).append(local)
        if not groups:
            raise ValueError("没有选中任何父会话")
        result = {"ok": True, "updated": [], "errors": []}
        for nid, uids in groups.items():
            node = self.registry.get(nid)
            try:
                if not node:
                    raise ValueError("机器未注册")
                status, data = self.registry.request(
                    node, "/api/sessions/fork-visibility", "POST",
                    {"uids": uids, "visible": visible})
                if status != 200:
                    raise ValueError("更新失败")
                data = fed.public_payload(
                    data, node, "/api/sessions/fork-visibility")
                result["updated"].extend(data.get("updated", []))
                result["errors"].extend(data.get("errors", []))
            except (OSError, ValueError, http.client.HTTPException):
                result["errors"].extend({
                    "uid": fed.qualify(nid, uid, True),
                    "error": "机器请求失败，请核对结果",
                } for uid in uids)
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

    def set_display(self, nid, body):
        """网页只改机器的名称、配色和是否启用；接机器、下机器、地址和凭据仍是服务器端操作。"""
        body = body if isinstance(body, dict) else {}
        node = self.registry.find(nid)
        if not node:
            return self._json({"error": "机器未注册或已移除"}, 404)
        before = {"name": node["name"], "color": node.get("color", ""), "enabled": enabled(node)}
        flag = body.get("enabled")
        if flag is not None and not isinstance(flag, bool):
            return self._json({"error": "enabled 只能是 true 或 false"}, 400)
        try:
            row = self.registry.update_display(
                nid, name=body.get("name"), color=body.get("color"), enabled_flag=flag)
        except KeyError:
            return self._json({"error": "机器未注册或已移除"}, 404)
        except ValueError as error:
            return self._json({"error": str(error)}, 400)
        if row != {"id": nid, **before}:
            server.audit.record("hub.node.display.changed", category="terminal",
                                data={"node_id": nid, "from": before,
                                      "to": {k: row[k] for k in ("name", "color", "enabled")}})
        return self._json({"ok": True, "node": row})

    def set_order(self, body):
        """设置页拖动后的机器顺序：机器筛选、新建会话下拉和设置页都按它排。"""
        body = body if isinstance(body, dict) else {}
        before = [n["id"] for n in self.registry.machines()]
        try:
            after = self.registry.reorder(body.get("ids"))
        except ValueError as error:
            return self._json({"error": str(error)}, 400)
        if after != before:
            server.audit.record("hub.node.order.changed", category="terminal",
                                data={"from": before, "to": after})
        return self._json({"ok": True, "machines": self.registry.machines()})

    def _remember_page_node(self, page_id, nid):
        if not page_id:
            return
        with self._page_nodes_lock:
            self._page_nodes.pop(page_id, None)
            self._page_nodes[page_id] = nid
            while len(self._page_nodes) > self._PAGE_NODES_MAX:
                self._page_nodes.pop(next(iter(self._page_nodes)))

    def browser_audit(self, body):
        groups = {}
        page_id = body.get("page_id") or ""
        for event in body.get("events", []):
            try:
                nid, uid = fed.split(event.get("uid") or body.get("uid") or "", True)
            except ValueError:
                with self._page_nodes_lock:
                    nid = self._page_nodes.get(page_id) if page_id else None
                if not nid:
                    continue
                uid = ""
            else:
                self._remember_page_node(page_id, nid)
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
        for key in ("Content-Type", "X-AgentHub-Page", "X-AgentHub-Trace", "X-AgentHub-Build", "Range"):
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
                        "Content-Security-Policy", "Content-Range", "Accept-Ranges"):
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
