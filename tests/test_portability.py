"""在一个没有 POSIX 扩展的解释器里跑真实代码路径。

Windows 节点上的故障没有一个是逻辑写错，全是代码默认自己在 POSIX 上：
`os.sysconf` 让整个服务导不进来，`os.kill(pid, 0)` 把宿主进程杀了，
`os.open(目录)` 让新建会话变成 `[Errno 13] Permission denied`，
`os.O_DIRECTORY` 的 AttributeError 从 `except OSError` 底下穿过去。
这些在 Linux 上按平常方式跑单测永远碰不到，只能等 Windows 节点炸给人看。

所以这里不按 bug 的形状写断言，而是换一个环境：子进程里把 POSIX 专有的
属性和模块拿掉、把 `os.open` 换成打不开目录的版本，再跑真正的写盘路径。
谁往这些路径里加一个 POSIX 假设，这里就会红，不必等 cetus。

两条腿走路，因为有些符号标准库自己也在用（`os.O_NONBLOCK`），沙箱里删不掉：
- `WindowsSandboxTests` 跑行为，抓运行到才会炸的；
- `SourceScanTests` 扫源码，抓沙箱删不掉的那部分符号。
"""
import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = PROJECT_ROOT / "agenthub"

# Windows 上不存在、而标准库在 POSIX 上也用不着的 os 属性。
# os.kill 故意留着：它在 Windows 上存在但语义完全不同（会终止进程），那是
# test_host 单独盯的事；删掉只会打断 subprocess 自己。
POSIX_ONLY_ATTRS = (
    "sysconf", "sysconf_names", "confstr", "confstr_names", "fork", "forkpty",
    "setsid", "setpgid", "setuid", "setgid", "getuid", "geteuid", "getgid",
    "getegid", "getgroups", "initgroups", "fchown", "lchown", "mkfifo", "mknod",
    "uname", "getpriority", "setpriority", "getloadavg", "sched_getaffinity",
    "O_DIRECTORY", "O_NOFOLLOW", "O_PATH", "O_TMPFILE", "O_NDELAY", "O_LARGEFILE",
)
# 这些符号 Windows 也没有，但标准库在 POSIX 上要用，沙箱删不掉，只能扫源码。
POSIX_ONLY_IN_SOURCE = (
    "O_NONBLOCK", "O_CLOEXEC", "O_SYNC", "O_DSYNC", "O_NOCTTY", "O_ASYNC",
    "wait3", "wait4", "waitpid", "WIFEXITED", "WEXITSTATUS", "killpg",
    "getpgid", "tcsetpgrp", "openpty", "login_tty", "pipe2", "statvfs",
) + POSIX_ONLY_ATTRS
# Windows 的 Python 里根本没有这几个模块，import 直接 ImportError。
POSIX_ONLY_MODULES = ("fcntl", "pty", "termios", "grp", "pwd", "resource", "syslog",
                      "tty", "crypt", "spwd", "nis")

PRELUDE = f'''
import errno, os, sys

# 先把对 os.name / sys.platform 敏感的标准库导进来（ctypes 会去拿只有
# Windows 才有的 _ctypes.FormatError），装扮之后它们就只读缓存了。
import ctypes, mimetypes, platform, selectors, shutil, socket, sqlite3, ssl
import subprocess, tempfile, threading, webbrowser
import http.server, urllib.request, pathlib

# 装扮成 win32 之后 tempfile 会去找 C:\temp 之类，一个都不存在就退回当前目录，
# 于是临时文件全掉进仓库里。先把真正的临时目录钉住。
_REAL_TMPDIR = tempfile.gettempdir()

os.name = "nt"
sys.platform = "win32"
tempfile.tempdir = _REAL_TMPDIR

# pathlib 按 os.name 挑实现，改完它就要造 WindowsPath，而那在 POSIX 上根本
# 实例化不了。这里要的只是"POSIX 扩展不存在"，路径语义仍按本机来，所以单独
# 给 pathlib 一个 name 还是 posix 的 os，其余属性照常转发。
class _PosixNameOS:
    name = "posix"

    def __getattr__(self, item):
        return getattr(os, item)

pathlib.os = _PosixNameOS()

for _name in {POSIX_ONLY_ATTRS!r}:
    if hasattr(os, _name):
        delattr(os, _name)

# 反过来，Windows 有而 POSIX 没有的标志也要在场，否则代码用 getattr 兜底时
# 测出来的是"两边都没有"，跟真实 Windows 不是一回事。
os.O_BINARY = getattr(os, "O_BINARY", 0x8000)
os.O_TEXT = getattr(os, "O_TEXT", 0x4000)
_WINDOWS_ONLY_FLAGS = os.O_BINARY | os.O_TEXT

_real_open = os.open

def _windows_open(path, flags, mode=0o777, *, dir_fd=None):
    """Windows 打不开目录：os.open 直接 PermissionError，不是 IsADirectoryError。

    只拦 agenthub 自己发起的调用：shutil.rmtree 这类在 POSIX 上本来就要拿
    目录的文件描述符，一并拦掉的话，测出来的全是沙箱自己的毛病。
    """
    caller = sys._getframe(1).f_globals.get("__name__", "")
    if caller.startswith("agenthub") and dir_fd is None and os.path.isdir(path):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))
    if dir_fd is not None:
        return _real_open(path, flags, mode, dir_fd=dir_fd)
    return _real_open(path, flags & ~_WINDOWS_ONLY_FLAGS, mode)

os.open = _windows_open

for _name in {POSIX_ONLY_MODULES!r}:
    sys.modules[_name] = None

sys.path.insert(0, {str(PROJECT_ROOT)!r})
'''


def run_as_windows(body: str) -> subprocess.CompletedProcess:
    script = PRELUDE + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=120, cwd=str(PROJECT_ROOT))


class WindowsSandboxTests(unittest.TestCase):
    def assertRuns(self, body: str, expect: str):
        done = run_as_windows(body)
        if done.returncode != 0 or expect not in done.stdout:
            self.fail(f"在没有 POSIX 的环境里失败了\n"
                      f"--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}")

    def test_every_module_imports_without_posix_extras(self):
        """一个模块级的 os.sysconf 就能让整个节点服务起不来（live.py 真这样过）。
        导入期没有第二次机会：服务根本到不了能报错的地方。"""
        self.assertRuns('''
            import importlib, pkgutil
            import agenthub
            names = ["agenthub." + m.name for m in pkgutil.iter_modules(agenthub.__path__)]
            names += ["agenthub.host." + m.name
                      for m in pkgutil.iter_modules([agenthub.__path__[0] + "/host"])]
            for name in sorted(names):
                importlib.import_module(name)
            print("IMPORTED", len(names))
        ''', "IMPORTED")

    def test_a_creation_receipt_is_written(self):
        """收据落盘后原本还要 fsync 它所在的目录——POSIX 的做法，Windows 上
        直接 Errno 13，新建会话就只剩一句 Permission denied。"""
        self.assertRuns('''
            import tempfile
            from pathlib import Path
            from agenthub import create_requests
            with tempfile.TemporaryDirectory() as root:
                create_requests.DATA_DIR = Path(root) / "create-requests"
                body = {"request_id": "receipt-0001", "source": "claude", "cwd": "C:/Users/zj"}
                first = create_requests.run(body, lambda: (200, {"name": "session"}))
                assert first == (200, {"name": "session"}), first
                # 收据的意义在于重放不会再启动一次
                again = create_requests.run(body, lambda: (200, {"name": "SECOND LAUNCH"}))
                assert again == first, again
            print("RECEIPT OK")
        ''', "RECEIPT OK")

    def test_the_claude_queue_persists(self):
        """队列写完也 fsync 目录，用的还是 O_DIRECTORY —— Windows 上这个常量
        不存在，AttributeError 会从 except OSError 底下直接穿过去。"""
        self.assertRuns('''
            import tempfile
            from pathlib import Path
            from agenthub import claude_queue
            with tempfile.TemporaryDirectory() as root:
                claude_queue.DATA_DIR = Path(root)
                claude_queue.QUEUE_FILE = Path(root) / "claude-send-queue.json"
                item, fresh = claude_queue.enqueue(
                    "claude:abc", "agenthub-claude-x", "hello", None, "req-1")
                assert fresh and item["id"] == "req-1", (item, fresh)
                assert claude_queue.QUEUE_FILE.exists(), "队列没落盘"
                assert claude_queue.list_for("claude:abc"), "读回来是空的"
            print("QUEUE OK")
        ''', "QUEUE OK")

    def test_the_file_manager_copies_a_file(self):
        """复制用 O_NOFOLLOW|O_NONBLOCK 防符号链接掉包，这两个标志 Windows 都没有。"""
        self.assertRuns('''
            import tempfile
            from pathlib import Path
            from agenthub import file_manager
            with tempfile.TemporaryDirectory() as root:
                manager = file_manager.Manager(Path(root) / "state")
                manager.save = lambda *a, **kw: None
                source = Path(root) / "source.txt"
                source.write_bytes(b"payload" * 1000)
                dest = Path(root) / "copy.txt"
                manager.copy(source, dest, {"id": "job-1", "bytes": 0})
                assert dest.read_bytes() == source.read_bytes(), "复制出来的内容不一样"
            print("COPY OK")
        ''', "COPY OK")

    def test_the_terminal_backends_report_themselves(self):
        """term_tmux 在模块级 import 过 fcntl/pty/termios，于是 Windows 上
        server → term → term_tmux 这条无条件导入链把整个服务带崩。"""
        self.assertRuns('''
            from agenthub import term, term_tmux
            assert term_tmux.available() is False, "没有 pty 还自称可用"
            assert term.default_backend() == "ptyhost", term.default_backend()
            rows = {b["name"]: b for b in term.backends()}
            assert set(rows) == {"tmux", "ptyhost"}, rows
            assert rows["tmux"]["available"] is False
            assert rows["tmux"]["unavailable_reason"], "不可用就得说清楚为什么"
            print("BACKENDS OK", sorted(rows))
        ''', "BACKENDS OK")

    def test_the_session_list_survives_without_proc(self):
        """/proc 只有 Linux 有；运行状态查不出来要降级，不能把列会话带崩。"""
        self.assertRuns('''
            from pathlib import Path
            from agenthub import live
            live.PROC_FS = Path("C:/nonexistent-proc")
            live.HAS_PROC = False
            live._psutil = lambda: None        # psutil 也没有：只能查不出运行状态
            live._cache.update(at=0.0, sids={}, paths={}, bare_claude={})
            assert live.snapshot(force=True) == ({}, {})
            session = {"uid": "claude:x", "source": "claude", "sid": "x",
                       "path": "C:/none.jsonl", "cwd": "C:/", "created": "2026-09-11T00:00:00Z"}
            assert live.is_live(session, force=True) is False
            assert live.started_at(session, force=True) is None
            assert live.active_processes([session], force=True) == ([], {"claude:x": []})
            print("LIVE OK")
        ''', "LIVE OK")

    def test_the_index_and_audit_write_their_stores(self):
        """会话索引和审计账本都要落盘，写盘路径同样不能假设 POSIX。
        audit.record 把异常吞成 False，所以这里盯返回值，不然写失败是静默的。"""
        self.assertRuns('''
            import tempfile
            from pathlib import Path
            from agenthub import audit, index
            with tempfile.TemporaryDirectory() as root:
                audit._global_store = audit.EventStore(Path(root) / "audit.sqlite3")
                try:
                    assert audit.record("test.event", data={"where": "sandbox"}) is True, \
                        "审计写不进去"
                    assert audit.flush(5.0) is True, "审计没刷盘"
                    assert (Path(root) / "audit.sqlite3").exists(), "审计库没建起来"
                    index.CACHE_DIR = Path(root) / "cache"
                    index.CACHE_FILE = index.CACHE_DIR / "index.json"
                    rows = index.load(force=True)
                    assert isinstance(rows, list), rows
                finally:
                    # Windows 不让删还开着的文件，写线程不收掉这里就 WinError 32。
                    # 真机上跑才暴露得出来，沙箱模拟不了文件锁。
                    audit._global_store.close()
                    audit._global_store = None
            print("STORES OK")
        ''', "STORES OK")


class SourceScanTests(unittest.TestCase):
    """沙箱删不掉的那些符号（标准库自己要用），只能在源码里找。

    判据是"引用处必须自己处理缺席"：写成 getattr(os, "X", 默认) ，或者放在
    一个平台判断里面。裸的 os.O_NONBLOCK 在 Windows 上就是 AttributeError。
    """

    def sources(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            yield path, ast.parse(path.read_text(), filename=str(path))

    def test_posix_only_symbols_are_never_referenced_bare(self):
        offenders = []
        for path, tree in self.sources():
            guarded = self.guarded_lines(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name) and node.value.id == "os"):
                    continue
                if node.attr not in POSIX_ONLY_IN_SOURCE:
                    continue
                if node.lineno in guarded:
                    continue
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} os.{node.attr}")
        self.assertEqual(offenders, [], "这些符号 Windows 上不存在，"
                         "要么 getattr(os, ..., 默认值)，要么放进平台判断里：\n"
                         + "\n".join(offenders))

    def guarded_lines(self, tree) -> set[int]:
        """收集"已经处理过缺席"的行号。

        算数的守卫有四种：getattr(os, "X", 默认)、if/三元里判平台或 hasattr、
        try 块。其余都算裸引用——Windows 上就是一个 AttributeError。
        """
        safe: set[int] = set()
        for node in ast.walk(tree):
            body = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "getattr" and len(node.args) == 3:
                body = [node]
            elif isinstance(node, ast.If) and self.guards_absence(node.test):
                body = node.body + node.orelse
            elif isinstance(node, ast.IfExp) and self.guards_absence(node.test):
                body = [node.body, node.orelse]
            elif isinstance(node, ast.Try):
                body = node.body
            if body is None:
                continue
            for item in body:
                for inner in ast.walk(item):
                    line = getattr(inner, "lineno", None)
                    if line is not None:
                        safe.add(line)
        return safe

    def guards_absence(self, test) -> bool:
        """这个条件是不是在判平台、或者在判这个符号到底存不存在。"""
        for node in ast.walk(test):
            if isinstance(node, ast.Name) and node.id in ("WINDOWS", "LINUX", "HAS_PROC"):
                return True
            if isinstance(node, ast.Attribute) and node.attr in ("name", "platform"):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("hasattr", "getattr"):
                return True
        return False

    def test_posix_only_modules_are_imported_defensively(self):
        """模块级 import fcntl/pty/termios 会让 Windows 上的服务在导入期就死。"""
        offenders = []
        for path, tree in self.sources():
            protected = {line for node in ast.walk(tree) if isinstance(node, ast.Try)
                         for item in node.body for n in ast.walk(item)
                         if (line := getattr(n, "lineno", None)) is not None}
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in POSIX_ONLY_MODULES and node.lineno not in protected:
                        offenders.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} import {name}")
        self.assertEqual(offenders, [], "这些模块 Windows 上没有，"
                         "要放进 try/except ImportError：\n" + "\n".join(offenders))


class SandboxItselfTests(unittest.TestCase):
    """沙箱要是形同虚设，上面全部会变成一直绿的装饰品。"""

    def test_the_sandbox_actually_removes_posix(self):
        done = run_as_windows('''
            import os, sys
            assert os.name == "nt" and sys.platform == "win32"
            assert not hasattr(os, "sysconf"), "sysconf 还在"
            assert not hasattr(os, "O_DIRECTORY"), "O_DIRECTORY 还在"
            assert hasattr(os, "O_BINARY"), "Windows 该有 O_BINARY"
            for name in ("fcntl", "pty", "termios"):
                try:
                    __import__(name)
                except ImportError:
                    continue
                raise AssertionError(name + " 居然导进来了")
            print("SANDBOX OK")
        ''')
        self.assertIn("SANDBOX OK", done.stdout, done.stderr)

    def test_opening_a_directory_is_denied_like_on_windows(self):
        done = run_as_windows('''
            import errno, os, tempfile, types
            # 冒充 agenthub 里的代码：拦截只对本项目生效，标准库照常
            fake = types.ModuleType("agenthub.fake")
            exec("import os\\ndef probe(path):\\n    return os.open(path, os.O_RDONLY)",
                 fake.__dict__)
            with tempfile.TemporaryDirectory() as root:
                try:
                    fake.probe(root)
                except PermissionError as e:
                    assert e.errno == errno.EACCES, e
                    print("DENIED OK")
                else:
                    raise AssertionError("目录居然打开了，沙箱没生效")
                # 普通文件还得能正常打开，否则测出来的全是沙箱自己的毛病
                path = os.path.join(root, "f")
                os.close(fake.os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
                print("FILE OK")
        ''')
        self.assertIn("DENIED OK", done.stdout, done.stderr)
        self.assertIn("FILE OK", done.stdout, done.stderr)

    def test_the_source_scan_can_actually_fail(self):
        """扫描器要是认不出裸引用，SourceScanTests 也就是一直绿而已。"""
        scan = SourceScanTests("test_posix_only_symbols_are_never_referenced_bare")
        tree = ast.parse("import os\nfd = os.open(p, os.O_NOFOLLOW)\n")
        self.assertEqual(scan.guarded_lines(tree), set())
        guarded = ast.parse('import os\nflags = getattr(os, "O_NOFOLLOW", 0)\n')
        self.assertIn(2, scan.guarded_lines(guarded))
        branch = ast.parse('import os\nif os.name != "nt":\n    fd = os.open(d, os.O_DIRECTORY)\n')
        self.assertIn(3, scan.guarded_lines(branch))


if __name__ == "__main__":
    unittest.main()
