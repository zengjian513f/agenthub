"""整份读取的解析视图缓存：热读逐字节等于冷读，追加只解析尾部，改写失效。"""

import gzip
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import adapters, index, media, server, session_meta


def _claude_row(kind, uid, parent, text, ts="2026-08-11T08:00:00Z", **extra):
    row = {"type": kind, "uuid": uid, "parentUuid": parent, "isSidechain": False,
           "timestamp": ts, "cwd": "/tmp/project",
           "sessionId": "00000000-0000-0000-0000-000000000200", **extra}
    if kind in {"user", "assistant"}:
        row["message"] = {"role": kind, "content": text}
    return row


def _codex_row(kind, payload, ts="2026-08-09T10:00:00Z"):
    return {"timestamp": ts, "type": kind, "payload": payload}


def _codex_text(role, text, turn="turn-1", phase=None):
    payload = {"type": "message", "role": role, "turn_id": turn,
               "content": [{"type": "input_text" if role == "user" else "output_text",
                            "text": text}]}
    if phase:
        payload["phase"] = phase
    return _codex_row("response_item", payload)


def _grok_row(kind, text, **extra):
    return {"type": kind, "content": [{"type": "text", "text": text}], **extra}


def _write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")


def _append(path: Path, rows):
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    # 同一秒内追加时 mtime 也要前进，与真实写入间隔一致。
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _dump(result: dict) -> bytes:
    return json.dumps(result, ensure_ascii=False).encode()


class _ReadCounter:
    """记录每次 adapter 解析的起始偏移；续读只允许从旧 EOF 开始。"""

    def __init__(self):
        self.starts: list[int] = []
        self.original = adapters._iter_records

    def __call__(self, path, start=0):
        self.starts.append(int(start))
        return self.original(path, start)


class MessageViewCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patchers = [
            patch.object(index, "WINDOW_CACHE_DIR", self.root / "windows"),
            patch.object(session_meta, "DATA_DIR", self.root / "meta"),
            patch.object(session_meta, "META_FILE", self.root / "meta" / "session-meta.json"),
            patch.object(adapters, "CODEX_ROOT", self.root / "codex"),
            patch.object(adapters, "CODEX_INDEX", self.root / "missing-index"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        index._clear_view_cache()
        index._clear_window_cache_memory()
        self.addCleanup(index._clear_view_cache)
        self.addCleanup(index._clear_window_cache_memory)

    # ---- fixtures -------------------------------------------------------
    def claude_session(self, name="claude"):
        path = self.root / f"{name}.jsonl"
        _write(path, [
            _claude_row("user", "u0", None, "第一个问题"),
            _claude_row("assistant", "a0", "u0", [
                {"type": "text", "text": "先看一下"},
                {"type": "tool_use", "id": "call-1", "name": "Bash",
                 "input": {"command": "ls"}}]),
        ])
        session = {"uid": f"claude:{name}", "source": "claude",
                   "sid": "00000000-0000-0000-0000-000000000200",
                   "path": str(path), "cwd": "/tmp/project"}
        tail = [
            # 工具结果对应的调用在前缀里：工具名表必须跨越续读边界。
            _claude_row("user", "u1", "a0", [
                {"type": "tool_result", "tool_use_id": "call-1",
                 "content": "file.txt"}]),
            _claude_row("assistant", "a1", "u1", "看完了"),
            _claude_row("system", "s1", "a1", "", subtype="turn_duration",
                        durationMs=1200),
            _claude_row("user", "u2", "a1", "第二个问题"),
        ]
        return session, path, tail

    def codex_session(self, name="rollout"):
        path = self.root / "codex" / "2026" / "08" / "09" / f"rollout-{name}.jsonl"
        sid = f"00000000-0000-0000-0000-0000000003{len(name):02d}"
        _write(path, [
            _codex_row("session_meta", {"id": sid, "session_id": sid,
                                        "timestamp": "2026-08-09T10:00:00Z",
                                        "cwd": "/tmp/project"}),
            _codex_row("event_msg", {"type": "task_started", "turn_id": "turn-1"}),
            _codex_text("user", "请修一下"),
            _codex_text("assistant", "我先看看", phase="commentary"),
            _codex_row("response_item", {
                "type": "function_call", "name": "shell", "call_id": "c1",
                "turn_id": "turn-1", "arguments": json.dumps({"command": ["ls"]})}),
        ])
        session = {"uid": f"codex:{name}", "source": "codex", "sid": sid,
                   "path": str(path), "cwd": "/tmp/project"}
        tail = [
            # 输出对应的调用在前缀里；turn_aborted 要回填前缀中的最后一条进展。
            _codex_row("response_item", {
                "type": "function_call_output", "call_id": "c1", "turn_id": "turn-1",
                "output": "file.txt"}),
            _codex_row("event_msg", {"type": "turn_aborted", "turn_id": "turn-1",
                                     "reason": "interrupted"}),
            _codex_text("user", "换个思路", turn="turn-2"),
            _codex_row("event_msg", {"type": "task_started", "turn_id": "turn-2"}),
        ]
        return session, path, tail

    def grok_session(self, name="grok"):
        directory = self.root / name
        path = directory / "chat_history.jsonl"
        _write(path, [
            _grok_row("user", "你好", prompt_index=0),
            _grok_row("assistant", "在", tool_calls=[
                {"id": "t1", "name": "read_file", "arguments": "{\"path\": \"a\"}"}]),
        ])
        session = {"uid": f"grok:{name}", "source": "grok", "sid": name,
                   "path": str(directory), "cwd": "/tmp/project"}
        tail = [
            {"type": "tool_result", "tool_call_id": "t1",
             "content": [{"type": "text", "text": "内容"}]},
            _grok_row("assistant", "读完了"),
            _grok_row("user", "谢谢", prompt_index=1),
        ]
        return session, path, tail

    def all_sessions(self):
        return [self.claude_session(), self.codex_session(), self.grok_session()]

    def cold(self, session):
        index._clear_view_cache()
        return index.messages_for(session)

    # ---- tests ----------------------------------------------------------
    def test_hot_read_equals_cold_read_byte_for_byte(self):
        for session, path, tail in self.all_sessions():
            _append(path, tail)
            cold = self.cold(session)
            self.assertGreater(len(cold["messages"]), 3, session["source"])
            adapter = index.ADAPTERS[session["source"]]
            with patch.object(adapter, "read_state",
                              side_effect=AssertionError("hot read must not parse")):
                hot = index.messages_for(session)
            self.assertEqual(_dump(hot), _dump(cold), session["source"])
            self.assertEqual(hot["messages"].json_bytes,
                             json.dumps(cold["messages"], ensure_ascii=False).encode())
            # 响应级拼接与 gzip 拼接都要还原出同一份字节。
            prefix, raw, suffix = server._spliced_json(hot)
            self.assertEqual(prefix + raw + suffix, _dump(cold))
            packed = b"".join(server._gzip_spliced(prefix, hot["messages"], suffix, 4))
            self.assertEqual(gzip.decompress(packed), _dump(cold))

    def test_append_after_cache_parses_only_the_tail_and_equals_cold(self):
        for session, path, tail in self.all_sessions():
            first = self.cold(session)
            first_end = first["end"]
            # 先让 gzip 流也进入缓存，追加后必须能续压而不是整份重压。
            first["messages"].deflate(4)
            _append(path, tail)
            counter = _ReadCounter()
            with patch.object(adapters, "_iter_records", counter):
                extended = index.messages_for(session)
            self.assertTrue(counter.starts, session["source"])
            self.assertTrue(all(start >= first_end for start in counter.starts),
                            (session["source"], counter.starts))
            cold = self.cold(session)
            self.assertEqual(_dump(extended), _dump(cold), session["source"])
            self.assertGreater(len(cold["messages"]), len(first["messages"]))
            prefix, raw, suffix = server._spliced_json(extended)
            packed = b"".join(server._gzip_spliced(prefix, extended["messages"], suffix, 4))
            self.assertEqual(gzip.decompress(packed), _dump(cold))
            # 增量续读游标看到的也是同一段尾部。
            increment = index.messages_for(
                session, start=first_end, head=first["version"]["head"],
                anchor=first["anchor"])
            self.assertFalse(increment["reset"])
            self.assertEqual(increment["end"], cold["end"])

    def test_codex_turn_abort_in_tail_patches_cached_prefix_like_a_cold_parse(self):
        session, path, tail = self.codex_session()
        self.cold(session)
        _append(path, tail)
        extended = index.messages_for(session)
        cold = self.cold(session)
        self.assertEqual(_dump(extended), _dump(cold))
        commentary = next(m for m in cold["messages"] if m.get("phase") == "progress")
        self.assertTrue(commentary.get("interrupted"))
        result = next(m for m in cold["messages"] if m["role"] == "tool_result")
        self.assertEqual(result["name"], "shell")

    def test_claude_tail_that_reclassifies_old_records_falls_back_to_full_parse(self):
        path = self.root / "branch.jsonl"
        _write(path, [
            _claude_row("user", "u0", None, "开头"),
            _claude_row("assistant", "a0", "u0", "回答"),
            _claude_row("user", "ua", "a0", "被丢弃的输入"),
            {"type": "last-prompt", "leafUuid": "a0"},
        ])
        session = {"uid": "claude:branch", "source": "claude",
                   "sid": "00000000-0000-0000-0000-000000000201",
                   "path": str(path), "cwd": "/tmp/project"}
        first = self.cold(session)
        self.assertEqual([m["text"] for m in first["messages"]], ["开头", "回答"])
        # 新输入挂在 a0 下：旧的 ua 从“隐藏”变成“已中断的兄弟输入”。
        _append(path, [_claude_row("user", "un", "a0", "新的输入")])
        extended = index.messages_for(session)
        cold = self.cold(session)
        self.assertEqual(_dump(extended), _dump(cold))
        self.assertIn("被丢弃的输入", [m["text"] for m in cold["messages"]])

    def test_truncation_and_rewrite_invalidate_and_still_reset_old_cursors(self):
        session, path, tail = self.codex_session()
        _append(path, tail)
        full = self.cold(session)
        cursor = {"start": full["end"], "head": full["version"]["head"],
                  "anchor": full["anchor"]}
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        _write(path, rows[:3])                       # 回退截断
        cold = self.cold(session)
        after_truncate = index.messages_for(session)
        self.assertEqual(_dump(after_truncate), _dump(cold))
        self.assertEqual(len(cold["messages"]), 1)
        reset = index.messages_for(session, **cursor)
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["messages"], cold["messages"])
        probe = index.messages_for(session, append_only=True, **cursor)
        self.assertTrue(probe["reset"])
        self.assertEqual(probe["messages"], [])
        self.assertEqual(probe["end"], path.stat().st_size)
        # 等长改写：大小不变，仍必须失效。
        text = path.read_text().replace("请修一下", "请改一下")
        path.write_text(text)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertEqual(index.messages_for(session)["messages"][0]["text"], "请改一下")

    def test_incomplete_trailing_line_stays_outside_the_committed_view(self):
        session, path, tail = self.grok_session()
        complete = path.stat().st_size
        line = json.dumps(_grok_row("user", "半行", prompt_index=1))
        with path.open("a") as fh:
            fh.write(line[:-5])
        partial = index.messages_for(session)
        self.assertEqual(partial["end"], complete)
        self.assertNotIn("半行", [m["text"] for m in partial["messages"]])
        with path.open("a") as fh:
            fh.write(line[-5:] + "\n")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        counter = _ReadCounter()
        with patch.object(adapters, "_iter_records", counter):
            done = index.messages_for(session)
        self.assertIn("半行", [m["text"] for m in done["messages"]])
        self.assertEqual(done["end"], path.stat().st_size)
        self.assertEqual(counter.starts, [complete])
        self.assertEqual(_dump(done), _dump(self.cold(session)))

    def test_cache_bound_evicts_least_recently_used_view(self):
        sessions = [self.grok_session(f"g{i}")[0] for i in range(3)]
        with patch.object(index, "VIEW_CACHE_MAX_ITEMS", 2):
            for session in sessions:
                index.messages_for(session)
            self.assertEqual(index.view_cache_stats()["items"], 2)
            adapter = index.ADAPTERS["grok"]
            with patch.object(adapter, "read_state", wraps=adapter.read_state) as read:
                index.messages_for(sessions[2])       # 最近使用：命中
                index.messages_for(sessions[1])       # 命中
                self.assertEqual(read.call_count, 0)
                index.messages_for(sessions[0])       # 已淘汰：重新解析
                self.assertEqual(read.call_count, 1)
        index._clear_view_cache()
        with patch.object(index, "VIEW_CACHE_MAX_BYTES", 1):
            # 字节上限在写入时生效：放不下的视图不进缓存，读取仍然正确。
            index.messages_for(sessions[1])
            self.assertEqual(index.view_cache_stats()["items"], 0)
            self.assertEqual(_dump(index.messages_for(sessions[1])),
                             _dump(self.cold(sessions[1])))

    def test_concurrent_cold_readers_parse_once_and_share_the_result(self):
        session, path, tail = self.claude_session()
        _append(path, tail)
        adapter = index.ADAPTERS["claude"]
        barrier = threading.Barrier(8)
        results, errors = [], []
        original = adapter.read_state
        calls = []

        def slow_read(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(_dump(index.messages_for(session)))
            except Exception as error:      # noqa: BLE001
                errors.append(error)

        with patch.object(adapter, "read_state", side_effect=slow_read):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(results), 8)

    def test_hit_requires_media_tokens_to_still_resolve(self):
        image = self.root / "shot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        session, path, tail = self.grok_session()
        _append(path, [_grok_row("assistant", f"截图在 {image}")])
        cold = self.cold(session)
        tokens = index._media_tokens(cold["messages"])
        self.assertEqual(len(tokens), 1)
        self.assertIsNotNone(media.get(tokens[0]))
        with media._lock:
            media._items.clear()
        adapter = index.ADAPTERS["grok"]
        with patch.object(adapter, "read_state", wraps=adapter.read_state) as read:
            hot = index.messages_for(session)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(_dump(hot), _dump(cold))
        self.assertIsNotNone(media.get(tokens[0]))

    def test_initial_window_is_served_from_the_view_without_reparsing(self):
        session, path, tail = self.claude_session()
        _append(path, tail)
        with patch.object(index, "WINDOW_CACHE_MIN_BYTES", 0):
            full = self.cold(session)
            adapter = index.ADAPTERS["claude"]
            with patch.object(adapter, "read_state",
                              side_effect=AssertionError("window must reuse the view")):
                window = index.messages_for(session, windowed=True)
            self.assertEqual(window["messages"], full["messages"])
            self.assertEqual(window["message_total"], full["message_total"])
            # 窗口值经过缓存深拷贝，也必须已经带上媒体信息而不再二次发现。
            _append(path, [_claude_row("user", "u3", "u2", "再来一条")])
            counter = _ReadCounter()
            with patch.object(adapters, "_iter_records", counter):
                grown = index.messages_for(session, windowed=True)
            self.assertTrue(all(start >= full["end"] for start in counter.starts),
                            counter.starts)
            self.assertEqual(grown["messages"], self.cold(session)["messages"])


class LatestTipAfterTests(unittest.TestCase):
    """反向扫描的区间末叶必须与正向全扫逐一相同，包括跨块、无信号填充和行中间的 end。"""

    @staticmethod
    def forward(path, start, end, agent=None):
        tip = None
        for rec, off in adapters._iter_records(path, start):
            if end is not None and off > end:
                break
            signal = adapters.ClaudeAdapter._lineage_signal(rec, agent)
            if signal:
                tip = signal
        return tip

    def test_reverse_scan_matches_forward_scan_on_every_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tips.jsonl"
            rows = []
            for i in range(6):
                rows.append(_claude_row("user", f"u{i}", f"a{i-1}" if i else None, f"问 {i}"))
                # 超过一个 64 KiB 块的无信号填充，迫使反向扫描跨块拼接半行。
                rows.append({"type": "progress", "pad": "x" * (70 * 1024 if i % 2 else 10)})
                rows.append(_claude_row("assistant", f"a{i}", f"u{i}", f"答 {i}"))
            rows.append({"type": "last-prompt", "leafUuid": "a3"})
            _write(path, rows)
            raw = path.read_bytes()
            boundaries = [0] + [i + 1 for i, b in enumerate(raw) if b == 10]
            probes = sorted(set(boundaries + [b + 3 for b in boundaries[:-1]]
                                + [len(raw) - 1, len(raw)]))
            starts = [0] + boundaries[3:12:4] + [boundaries[-3]]
            checked = 0
            for start in starts:
                for end in probes:
                    if end < start:
                        continue
                    self.assertEqual(
                        adapters.ClaudeAdapter.latest_tip_after(str(path), start, end=end),
                        self.forward(str(path), start, end), (start, end))
                    checked += 1
                self.assertEqual(
                    adapters.ClaudeAdapter.latest_tip_after(str(path), start),
                    self.forward(str(path), start, None), start)
            self.assertGreater(checked, 50)
            self.assertEqual(adapters.ClaudeAdapter.latest_tip_after(str(path), 0), "a3")


class SplicedJsonResponseTests(unittest.TestCase):
    def _handler(self, accept: str):
        handler = object.__new__(server.Handler)
        handler.headers = {"Accept-Encoding": accept}
        handler.wfile = io.BytesIO()
        sent = {}
        handler.send_response = lambda code: sent.__setitem__("code", code)
        handler.send_header = lambda k, v: sent.__setitem__(k, v)
        handler.end_headers = lambda: None
        return handler, sent

    def test_handler_json_emits_identical_bytes_with_and_without_cached_body(self):
        msgs = [{"role": "user", "text": f"消息 {i}", "n": i} for i in range(400)]
        view = index._View()
        view.messages = msgs
        view.body = json.dumps(msgs, ensure_ascii=False).encode()
        cached = {"meta": {"uid": "x"}, "messages": index.CachedMessages(
            (dict(m) for m in msgs), view), "end": 7, "prompt": None}
        plain = {**cached, "messages": msgs}
        for accept in ("identity", "gzip"):
            outputs = []
            for obj in (cached, plain):
                handler, sent = self._handler(accept)
                handler._json(obj)
                body = handler.wfile.getvalue()
                if sent.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                self.assertEqual(int(sent["Content-Length"]), len(handler.wfile.getvalue()))
                self.assertEqual(int(sent["X-AgentHub-Decoded-Length"]), len(body))
                outputs.append(body)
            self.assertEqual(outputs[0], outputs[1], accept)
            self.assertEqual(outputs[0], json.dumps(plain, ensure_ascii=False).encode())


if __name__ == "__main__":
    unittest.main()
