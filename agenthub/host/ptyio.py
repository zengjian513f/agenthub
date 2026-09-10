"""平台 pty 抽象: POSIX 用 forkpty, Windows 用 ConPTY (pywinpty)。

两边都只暴露 spawn/read/write/resize/alive/close 和 pid。read 返回 b"" 表示
超时无数据, None 表示 EOF (子进程已退出且输出读尽)。
"""

from __future__ import annotations

import os
import sys
import threading

WINDOWS = sys.platform == "win32"


class BasePty:
    pid: int = 0

    def read(self, timeout: float = 0.1) -> bytes | None:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def alive(self) -> bool:
        raise NotImplementedError

    def exit_code(self) -> int | None:
        raise NotImplementedError

    def terminate(self, force: bool = False) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


if not WINDOWS:
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios

    class PosixPty(BasePty):
        def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str],
                     cols: int, rows: int):
            pid, fd = pty.fork()
            if pid == 0:                        # 子进程
                try:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                    if cwd:
                        os.chdir(cwd)
                    os.execvpe(argv[0], argv, env)
                except BaseException as e:      # noqa: BLE001 - 子进程只能报告后退出
                    os.write(2, f"agenthub-host: 启动失败: {e}\r\n".encode())
                finally:
                    os._exit(127)
            self.pid = pid
            self.fd = fd
            self._status: int | None = None
            self._eof = False
            self._hung_up = False

        def read(self, timeout: float = 0.1) -> bytes | None:
            if self._eof:
                return None
            try:
                r, _, _ = select.select([self.fd], [], [], timeout)
            except (OSError, ValueError):
                self._eof = True
                return None
            if not r:
                return b""
            try:
                data = os.read(self.fd, 65536)
            except OSError:                      # EIO: 子进程已关闭从属端
                self._eof = True
                return None
            if not data:
                self._eof = True
                return None
            return data

        def write(self, data: bytes) -> None:
            if self._hung_up:
                return
            view = memoryview(data)
            while view:
                try:
                    n = os.write(self.fd, view)
                except BlockingIOError:
                    select.select([], [self.fd], [], 1.0)
                    continue
                except OSError:
                    return
                view = view[n:]

        def resize(self, cols: int, rows: int) -> None:
            if self._hung_up:
                return
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

        def _reap(self, block: bool = False) -> None:
            if self._status is not None:
                return
            try:
                pid, status = os.waitpid(self.pid, 0 if block else os.WNOHANG)
            except ChildProcessError:
                self._status = -1
                return
            if pid == self.pid:
                self._status = status

        def alive(self) -> bool:
            self._reap()
            return self._status is None

        def exit_code(self) -> int | None:
            self._reap()
            if self._status is None:
                return None
            if self._status < 0:
                return 255
            if os.WIFSIGNALED(self._status):
                return 128 + os.WTERMSIG(self._status)
            return os.WEXITSTATUS(self._status)

        def terminate(self, force: bool = False) -> None:
            """与 tmux kill-session 一致: 先 HUP 整个前台进程组并挂断 pty。

            交互式 shell 会忽略 TERM 却响应 HUP; CLI 收到 HUP 也有机会存盘。
            force 时对进程组 KILL。
            """
            sig = signal.SIGKILL if force else signal.SIGHUP
            try:
                os.killpg(self.pid, sig)
            except OSError:
                try:
                    os.kill(self.pid, sig)
                except OSError:
                    pass
            if not force and not self._hung_up:
                self._hung_up = True
                # 关闭主端让从属端读到 EOF/EIO; 之后的 read 返回 None 触发收尾
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self._eof = True

        def close(self) -> None:
            if self._hung_up:
                return
            self._hung_up = True
            try:
                os.close(self.fd)
            except OSError:
                pass

    Pty = PosixPty

else:

    class WinPty(BasePty):
        """ConPTY 后端。pywinpty 的 read 只提供字符串, 这里重新编码为 UTF-8。

        尚未在真实 Windows 节点上验证; 接口按 pywinpty 2.x 编写。
        """

        def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str],
                     cols: int, rows: int):
            from winpty import PTY  # type: ignore[import-not-found]
            import subprocess

            self._pty = PTY(cols, rows)
            cmdline = subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else None
            env_block = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0" if env else None
            self._pty.spawn(argv[0], cmdline=cmdline, cwd=cwd, env=env_block)
            self.pid = int(self._pty.pid)
            self._queue: list[bytes] = []
            self._cond = threading.Condition()
            self._eof = False
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()

        def _pump(self) -> None:
            while True:
                try:
                    text = self._pty.read(65536, blocking=True)
                except Exception:
                    break
                if not text:
                    if not self._pty.isalive():
                        break
                    continue
                with self._cond:
                    self._queue.append(text.encode("utf-8", "replace"))
                    self._cond.notify_all()
            with self._cond:
                self._eof = True
                self._cond.notify_all()

        def read(self, timeout: float = 0.1) -> bytes | None:
            with self._cond:
                if not self._queue and not self._eof:
                    self._cond.wait(timeout)
                if self._queue:
                    data = b"".join(self._queue)
                    self._queue.clear()
                    return data
                return None if self._eof else b""

        def write(self, data: bytes) -> None:
            try:
                self._pty.write(data.decode("utf-8", "replace"))
            except Exception:
                pass

        def resize(self, cols: int, rows: int) -> None:
            try:
                self._pty.set_size(cols, rows)
            except Exception:
                pass

        def alive(self) -> bool:
            try:
                return bool(self._pty.isalive())
            except Exception:
                return False

        def exit_code(self) -> int | None:
            if self.alive():
                return None
            try:
                return int(self._pty.get_exitstatus() or 0)
            except Exception:
                return 255

        def terminate(self, force: bool = False) -> None:
            try:
                import psutil  # type: ignore[import-not-found]
                proc = psutil.Process(self.pid)
                for child in proc.children(recursive=True):
                    (child.kill if force else child.terminate)()
                (proc.kill if force else proc.terminate)()
            except Exception:
                try:
                    os.kill(self.pid, 9)
                except OSError:
                    pass

        def close(self) -> None:
            try:
                del self._pty
            except Exception:
                pass

    Pty = WinPty
