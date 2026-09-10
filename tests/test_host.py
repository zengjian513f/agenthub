"""自制会话宿主: 屏幕模型、协议、真实 pty 会话进程与 term 调度。"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import term, term_host, term_tmux
from agenthub.host import client, procs
from agenthub.host.protocol import (FRAME_DATA, FRAME_RESIZE, key_bytes, pack_frame,
                                    read_frames)
from agenthub.host.screen import Screen, strip_sgr

POSIX = os.name == "posix"
ROOT = Path(__file__).resolve().parent.parent


class ScreenTests(unittest.TestCase):
    def test_lines_scroll_into_history_and_soft_wraps_join(self):
        s = Screen(10, 3)
        s.feed(b"hello\r\nworld\r\nabcdefghijklmno")
        self.assertEqual(s.screen_lines(styled=False), ["world", "abcdefghij", "klmno"])
        self.assertEqual([t for t, _ in s.history], ["hello"])
        self.assertEqual(s.scrollback_lines(100, styled=False, join=True),
                         ["hello", "world", "abcdefghijklmno"])
        self.assertEqual(s.cursor, (5, 2))

    def test_sgr_attributes_render_and_strip(self):
        s = Screen(20, 2)
        s.feed(b"\x1b[1;31mRED\x1b[0m ok \x1b[38;2;1;2;3mrgb\x1b[m")
        styled = s.screen_lines()[0]
        self.assertEqual(styled, "\x1b[0m\x1b[1;31mRED\x1b[0m ok \x1b[0m\x1b[38;2;1;2;3mrgb\x1b[0m")
        self.assertEqual(strip_sgr(styled), "RED ok rgb")
        self.assertEqual(s.screen_lines(styled=False)[0], "RED ok rgb")

    def test_cursor_moves_erase_and_insert_delete(self):
        s = Screen(10, 4)
        s.feed(b"line1\r\nline2\r\nline3\r\nline4")
        s.feed(b"\x1b[2;1H\x1b[K")                  # 清第二行
        s.feed(b"\x1b[1;3H\x1b[2@")                 # 第一行插两个空格
        s.feed(b"\x1b[4;1H\x1b[2P")                 # 第四行删两个字符
        self.assertEqual(s.screen_lines(styled=False), ["li  ne1", "", "line3", "ne4"])
        s.feed(b"\x1b[3;1H\x1b[M")                  # 删第三行
        self.assertEqual(s.screen_lines(styled=False), ["li  ne1", "", "ne4", ""])
        s.feed(b"\x1b[2J\x1b[H")
        self.assertEqual(s.screen_lines(styled=False), ["", "", "", ""])
        self.assertEqual(s.cursor, (0, 0))

    def test_scroll_region_does_not_leak_into_history(self):
        s = Screen(10, 4)
        s.feed(b"top\r\n\x1b[2;3r\x1b[2;1Ha\r\nb\r\nc\r\nd")
        self.assertEqual(s.screen_lines(styled=False), ["top", "c", "d", ""])
        self.assertEqual(len(s.history), 0)

    def test_ink_style_redraw_keeps_cursor_and_screen_consistent(self):
        s = Screen(20, 5)
        s.feed(b"> hi\r\n\x1b[2mthinking\x1b[0m\r\n")
        s.feed(b"\x1b[2A\x1b[J> hi there\r\ndone\r\n")   # 上移两行整段重绘
        self.assertEqual(s.screen_lines(styled=False), ["> hi there", "done", "", "", ""])
        self.assertEqual(s.cursor, (0, 2))

    def test_alternate_screen_restores_main_content(self):
        s = Screen(10, 3)
        s.feed(b"main\r\n")
        s.feed(b"\x1b[?1049h\x1b[HALT\x1b[?1049l")
        self.assertFalse(s.alt)
        self.assertEqual(s.screen_lines(styled=False), ["main", "", ""])
        self.assertEqual(s.cursor, (0, 1))

    def test_wide_and_combining_characters_take_correct_columns(self):
        s = Screen(6, 2)
        s.feed("你好e\u0301x".encode())
        self.assertEqual(s.screen_lines(styled=False)[0], "你好e\u0301x")
        self.assertEqual(s.cursor, (6 - 1, 0))
        s.feed("世界".encode())                          # 剩 1 列, 宽字符整体换行
        self.assertEqual(s.screen_lines(styled=False), ["你好e\u0301x", "世界"])
        self.assertTrue(s.wrapped[0])

    def test_resize_moves_rows_between_screen_and_history(self):
        s = Screen(10, 4)
        s.feed(b"a\r\nb\r\nc\r\nd")
        s.resize(10, 2)
        self.assertEqual(s.screen_lines(styled=False), ["c", "d"])
        self.assertEqual([t for t, _ in s.history], ["a", "b"])
        self.assertEqual(s.cursor, (1, 1))
        s.resize(10, 5)
        self.assertEqual(s.screen_lines(styled=False), ["a", "b", "c", "d", ""])
        self.assertEqual(s.cursor, (1, 3))

    def test_queries_are_answered_and_modes_tracked(self):
        s = Screen(10, 3)
        s.feed(b"\x1b[6n\x1b[c\x1b[18t\x1b[?1h\x1b[?2004h")
        self.assertEqual(s.responses, [b"\x1b[1;1R", b"\x1b[?1;2c", b"\x1b[8;3;10t"])
        self.assertTrue(s.app_cursor)
        self.assertTrue(s.bracketed_paste)
        s.feed(b"\x1b[?1l\x1b[?2004l")
        self.assertFalse(s.app_cursor)
        self.assertFalse(s.bracketed_paste)

    def test_osc_and_unknown_sequences_are_ignored(self):
        s = Screen(10, 2)
        s.feed(b"\x1b]0;title\x07a\x1b]8;;http://x\x1b\\b\x1b[?25l\x1b[2 qc")
        self.assertEqual(s.screen_lines(styled=False)[0], "abc")
        self.assertFalse(s.cursor_visible)

    def test_redraw_bytes_fill_viewport_and_place_cursor(self):
        s = Screen(10, 3)
        s.feed(b"a\r\nbb")
        out = s.redraw_bytes().decode()
        self.assertIn("a\r\nbb\r\n", out)
        self.assertTrue(out.endswith("\x1b[2;3H\x1b[?25h"))


class ProtocolTests(unittest.TestCase):
    def test_key_names_follow_tmux_conventions(self):
        self.assertEqual(key_bytes("Enter"), b"\r")
        self.assertEqual(key_bytes("C-d"), b"\x04")
        self.assertEqual(key_bytes("C-u"), b"\x15")
        self.assertEqual(key_bytes("Escape"), b"\x1b")
        self.assertEqual(key_bytes("Up"), b"\x1b[A")
        self.assertEqual(key_bytes("Up", app_cursor=True), b"\x1bOA")
        self.assertEqual(key_bytes("M-x"), b"\x1bx")
        self.assertEqual(key_bytes("BSpace"), b"\x7f")
        self.assertEqual(key_bytes("literal text"), b"literal text")

    def test_frames_round_trip_across_partial_reads(self):
        stream = pack_frame(FRAME_DATA, b"abc") + pack_frame(FRAME_RESIZE, b"{}")
        buffer = bytearray(stream[:7])
        self.assertEqual(read_frames(buffer), [])
        buffer.extend(stream[7:])
        self.assertEqual(read_frames(buffer), [(FRAME_DATA, b"abc"), (FRAME_RESIZE, b"{}")])
        self.assertEqual(buffer, bytearray())


@unittest.skipUnless(POSIX, "宿主进程测试需要 POSIX pty")
class SessionProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agenthub-host-")
        self.dir = Path(self.tmp.name)
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.tmp.cleanup()

    def start(self, name, *command, cols=40, rows=8):
        proc = subprocess.Popen(
            [sys.executable, "-m", "agenthub.host", "--dir", str(self.dir), "run",
             "--name", name, "--cwd", self.tmp.name, "--cols", str(cols), "--rows", str(rows),
             "--", *command],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=open(self.dir / f"{name}.log", "wb"), start_new_session=True)
        self.procs.append(proc)
        info = client.wait_for(name, self.dir, timeout=8, alive=lambda: proc.poll() is None)
        if info is None:                    # 不能无条件读 stderr: 宿主还活着时会阻塞
            self.fail((self.dir / f"{name}.log").read_text(errors="replace"))
        return proc, info

    def wait_screen(self, name, needle, timeout=3.0):
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = client.request(name, "capture", self.dir, kind="scrollback", lines=500,
                                  styled=False, join=True)["text"]
            if needle in text:
                return text
            time.sleep(0.05)
        self.fail(f"屏幕中没有 {needle!r}: {text!r}")

    def test_session_lifecycle_send_capture_attach_and_exit(self):
        proc, info = self.start("agenthub-t1", "sh", "-i")
        self.assertEqual(info["cols"], 40)
        rows = client.list_sessions(self.dir)
        self.assertEqual([(r["name"], r["pid"], r["owned"], r["server"]) for r in rows],
                         [("agenthub-t1", info["pid"], True, "host")])
        self.assertTrue(stat.S_ISSOCK(os.stat(info["sock"]).st_mode))
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)

        client.request("agenthub-t1", "send", self.dir, text="echo hi-$((6*7))")
        client.request("agenthub-t1", "keys", self.dir, keys=["Enter"])
        self.wait_screen("agenthub-t1", "hi-42")
        reply = client.request("agenthub-t1", "capture", self.dir, kind="screen", styled=True)
        self.assertEqual(len(reply["cursor"]), 2)
        self.assertEqual(client.request("agenthub-t1", "cursor", self.dir)["y"], reply["cursor"][1])

        att = client.Attach("agenthub-t1", 80, 24, directory=self.dir)
        got = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and b"hi-42" not in got:
            got += att.read(0.1)
        self.assertIn(b"hi-42", got)                       # attach 回放历史
        self.assertTrue(client.list_sessions(self.dir)[0]["attached"])
        att.write(b"echo live-$((7*7))\r")
        got = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and b"live-49" not in got:
            got += att.read(0.1)
        self.assertIn(b"live-49", got)
        self.assertTrue(att.resize(100, 30))
        self.wait_screen("agenthub-t1", "live-49")
        self.assertEqual(client.session_info("agenthub-t1", self.dir)["cols"], 100)
        att.close()

        client.request("agenthub-t1", "send", self.dir, text="exit\r")
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(list(self.dir.glob("agenthub-t1.json")) + list(self.dir.glob("agenthub-t1.sock")), [])
        self.assertEqual(client.list_sessions(self.dir), [])

    def test_attach_learns_about_exit_and_paste_respects_bracketed_mode(self):
        proc, _ = self.start("agenthub-t2", "sh", "-i")
        att = client.Attach("agenthub-t2", 80, 24, replay=False, directory=self.dir)
        client.request("agenthub-t2", "send", self.dir, text="printf '\\033[?2004h'\r")
        self.wait_screen("agenthub-t2", "2004h")
        time.sleep(0.1)
        reply = client.request("agenthub-t2", "paste", self.dir, text="echo pasted")
        self.assertTrue(reply["bracketed"])
        got = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and b"[201~" not in got:
            got += att.read(0.1)
        # sh 把收到的控制序列回显成 ^[[200~ ... ^[[201~, 证明输入端确实包了起止序列
        self.assertIn(b"^[[200~echo pasted^[[201~", got)
        client.request("agenthub-t2", "kill", self.dir)
        proc.wait(timeout=5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and att.alive():
            att.read(0.1)
        self.assertFalse(att.alive())
        att.close()

    def test_rename_moves_socket_and_info(self):
        proc, _ = self.start("agenthub-t3", "sh", "-i")
        client.request("agenthub-t3", "rename", self.dir, to="agenthub-t3-renamed")
        self.assertIsNone(client.session_info("agenthub-t3", self.dir))
        self.assertEqual(client.request("agenthub-t3-renamed", "info", self.dir)["info"]["name"],
                         "agenthub-t3-renamed")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.suffix != ".log"),
                         ["agenthub-t3-renamed.json", "agenthub-t3-renamed.sock"])
        client.request("agenthub-t3-renamed", "kill", self.dir)
        proc.wait(timeout=5)

    def test_stale_info_files_are_cleaned_when_host_is_gone(self):
        (self.dir / "agenthub-dead.json").write_text(json.dumps(
            {"name": "agenthub-dead", "host_pid": 2 ** 22 + 7, "pid": 1, "sock": "x"}))
        self.assertEqual(client.list_sessions(self.dir), [])
        self.assertFalse((self.dir / "agenthub-dead.json").exists())

    def test_host_survives_launcher_exit(self):
        launcher = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys;"
             "subprocess.Popen([sys.executable, '-m', 'agenthub.host', '--dir', sys.argv[1], 'run',"
             " '--name', 'agenthub-t5', '--', 'sh', '-i'], start_new_session=True,"
             " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
             str(self.dir)], cwd=str(ROOT))
        launcher.wait(timeout=10)
        info = client.wait_for("agenthub-t5", self.dir, timeout=8)
        self.assertIsNotNone(info)
        self.assertFalse(procs.gone(info["host_pid"]))
        client.request("agenthub-t5", "kill", self.dir)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and client.session_info("agenthub-t5", self.dir):
            time.sleep(0.05)
        self.assertIsNone(client.session_info("agenthub-t5", self.dir))


@unittest.skipUnless(POSIX, "宿主后端测试需要 POSIX pty")
class TermHostBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agenthub-termhost-")
        self.env = patch.dict(os.environ, {
            "AGENTHUB_HOST_DIR": self.tmp.name, "AGENTHUB_HOST_SCOPE": "0",
            "AGENTHUB_HOST_ENV_WRAPPER": "", "AGENTHUB_TERM_BACKEND": "host"})
        self.env.start()
        self.audit = patch.object(term_host.audit, "record")
        self.audit.start()
        term.configure(None)

    def tearDown(self):
        for row in term_host.list_sessions():
            try:
                term_host.kill_session(row["name"])
            except RuntimeError:
                pass
        self.audit.stop()
        self.env.stop()
        term.configure(None)
        self.tmp.cleanup()

    def test_new_session_runs_shell_command_and_reports_pane_shape(self):
        name = term.new_session("shell-a", "sh -i", self.tmp.name, 50, 12)
        self.assertEqual(name, "agenthub-shell-a")
        row = term.session_info(name)
        self.assertEqual((row["cols"], row["rows"], row["owned"], row["server"]),
                         (50, 12, True, "host"))
        self.assertTrue(term.has_session(name))
        self.assertEqual(term.backend_name(), "host")
        self.assertEqual(term.cursor_position(name)[1], 0)
        term.submit_text(name, "echo sub-$((5*5))")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "sub-25" not in term.capture_screen_plain(name):
            time.sleep(0.05)
        screen, cursor = term.capture_screen_state(name)
        self.assertIn("sub-25", strip_sgr(screen))
        self.assertEqual(len(cursor), 2)
        self.assertIn("sub-25", term.capture_plain(name, 80))
        self.assertIn("sub-25", term.capture_history(name, 100))
        self.assertTrue(term.in_tmux([row["pid"]]))
        self.assertTrue(term.process_belongs_to(row["pid"], row["pid"]))
        self.assertFalse(term_host.hosts([os.getpid()]))   # 测试进程自己可能跑在 tmux 里
        self.assertEqual(term.scroll(name, True), 0)
        term.leave_copy_mode(name)

        att = term.Attach(name, 80, 24)
        got = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and b"sub-25" not in got:
            got += att.read(0.1)
        self.assertIn(b"sub-25", got)
        att.close()

        stopped = term.graceful_stop(name, [row["pid"]], timeout=3.0)
        self.assertEqual(stopped, [row["pid"]])
        self.assertFalse(term.has_session(name))
        self.assertTrue(term.gone(row["pid"]))

    def test_dispatch_finds_sessions_in_either_backend(self):
        name = term.new_session("shell-b", ["sh", "-i"], self.tmp.name)
        fake_tmux = [{"name": "agenthub-legacy", "pid": 0, "owned": True, "server": "agenthub"}]
        with patch.object(term_tmux, "available", return_value=True), \
                patch.object(term_tmux, "list_sessions", return_value=fake_tmux), \
                patch.object(term_tmux, "has_session",
                             side_effect=lambda n: n == "agenthub-legacy"), \
                patch.object(term_tmux, "send_keys") as legacy_keys:
            names = [row["name"] for row in term.list_sessions()]
            self.assertEqual(names, [name, "agenthub-legacy"])
            term.send_keys("agenthub-legacy", "Enter")
            legacy_keys.assert_called_once_with("agenthub-legacy", "Enter")
            with self.assertRaisesRegex(RuntimeError, "会话不存在"):
                term.send_keys("agenthub-missing", "Enter")
        self.assertTrue(term.kill_session(name))
        self.assertFalse(term.kill_session(name))

    def test_new_cli_session_launches_whitelisted_cli_through_host(self):
        fake = Path(self.tmp.name) / "codex"
        fake.write_text("#!/bin/sh\necho fake-codex \"$@\"\nsleep 30\n")
        fake.chmod(0o755)
        with patch.object(term, "_which_cli", return_value=str(fake)):
            info = term.new_cli_session("codex", self.tmp.name, 60, 10)
        self.assertTrue(info["name"].startswith("agenthub-codex-new-"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline \
                and "fake-codex" not in term.capture_screen_plain(info["name"]):
            time.sleep(0.05)
        screen = term.capture_screen_plain(info["name"])
        self.assertIn("fake-codex --enable default_mode_request_user_input", screen)
        row = term.session_info(info["name"])
        self.assertEqual(row["cwd"], self.tmp.name)
        self.assertEqual(term.kill_pids([row["pid"]], timeout=2), [row["pid"]])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and term.has_session(info["name"]):
            time.sleep(0.05)
        self.assertFalse(term.has_session(info["name"]))

    def test_launch_failure_is_reported_and_dead_command_vanishes_like_tmux(self):
        with patch.object(term_host.sys, "executable", "/nonexistent/python"):
            with self.assertRaisesRegex(RuntimeError, "会话启动失败"):
                term.new_session("bad", "whatever", self.tmp.name)
        self.assertEqual(term_host.list_sessions(), [])
        # 命令本身立刻失败时与 tmux 一致: 创建成功, 会话随即消失, 由调用方复查
        name = term.new_session("dead", ["/nonexistent/cli"], self.tmp.name)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and term.has_session(name):
            time.sleep(0.05)
        self.assertFalse(term.has_session(name))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(POSIX, "服务集成测试需要 POSIX pty")
class ServerHostBackendTests(unittest.TestCase):
    """真实 HTTP 服务 + WebSocket 走宿主后端: 列表、认领、attach、输入、resize、kill。"""

    def setUp(self):
        from agenthub import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory(prefix="agenthub-termsrv-")
        self.env = patch.dict(os.environ, {
            "AGENTHUB_HOST_DIR": self.tmp.name, "AGENTHUB_HOST_SCOPE": "0",
            "AGENTHUB_HOST_ENV_WRAPPER": "", "AGENTHUB_TERM_BACKEND": "host"})
        self.env.start()
        self.audit = patch.object(term_host.audit, "record")
        self.audit.start()
        term.configure(None)
        self.terminal = server.TERMINAL
        server.TERMINAL = True
        server.ALLOWED_IPS.add("127.0.0.1")
        self.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.daemon_threads = True
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        for row in term_host.list_sessions():
            try:
                term_host.kill_session(row["name"])
            except RuntimeError:
                pass
        self.srv.shutdown()
        self.srv.server_close()
        self.server.TERMINAL = self.terminal
        self.server.ALLOWED_IPS.discard("127.0.0.1")
        self.audit.stop()
        self.env.stop()
        term.configure(None)
        self.tmp.cleanup()

    def api(self, path, body=None):
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_console_round_trip_through_http_and_websocket(self):
        import socket
        import urllib.parse
        from agenthub import wsock

        name = term.new_session("ws-a", ["sh", "-i"], self.tmp.name, 60, 12)
        status, listing = self.api("/api/term/list")
        self.assertEqual(status, 200)
        self.assertTrue(listing["enabled"], listing)
        row = next(x for x in listing["sessions"] if x["name"] == name)
        self.assertEqual((row["server"], row["cols"]), ("host", 60))

        status, claim = self.api("/api/term/claim", {"name": name, "page": "page-1"})
        self.assertEqual(status, 200, claim)
        query = urllib.parse.urlencode({"name": name, "page": "page-1", "token": claim["token"],
                                        "cols": 80, "rows": 24})
        sock = socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=10)
        sock.sendall((f"GET /api/term/attach?{query} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        self.assertTrue(head.startswith(b"HTTP/1.1 101"), head)

        wsock.send(sock, b"echo ws-$((9*9))\r", wsock.OP_BIN)
        got = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"ws-81" not in got:
            op, payload = wsock.recv(sock)
            if op == wsock.OP_BIN:
                got += payload
        self.assertIn(b"ws-81", got)
        wsock.send(sock, json.dumps({"t": "resize", "cols": 100, "rows": 30}).encode(), wsock.OP_TEXT)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and term.session_info(name)["cols"] != 100:
            time.sleep(0.05)
        self.assertEqual(term.session_info(name)["cols"], 100)

        status, _ = self.api("/api/term/kill", {"name": name})
        self.assertEqual(status, 200)
        self.assertFalse(term.has_session(name))
        deadline = time.monotonic() + 3
        closed = False
        while time.monotonic() < deadline and not closed:
            try:
                op, _ = wsock.recv(sock)
                closed = op == wsock.OP_CLOSE
            except (ConnectionError, OSError):
                closed = True
        self.assertTrue(closed)
        sock.close()
