"""ptyhost: 协议、真实 pty 会话进程、term 调度与后端选择。"""

import json
import os
import re
import stat
import socket
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import live, server, term, term_host, term_tmux
from agenthub.host import client, procs
from agenthub.host.protocol import FRAME_DATA, FRAME_RESIZE, pack_frame, read_frames

POSIX = os.name == "posix"
ROOT = Path(__file__).resolve().parent.parent
HOST_BIN = term_host.host_binary()
NEEDS_BIN = "需要先构建会话宿主: cd host-rs && cargo build --release"


class ProtocolTests(unittest.TestCase):
    def test_frames_round_trip_across_partial_reads(self):
        stream = pack_frame(FRAME_DATA, b"abc") + pack_frame(FRAME_RESIZE, b"{}")
        buffer = bytearray(stream[:7])
        self.assertEqual(read_frames(buffer), [])
        buffer.extend(stream[7:])
        self.assertEqual(read_frames(buffer), [(FRAME_DATA, b"abc"), (FRAME_RESIZE, b"{}")])
        self.assertEqual(buffer, bytearray())


@unittest.skipUnless(POSIX, "宿主进程测试需要 POSIX pty")
@unittest.skipUnless(HOST_BIN, NEEDS_BIN)
class SessionProcessTests(unittest.TestCase):
    """Web 服务的 Python 客户端 × Rust 宿主：协议与行为。"""

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
            [HOST_BIN, "--dir", str(self.dir), "run",
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
                         [("agenthub-t1", info["pid"], True, "ptyhost")])
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
        # resize 是一帧异步消息, 等它生效而不是假设它已经生效
        self.assertTrue(att.resize(100, 30))
        deadline = time.monotonic() + 3
        while (time.monotonic() < deadline
               and client.session_info("agenthub-t1", self.dir)["cols"] != 100):
            time.sleep(0.02)
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

    def test_a_cursor_query_is_answered_with_the_position_at_that_point(self):
        """ConPTY 以 INHERIT_CURSOR 创建伪控制台：conhost 先问 ESC[6n，拿到
        ESC[row;colR 之前既不产出输出也不消费输入。宿主不答就是双向死锁。
        应答必须用"流里那个位置"的光标，不能用滞后的模型状态。"""
        probe = self.dir / "probe.py"
        probe.write_text(
            "import sys, tty\n"
            # 和 conhost 一样 raw 读：DSR 应答不带换行，cooked 模式会把它压在行缓冲里
            "tty.setraw(sys.stdin.fileno())\n"
            "sys.stdout.write('one\\r\\ntwo\\r\\nthree\\r\\n\\x1b[6n')\n"
            "sys.stdout.flush()\n"
            "answer = ''\n"
            "while not answer.endswith('R'):\n"
            "    ch = sys.stdin.read(1)\n"
            "    if not ch: sys.exit(2)\n"
            "    answer += ch\n"
            "sys.stdout.write('ANSWER ' + answer[2:] + '\\r\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.read(1)\n", encoding="utf-8")
        proc, _ = self.start("agenthub-dsr", sys.executable, "-u", str(probe),
                            cols=80, rows=24)
        text = self.wait_screen("agenthub-dsr", "ANSWER", timeout=8)
        # 三行输出之后光标在第 4 行第 1 列；1 起算即 4;1
        self.assertIn("ANSWER 4;1R", text, text)
        # 查询本身既不进画面也不进 attach 回放，否则浏览器会再答一遍
        self.assertNotIn("[6n", text)
        att = client.Attach("agenthub-dsr", 80, 24, directory=self.dir)
        replay = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and b"ANSWER" not in replay:
            replay += att.read(0.05)
        self.assertIn(b"ANSWER", replay)
        self.assertNotIn(b"\x1b[6n", replay)
        att.write(b"q")
        att.close()
        client.request("agenthub-dsr", "kill", self.dir)
        proc.wait(timeout=8)

    def test_stale_info_files_are_cleaned_when_host_is_gone(self):
        (self.dir / "agenthub-dead.json").write_text(json.dumps(
            {"name": "agenthub-dead", "host_pid": 2 ** 22 + 7, "pid": 1, "sock": "x"}))
        self.assertEqual(client.list_sessions(self.dir), [])
        self.assertFalse((self.dir / "agenthub-dead.json").exists())

    def test_host_survives_launcher_exit(self):
        launcher = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys;"
             "subprocess.Popen([sys.argv[1], '--dir', sys.argv[2], 'run',"
             " '--name', 'agenthub-t5', '--', 'sh', '-i'], start_new_session=True,"
             " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
             HOST_BIN, str(self.dir)], cwd=str(ROOT))
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
@unittest.skipUnless(HOST_BIN, NEEDS_BIN)
class TermHostBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agenthub-termhost-")
        self.env = patch.dict(os.environ, {
            "AGENTHUB_HOST_DIR": self.tmp.name, "AGENTHUB_HOST_SCOPE": "0",
            "AGENTHUB_HOST_ENV_WRAPPER": "", "AGENTHUB_TERM_BACKEND": "ptyhost"})
        self.env.start()
        # 必须隔离真实的后端选择文件：它是这台机器的持久设置，优先于环境变量。
        # 不隔离的话，开发机上一旦在网页里选过 tmux，这组测试就会把会话建到
        # 真实的 tmux server 里，污染生产会话列表。
        self.backend_file = patch.object(
            term, "BACKEND_FILE", Path(self.tmp.name) / "terminal-backend")
        self.backend_file.start()
        self.audit = patch.object(term_host.audit, "record")
        self.audit.start()
        term.configure(None)
        self.assertIs(term.primary(), term_host, "这组测试必须跑在宿主后端上")

    def tearDown(self):
        for row in term_host.list_sessions():
            try:
                term_host.kill_session(row["name"])
            except RuntimeError:
                pass
        self.audit.stop()
        self.backend_file.stop()
        self.env.stop()
        term.configure(None)
        self.tmp.cleanup()

    def test_new_session_runs_shell_command_and_reports_pane_shape(self):
        name = term.new_session("shell-a", "sh -i", self.tmp.name, 50, 12)
        self.assertEqual(name, "agenthub-shell-a")
        row = term.session_info(name)
        self.assertEqual((row["cols"], row["rows"], row["owned"], row["server"]),
                         (50, 12, True, "ptyhost"))
        self.assertTrue(term.has_session(name))
        self.assertEqual(term.backend_name(), "ptyhost")
        self.assertEqual(term.cursor_position(name)[1], 0)
        term.submit_text(name, "echo sub-$((5*5))")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "sub-25" not in term.capture_screen_plain(name):
            time.sleep(0.05)
        screen, cursor = term.capture_screen_state(name)
        self.assertIn("sub-25", re.sub(r"\x1b\\[[0-9;:]*m", "", screen))
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
        with patch.object(term_host, "host_binary", return_value="/nonexistent/agenthub-host"):
            with self.assertRaisesRegex(RuntimeError, "会话启动失败"):
                term.new_session("bad", "whatever", self.tmp.name)
        self.assertEqual(term_host.list_sessions(), [])
        # 完全没有宿主程序时，控制台要给出可操作的原因而不是静默不可用
        with patch.object(term_host, "host_binary", return_value=None):
            self.assertFalse(term_host.available())
            self.assertIn("cargo build", term_host.unavailable_reason())
        # 命令自己退出时与 tmux 一致: 会话先出现, 随 CLI 结束而消失。
        # 用一个短暂存活的命令,避免断言落在"宿主还没来得及被看见"的竞态上。
        name = term.new_session("dead", ["sh", "-c", "sleep 0.5; exit 3"], self.tmp.name)
        self.assertTrue(term.has_session(name))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and term.has_session(name):
            time.sleep(0.05)
        self.assertFalse(term.has_session(name))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(POSIX, "服务集成测试需要 POSIX pty")
@unittest.skipUnless(HOST_BIN, NEEDS_BIN)
class ServerHostBackendTests(unittest.TestCase):
    """真实 HTTP 服务 + WebSocket 走宿主后端: 列表、认领、attach、输入、resize、kill。"""

    def setUp(self):
        from agenthub import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory(prefix="agenthub-termsrv-")
        self.env = patch.dict(os.environ, {
            "AGENTHUB_HOST_DIR": self.tmp.name, "AGENTHUB_HOST_SCOPE": "0",
            "AGENTHUB_HOST_ENV_WRAPPER": "", "AGENTHUB_TERM_BACKEND": "ptyhost"})
        self.env.start()
        # 同 TermHostBackendTests：真实的后端选择文件优先于环境变量，必须隔离，
        # 否则开发机选过 tmux 后这组测试会去污染生产的 tmux 会话列表。
        self.backend_file = patch.object(
            term, "BACKEND_FILE", Path(self.tmp.name) / "terminal-backend")
        self.backend_file.start()
        self.audit = patch.object(term_host.audit, "record")
        self.audit.start()
        term.configure(None)
        self.assertIs(term.primary(), term_host, "这组测试必须跑在宿主后端上")
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
        self.backend_file.stop()
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
        self.assertEqual((row["server"], row["cols"]), ("ptyhost", 60))

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


class BackendSelectionTests(unittest.TestCase):
    """终端后端是每台机器的服务端设置，网页可切换，只影响新建会话。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agenthub-backend-")
        self.file = patch.object(term, "BACKEND_FILE", Path(self.tmp.name) / "terminal-backend")
        self.file.start()
        # 切换走 API 会写审计账本；不打桩的话测试会把事件写进生产的 audit.sqlite3。
        self.audit = patch.object(server.audit, "record")
        self.audit.start()
        term.configure(None)

    def tearDown(self):
        self.audit.stop()
        self.file.stop()
        term.configure(None)
        self.tmp.cleanup()

    @staticmethod
    def handler():
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        return handler

    def post(self, backend):
        with patch.object(server, "TERMINAL", True):
            return self.handler()._set_terminal_backend({"backend": backend})

    def test_a_process_that_never_configured_still_sees_the_choice(self):
        """持久化的选择是这台机器的唯一事实来源。脚本或工具直接 import term 时
        若退回默认值，就会把会话建到用户没选的那个后端里。"""
        term.configure("tmux")
        term.set_backend("ptyhost")
        # 模拟一个全新进程：三个模块级状态都没初始化过
        with patch.object(term, "_chosen", None), patch.object(term, "_default", None), \
                patch.object(term, "_loaded", False):
            self.assertEqual(term.backend_name(), "ptyhost")
            self.assertIs(term.primary(), term_host)

    def test_a_missing_binary_falls_back_instead_of_disabling_the_console(self):
        """默认是 ptyhost，但一台还没拷二进制的节点上它不可用；若因此关掉整个
        终端功能，连已有的 tmux 会话都会从列表里消失。"""
        term.configure(None)
        self.assertEqual(term.configured_backend(), "ptyhost")
        with patch.object(term_host, "host_binary", return_value=None), \
                patch.object(term_tmux, "available", return_value=True):
            self.assertEqual(term.backend_name(), "tmux")
            self.assertIs(term.primary(), term_tmux)
            self.assertTrue(term.available())
            rows = {b["name"]: b for b in term.backends()}
            self.assertTrue(rows["tmux"]["current"])
            self.assertFalse(rows["ptyhost"]["available"])
            # 退让不会被写成用户的选择
            self.assertFalse(term.BACKEND_FILE.exists())
        # 二进制到位后自动回到配置的默认
        with patch.object(term_host, "host_binary", return_value="/usr/local/bin/ptyhost"):
            self.assertEqual(term.backend_name(), "ptyhost")

    def test_a_choice_outlives_the_startup_default(self):
        self.assertEqual(term.configure("tmux"), "tmux")
        self.assertEqual(term.set_backend("ptyhost"), "ptyhost")
        # 重启时启动参数仍是 tmux，但网页选过的值优先
        self.assertEqual(term.configure("tmux"), "ptyhost")
        self.assertEqual(term.backend_name(), "ptyhost")
        self.assertIs(term.primary(), term_host)

    def test_the_listing_marks_the_current_backend_and_explains_the_others(self):
        term.configure("tmux")
        with patch.object(term_host, "available", return_value=False), \
                patch.object(term_host, "unavailable_reason", return_value="没装宿主程序。"):
            rows = {row["name"]: row for row in term.backends()}
        self.assertTrue(rows["tmux"]["current"])
        self.assertFalse(rows["ptyhost"]["current"])
        self.assertFalse(rows["ptyhost"]["available"])
        self.assertEqual(rows["ptyhost"]["unavailable_reason"], "没装宿主程序。")
        self.assertEqual(rows["tmux"]["label"], "tmux")

    def test_an_unavailable_or_unknown_backend_is_refused(self):
        term.configure("tmux")
        with patch.object(term_host, "available", return_value=False), \
                patch.object(term_host, "unavailable_reason", return_value="没装宿主程序。"):
            with self.assertRaisesRegex(ValueError, "没装宿主程序"):
                term.set_backend("ptyhost")
        with self.assertRaisesRegex(ValueError, "未知终端后端"):
            term.set_backend("nope")
        self.assertEqual(term.backend_name(), "tmux")
        self.assertFalse(term.BACKEND_FILE.exists())

    def test_switching_leaves_sessions_of_the_other_backend_reachable(self):
        term.configure("tmux")
        legacy = [{"name": "agenthub-legacy", "pid": 0, "owned": True, "server": "agenthub"}]
        with patch.object(term_tmux, "available", return_value=True), \
                patch.object(term_tmux, "list_sessions", return_value=legacy), \
                patch.object(term_tmux, "has_session",
                             side_effect=lambda n: n == "agenthub-legacy"), \
                patch.object(term_tmux, "send_keys") as keys, \
                patch.object(term_host, "list_sessions", return_value=[]), \
                patch.object(term_host, "has_session", return_value=False):
            term.set_backend("ptyhost")
            self.assertIs(term.primary(), term_host)
            # 新建走宿主，但旧 tmux 会话仍在列表里，也仍然能操作
            self.assertEqual([r["name"] for r in term.list_sessions()], ["agenthub-legacy"])
            term.send_keys("agenthub-legacy", "Enter")
            keys.assert_called_once_with("agenthub-legacy", "Enter")

    def test_the_api_reports_the_change_and_refuses_a_bad_value(self):
        term.configure("tmux")
        result = self.post("ptyhost")
        self.assertEqual(result["_status"], 200)
        self.assertEqual(result["backend"], "ptyhost")
        self.assertEqual([b["name"] for b in result["backends"] if b["current"]], ["ptyhost"])
        bad = self.post("nope")
        self.assertEqual(bad["_status"], 400)
        self.assertIn("未知终端后端", bad["error"])
        self.assertEqual(term.backend_name(), "ptyhost")

    def test_the_terminal_listing_carries_the_backend_for_the_settings_panel(self):
        term.configure("tmux")
        with patch.object(server, "TERMINAL", True), \
                patch.object(server.term, "list_sessions", return_value=[]), \
                patch.object(server.term, "available_sources", return_value={}), \
                patch.object(server.pending_store, "active", return_value=[]), \
                patch.object(server.debug_runs, "filter_rows", side_effect=lambda rows, _="": rows):
            listing = self.handler()._api_get("/api/term/list", {})
        self.assertEqual(listing["backend"], "tmux")
        self.assertEqual({b["name"] for b in listing["backends"]}, {"tmux", "ptyhost"})


class WindowsPortabilityTests(unittest.TestCase):
    """Windows 节点必须能起服务：导入期不能依赖 POSIX 模块，运行期不能依赖 /proc。"""

    def test_the_tmux_backend_imports_without_posix_pty_modules(self):
        # server → term → term_tmux 是无条件导入链。term_tmux 里曾经在模块级
        # import fcntl/pty/termios，于是 Windows 上整个节点服务根本起不来。
        self.assertIsNotNone(term_tmux.select)
        for name in ("fcntl", "pty", "termios"):
            self.assertTrue(hasattr(term_tmux, name), name)
        with patch.object(term_tmux, "pty", None), \
                patch.object(term_tmux.shutil, "which", return_value="/usr/bin/tmux"):
            self.assertFalse(term_tmux.available(), "没有 pty 时 tmux 后端不能自称可用")

    def test_without_proc_the_status_degrades_instead_of_failing(self):
        session = {"uid": "codex:x", "source": "codex", "sid": "x",
                   "path": "/tmp/none.jsonl", "cwd": "/tmp",
                   "created": "2026-09-11T00:00:00Z"}
        cached = dict(live._cache)
        try:
            with patch.object(live, "HAS_PROC", False), \
                    patch.object(live.os, "listdir",
                                 side_effect=AssertionError("不该去扫 /proc")):
                live._cache.update(at=0.0, sids={}, paths={}, bare_claude={})
                self.assertEqual(live.snapshot(force=True), ({}, {}))
                self.assertEqual(live.pids_of(session, force=True), [])
                self.assertFalse(live.is_live(session, force=True))
                self.assertIsNone(live.started_at(session, force=True))
                self.assertEqual(live.active_processes([session], force=True),
                                 ([], {session["uid"]: []}))
        finally:
            live._cache.clear()
            live._cache.update(cached)

    def test_liveness_on_windows_never_uses_os_kill(self):
        """os.kill(pid, 0) 在 Windows 上会终止进程：CPython 的实现是
        OpenProcess + TerminateProcess，信号值直接当退出码。列会话要对每个
        宿主 pid 判活，用它等于每次列表都把所有会话清掉。"""
        with patch.object(procs, "LINUX", False), \
                patch.object(procs, "_psutil", return_value=None), \
                patch.object(procs.sys, "platform", "win32"), \
                patch.object(procs.os, "kill",
                             side_effect=AssertionError("不能用 os.kill 探活")), \
                patch.object(procs, "_windows_gone", return_value=False) as probe:
            self.assertFalse(procs.gone(4242))
            probe.assert_called_once_with(4242)
            # 查不出来时必须当作"还活着"：误判已结束会把宿主的会话记录清掉
            probe.return_value = None
            self.assertFalse(procs.gone(4242))
            probe.return_value = True
            self.assertTrue(procs.gone(4242))
            probe.side_effect = OSError("ctypes 不可用")
            self.assertFalse(procs.gone(4242))

    def test_ptyhost_is_the_default_everywhere(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTHUB_TERM_BACKEND", None)
            self.assertEqual(term.default_backend(), "ptyhost")
            with patch.object(term, "WINDOWS", True):
                self.assertEqual(term.default_backend(), "ptyhost")
            with patch.dict(os.environ, {"AGENTHUB_TERM_BACKEND": "tmux"}):
                self.assertEqual(term.default_backend(), "tmux")
