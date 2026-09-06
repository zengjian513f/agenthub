"""回收站: 查看、恢复、彻底清除已删除的会话。

删除时在移入的文件旁写一份 sidecar 清单, 记录原始路径与自有元数据, 恢复
按清单放回原位。清单出现之前删除的旧条目仍可查看和清除, 恢复则按各 CLI
固有的目录规则推断原路径; 推断不出来时明确报告不可恢复, 不猜一个位置。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import adapters, session_meta

TRASH_DIR = Path.home() / ".local" / "share" / "agenthub" / "trash"
SIDECAR_SUFFIX = ".agenthub-trash.json"
MANIFEST_VERSION = 1
PROBE_CACHE_MAX = 512

_STAMP = re.compile(r"^(\d{8}-\d{6})-(.+)$")
_ROLLOUT_DAY = re.compile(r"^rollout-(\d{4})-(\d{2})-(\d{2})T")
_probe_cache: dict[str, tuple[tuple, dict]] = {}


# --------------------------------------------------------------------------
# 写入端: 由 index.delete 在移动完成后调用
# --------------------------------------------------------------------------

def manifest_path(dest: Path | str) -> Path:
    """条目的 sidecar 清单; 与条目同级, 便于整份目录被复制或备份。"""
    dest = Path(dest)
    return dest.with_name(dest.name + SIDECAR_SUFFIX)


def agents_dir(session: dict) -> Path | None:
    """Claude 子代理目录留在原处, 记下来才能在彻底清除时一并回收。"""
    if session.get("source") != "claude":
        return None
    src = Path(str(session.get("path") or ""))
    candidate = src.parent / src.stem
    return candidate if candidate.is_dir() else None


def record(dest: Path | str, session: dict, meta: dict | None = None) -> Path:
    """记录恢复所需的原始位置与元数据快照。"""
    dest = Path(dest)
    payload = {
        "version": MANIFEST_VERSION,
        "uid": str(session.get("uid") or ""),
        "source": str(session.get("source") or ""),
        "sid": str(session.get("sid") or ""),
        "title": str(session.get("title") or ""),
        "cwd": str(session.get("cwd") or ""),
        "created": str(session.get("created") or ""),
        "updated": str(session.get("updated") or ""),
        "size": int(session.get("size") or 0),
        "origin": str(session.get("path") or ""),
        "deleted_at": _iso(time.time()),
        "meta": dict(meta or {}),
    }
    agents = agents_dir(session)
    if agents:
        payload["agents_dir"] = str(agents)
    path = manifest_path(dest)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------
# 读取端
# --------------------------------------------------------------------------

def entries() -> list[dict]:
    """按删除时间倒序列出回收站条目。"""
    rows = []
    try:
        sources = sorted(p for p in TRASH_DIR.iterdir() if p.is_dir())
    except OSError:
        return rows
    for source_dir in sources:
        try:
            items = sorted(source_dir.iterdir())
        except OSError:
            continue
        for path in items:
            if path.name.endswith(SIDECAR_SUFFIX) or path.name.endswith(".tmp"):
                continue
            rows.append(_entry(source_dir.name, path))
    rows.sort(key=lambda row: row["deleted_ts"], reverse=True)
    return rows


def summary() -> dict:
    """列表加总量, 一次请求就够渲染整个回收站面板。"""
    rows = entries()
    return {"items": rows, "count": len(rows),
            "size": sum(row["size"] for row in rows), "dir": str(TRASH_DIR)}


def _entry(source: str, path: Path) -> dict:
    manifest = _read_manifest(path)
    stamp, original = _split_name(path.name)
    probe = {} if manifest else _probe(source, path)
    deleted_ts = (_epoch(manifest.get("deleted_at")) or _stamp_epoch(stamp)
                  or _mtime(path))
    origin = str(manifest.get("origin") or "") or _infer_origin(
        source, original, probe)
    taken = bool(origin) and Path(origin).exists()
    row = {
        "id": f"{source}/{path.name}",
        "source": source,
        "name": original,
        "title": (manifest.get("title") or probe.get("title") or original),
        "cwd": (manifest.get("cwd") or probe.get("cwd") or ""),
        "sid": (manifest.get("sid") or probe.get("sid") or ""),
        "uid": (str(manifest.get("uid") or "")
                or (adapters._uid(source, origin) if origin else "")),
        "updated": (manifest.get("updated") or probe.get("updated") or ""),
        "deleted_at": _iso(deleted_ts) if deleted_ts else "",
        "deleted_ts": deleted_ts,
        "size": _size(path),
        "kind": "dir" if path.is_dir() else "file",
        "origin": origin,
        "recorded": bool(manifest),
        "restorable": bool(origin) and not taken,
    }
    if not row["restorable"]:
        row["reason"] = ("原路径已存在同名会话" if taken
                         else "没有原始位置记录, 无法自动恢复")
    return row


# --------------------------------------------------------------------------
# 恢复与清除
# --------------------------------------------------------------------------

def restore(entry_id: str) -> dict:
    """把条目放回原始路径; 原路径被占用时不覆盖, 直接报错。"""
    path = _resolve(entry_id)
    source = path.parent.name
    manifest = _read_manifest(path)
    stamp, original = _split_name(path.name)
    probe = {} if manifest else _probe(source, path)
    origin = str(manifest.get("origin") or "") or _infer_origin(
        source, original, probe)
    if not origin:
        raise ValueError("没有原始位置记录, 无法自动恢复")
    dest = Path(origin)
    if dest.exists():
        raise FileExistsError(f"原路径已存在同名会话: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    _forget(path)
    manifest_path(path).unlink(missing_ok=True)
    uid = str(manifest.get("uid") or "") or adapters._uid(source, str(dest))
    # uid 由路径散列而来, 放回原位后与删除前一致, 星标等元数据可以跟着回来。
    session_meta.restore(uid, manifest.get("meta"))
    return {"path": str(dest), "uid": uid, "source": source,
            "title": (manifest.get("title") or probe.get("title") or original)}


def purge(entry_id: str) -> dict:
    """彻底删除一个条目, 连同它留在原目录的子代理残骸。"""
    path = _resolve(entry_id)
    manifest = _read_manifest(path)
    freed = _size(path)
    _remove(path)
    _forget(path)
    manifest_path(path).unlink(missing_ok=True)
    freed += _purge_orphan_agents(manifest)
    return {"removed": 1, "freed": freed}


def purge_all() -> dict:
    """清空回收站; 单条失败不中断其余条目。"""
    removed = freed = 0
    errors = []
    for row in entries():
        try:
            result = purge(row["id"])
        except (KeyError, OSError) as e:
            errors.append(f"{row['title']}: {e}")
            continue
        removed += result["removed"]
        freed += result["freed"]
    return {"removed": removed, "freed": freed, "errors": errors}


def _purge_orphan_agents(manifest: dict) -> int:
    """主会话已被彻底删除时, 同名子代理目录才算孤儿。"""
    recorded = str(manifest.get("agents_dir") or "")
    if not recorded:
        return 0
    directory = Path(recorded)
    main = directory.parent / f"{directory.name}.jsonl"
    if main.exists() or not directory.is_dir() or directory.is_symlink():
        return 0
    freed = _size(directory)
    shutil.rmtree(directory, ignore_errors=True)
    return 0 if directory.exists() else freed


def _resolve(entry_id: str) -> Path:
    """条目 id 固定为 <source>/<文件名>; 不接受任何越出回收站的路径。"""
    raw = str(entry_id or "").strip().lstrip("/")
    parts = Path(raw).parts if raw else ()
    if (len(parts) != 2 or any(part in {".", ".."} for part in parts)
            or raw.endswith(SIDECAR_SUFFIX)):
        raise KeyError(entry_id)
    target = TRASH_DIR / parts[0] / parts[1]
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise KeyError(entry_id)
    if TRASH_DIR.resolve() not in resolved.parents:
        raise KeyError(entry_id)   # 指向回收站之外的符号链接
    return target


# --------------------------------------------------------------------------
# 旧条目的原始位置推断
# --------------------------------------------------------------------------

def _infer_origin(source: str, original: str, probe: dict) -> str:
    if source == "claude":
        cwd = str(probe.get("cwd") or "")
        return str(adapters.CLAUDE_ROOT / _claude_project_dir(cwd)
                   / original) if cwd else ""
    if source == "codex":
        day = _ROLLOUT_DAY.match(original)
        return str(adapters.CODEX_ROOT.joinpath(*day.groups(),
                                                original)) if day else ""
    return ""


def _claude_project_dir(cwd: str) -> str:
    """Claude Code 把 cwd 里所有非字母数字字符换成 '-' 当项目目录名。"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _probe(source: str, path: Path) -> dict:
    """旧条目没有清单, 借 adapter 解析标题与 cwd; 结果按 mtime 缓存。"""
    ad = adapters.ADAPTERS.get(source)
    if ad is None or not hasattr(ad, "session_meta"):
        return {}
    try:
        st = path.stat()
    except OSError:
        return {}
    key, stamp = str(path), (st.st_mtime_ns, st.st_size)
    hit = _probe_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        parsed = ad.session_meta(path) or {}
    except Exception:
        parsed = {}     # 半截文件或格式变化不能让整个回收站列不出来
    row = {field: parsed.get(field) or ""
           for field in ("title", "cwd", "sid", "created", "updated")}
    if len(_probe_cache) >= PROBE_CACHE_MAX:
        _probe_cache.clear()
    _probe_cache[key] = (stamp, row)
    return row


def _forget(path: Path) -> None:
    _probe_cache.pop(str(path), None)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(manifest_path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        return {}
    return data


def _split_name(name: str) -> tuple[str, str]:
    hit = _STAMP.match(name)
    return (hit.group(1), hit.group(2)) if hit else ("", name)


def _stamp_epoch(stamp: str) -> float:
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return 0.0


def _epoch(value) -> float:
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _size(path: Path) -> int:
    try:
        if path.is_dir() and not path.is_symlink():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return path.stat().st_size
    except OSError:
        return 0


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
