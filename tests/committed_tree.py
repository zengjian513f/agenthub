#!/usr/bin/env python3
"""在"只有已提交内容"的树上跑测试。

工作区自洽不等于提交出去的东西自洽。真出过这事：app.js 提交了、配套的
index.html 还留在工作区，本机 e2e 全绿，中央一上线整页哑掉——顶层
`$('#x').onclick` 拿到 null，异常从顶层抛出去，后面的绑定一个都没装上，
表现只是"设置对话框打不开"。同一个 checkout 有好几个会话在改的时候尤其容易。

发布前跑一遍：
    python3 tests/committed_tree.py                # 查 HEAD
    python3 tests/committed_tree.py origin/main    # 查别的提交
    python3 tests/committed_tree.py HEAD -- python3 tests/hub_e2e.py

它把指定提交单独 checkout 到一个临时 worktree 里跑，工作区一个字节都不碰。
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMMANDS = (
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-q"],
)


def git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{done.stderr.strip()}")
    return done.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ref", nargs="?", default="HEAD", help="要检查的提交，默认 HEAD")
    parser.add_argument("command", nargs="*",
                        help="要跑的命令，写在 -- 后面；不给就跑全部单测")
    args = parser.parse_args()

    commit = git("rev-parse", args.ref)
    dirty = git("status", "--porcelain")
    if dirty:
        print(f"工作区有 {len(dirty.splitlines())} 处未提交改动，它们不会进入这次检查：")
        for line in dirty.splitlines()[:10]:
            print("   ", line)
        print()

    commands = [args.command] if args.command else [list(c) for c in DEFAULT_COMMANDS]
    root = Path(tempfile.mkdtemp(prefix="agenthub-committed-"))
    tree = root / "tree"
    failures = []
    try:
        git("worktree", "add", "--quiet", "--detach", str(tree), commit)
        print(f"在 {commit[:12]} 的干净副本里跑：{tree}\n")
        for command in commands:
            print("$", " ".join(command))
            done = subprocess.run(command, cwd=str(tree))
            if done.returncode != 0:
                failures.append(" ".join(command))
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)],
                       cwd=str(PROJECT_ROOT), capture_output=True)
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print(f"\n{commit[:12]} 本身跑不过，别发布：")
        for name in failures:
            print("   ", name)
        return 1
    print(f"\n{commit[:12]} 单独拿出来也是自洽的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
