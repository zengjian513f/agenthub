"""命令行入口。

  python -m agenthub.host run --name N --cwd DIR --cols C --rows R [--meta JSON] -- CMD...
  python -m agenthub.host list
  python -m agenthub.host attach NAME        # 手工接管 (POSIX 原始终端直通)
  python -m agenthub.host kill NAME [--force]
  python -m agenthub.host send NAME TEXT
  python -m agenthub.host capture NAME [--lines N] [--plain]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import client


def _cmd_run(args) -> int:
    from . import session
    meta = json.loads(args.meta) if args.meta else {}
    if not args.command:
        print("缺少命令", file=sys.stderr)
        return 2
    directory = Path(args.dir) if args.dir else client.host_dir()
    return session.run(args.name, list(args.command), args.cwd, args.cols, args.rows,
                       meta, directory, history=args.history)


def _cmd_list(args) -> int:
    rows = client.list_sessions(Path(args.dir) if args.dir else None)
    for row in rows:
        print(f"{row['name']}\tpid={row['pid']}\t{row['cols']}x{row['rows']}\t"
              f"{'attached' if row['attached'] else 'detached'}\t{row['cwd']}")
    return 0


def _cmd_kill(args) -> int:
    client.request(args.name, "kill", force=args.force)
    return 0


def _cmd_send(args) -> int:
    client.request(args.name, "paste" if args.enter else "send", text=args.text)
    if args.enter:
        client.request(args.name, "keys", keys=["Enter"])
    return 0


def _cmd_capture(args) -> int:
    reply = client.request(args.name, "capture", kind="scrollback" if args.lines else "screen",
                           lines=args.lines, styled=not args.plain, join=args.join)
    sys.stdout.write(reply["text"] + "\n")
    x, y = reply["cursor"]
    print(f"-- cursor {x},{y}{' alt' if reply.get('alt') else ''}", file=sys.stderr)
    return 0


def _cmd_attach(args) -> int:
    if sys.platform == "win32":
        print("Windows 下暂不支持命令行 attach, 请用网页控制台", file=sys.stderr)
        return 2
    import select
    import shutil
    import termios
    import tty

    cols, rows = shutil.get_terminal_size((120, 32))
    att = client.Attach(args.name, cols, rows)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    print(f"[agenthub] attached to {args.name}; 按 Ctrl-\\ 退出 (不影响会话)",
          file=sys.stderr)
    try:
        tty.setraw(fd)
        while att.alive():
            r, _, _ = select.select([fd, att.sock], [], [], 0.2)
            if fd in r:
                data = os.read(fd, 4096)
                if b"\x1c" in data:
                    break
                att.write(data)
            data = att.read(0.0 if att.sock in r else 0.0)
            if data:
                os.write(1, data)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        att.close()
        print("\r\n[agenthub] detached", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agenthub.host")
    ap.add_argument("--dir", help="会话目录 (默认 ~/.local/share/agenthub/host)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run")
    run.add_argument("--name", required=True)
    run.add_argument("--cwd")
    run.add_argument("--cols", type=int, default=120)
    run.add_argument("--rows", type=int, default=32)
    run.add_argument("--meta")
    run.add_argument("--history", type=int, default=10000)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=_cmd_run)

    sub.add_parser("list").set_defaults(func=_cmd_list)

    kill = sub.add_parser("kill")
    kill.add_argument("name")
    kill.add_argument("--force", action="store_true")
    kill.set_defaults(func=_cmd_kill)

    send = sub.add_parser("send")
    send.add_argument("name")
    send.add_argument("text")
    send.add_argument("--enter", action="store_true")
    send.set_defaults(func=_cmd_send)

    cap = sub.add_parser("capture")
    cap.add_argument("name")
    cap.add_argument("--lines", type=int, default=0)
    cap.add_argument("--plain", action="store_true")
    cap.add_argument("--join", action="store_true")
    cap.set_defaults(func=_cmd_capture)

    att = sub.add_parser("attach")
    att.add_argument("name")
    att.set_defaults(func=_cmd_attach)

    args = ap.parse_args(argv)
    if args.cmd == "run" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
