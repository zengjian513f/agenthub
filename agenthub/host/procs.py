"""跨平台进程查询与结束。Linux 直接读 /proc, 其他平台退到 psutil。"""

from __future__ import annotations

import os
import sys
import time

LINUX = sys.platform.startswith("linux")


def _psutil():
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    return psutil


def _proc_stat(pid: int) -> tuple[str, str, int] | None:
    """返回 (进程名, 状态, 父 pid)。"""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            st = fh.read()
        name = st[st.index("(") + 1:st.rindex(")")]
        rest = st[st.rindex(")") + 2:].split()
        return name, rest[0], int(rest[1])
    except (OSError, ValueError, IndexError):
        return None


def parent_pid(pid: int) -> int | None:
    if LINUX:
        info = _proc_stat(pid)
        return info[2] if info else None
    ps = _psutil()
    if not ps:
        return None
    try:
        return ps.Process(pid).ppid()
    except Exception:
        return None


def name_of(pid: int) -> str:
    if LINUX:
        info = _proc_stat(pid)
        return info[0] if info else ""
    ps = _psutil()
    if not ps:
        return ""
    try:
        return ps.Process(pid).name()
    except Exception:
        return ""


def _windows_gone(pid: int) -> bool | None:
    """用 OpenProcess + WaitForSingleObject 判断进程是否已结束。

    绝不能在 Windows 上用 `os.kill(pid, 0)` 探活：CPython 在 Windows 的实现是
    OpenProcess + TerminateProcess，信号值直接当退出码，所以 `os.kill(pid, 0)`
    会把目标进程杀掉并让它以 0 退出。列会话要对每个宿主 pid 判活，那等于
    每次列表都把所有会话清掉。返回 None 表示查不出来，交给调用方退让。
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x0010
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_OBJECT_0 = 0x0
    ERROR_INVALID_PARAMETER = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 参数无效 = 没有这个 pid；其他错误（通常是权限）说明进程还在。
        return ctypes.get_last_error() == ERROR_INVALID_PARAMETER or None
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def gone(pid: int) -> bool:
    """进程是否已经结束。僵尸也算结束。"""
    if pid <= 0:
        return True
    if LINUX:
        info = _proc_stat(pid)
        return info is None or info[1] == "Z"
    ps = _psutil()
    if not ps:
        if sys.platform == "win32":
            try:
                result = _windows_gone(pid)
            except (OSError, AttributeError, ValueError):
                result = None
            # 查不出来就当它还活着：误判为"已结束"会让宿主的会话记录被清掉。
            return bool(result)
        try:
            os.kill(pid, 0)
            return False
        except OSError:
            return True
    try:
        proc = ps.Process(pid)
        return proc.status() == ps.STATUS_ZOMBIE
    except Exception:
        return True


def descendant_of(pid: int, root_pid: int, depth: int = 16) -> bool:
    """pid 是否等于或派生自 root_pid。"""
    cur = abs(int(pid))
    root = abs(int(root_pid))
    if root <= 0:
        return False
    for _ in range(depth):
        if cur == root:
            return True
        parent = parent_pid(cur)
        if parent is None:
            return False
        if parent <= 1:
            return parent == root
        cur = parent
    return False


def ancestor_matches(pid: int, predicate, depth: int = 16) -> bool:
    cur = abs(int(pid))
    for _ in range(depth):
        if predicate(cur):
            return True
        parent = parent_pid(cur)
        if parent is None or parent <= 1:
            return False
        cur = parent
    return False


def terminate(pid: int, force: bool = False) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        ps = _psutil()
        try:
            if ps:
                proc = ps.Process(pid)
                (proc.kill if force else proc.terminate)()
            else:
                os.kill(pid, 9)
            return True
        except Exception:
            return False
    import signal
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except OSError:
        return False


def kill_pids(pids: list[int], timeout: float = 6.0) -> list[int]:
    """先 TERM 让 CLI 有机会存盘, 不退再 KILL。返回真正被结束的 pid。"""
    targets = [p for p in pids if p > 0]
    killed = [p for p in targets if terminate(p)]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(gone(p) for p in killed):
            return killed
        time.sleep(0.15)
    for p in killed:
        if not gone(p):
            terminate(p, force=True)
    time.sleep(0.3)
    return killed
