import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import adapters, index, session_meta, trash


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.claude_root = root / "claude"
        self.codex_root = root / "codex"
        self.grok_root = root / "grok"
        self.trash_dir = root / "trash"
        for directory in (self.claude_root, self.codex_root, self.grok_root):
            directory.mkdir()

        self.fresh_adapters = {
            "claude": adapters.ClaudeAdapter(),
            "codex": adapters.CodexAdapter(),
            "grok": adapters.GrokAdapter(),
        }
        self.old_state = index._state
        self.patchers = [
            patch.object(adapters, "CLAUDE_ROOT", self.claude_root),
            patch.object(adapters, "CODEX_ROOT", self.codex_root),
            patch.object(adapters, "CODEX_INDEX", root / "session_index.jsonl"),
            patch.object(adapters, "GROK_ROOT", self.grok_root),
            patch.object(adapters, "ADAPTERS", self.fresh_adapters),
            patch.object(index, "ADAPTERS", self.fresh_adapters),
            patch.object(index, "CACHE_FILE", root / "cache" / "index.json"),
            patch.object(index, "TRASH_DIR", self.trash_dir),
            patch.object(index, "CHECK_TTL", -1.0),
            patch.object(trash, "TRASH_DIR", self.trash_dir),
            patch.object(session_meta, "DATA_DIR", root / "share"),
            patch.object(session_meta, "META_FILE", root / "share" / "meta.json"),
        ]
        for patcher in self.patchers:
            patcher.start()
        index._state = index._empty_state()
        index._search_text_cache.clear()
        trash._probe_cache.clear()

    def tearDown(self):
        index._state = self.old_state
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    # ---------------- fixtures ----------------

    def claude_session(self, name: str, title: str, cwd: str = "/tmp/project") -> Path:
        project = "".join(c if c.isalnum() else "-" for c in cwd)
        path = self.claude_root / project / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "type": "user", "uuid": f"{name}-user", "parentUuid": None,
            "sessionId": name, "cwd": cwd,
            "timestamp": "2026-08-11T08:00:00Z",
            "message": {"content": [{"type": "text", "text": title}]},
        }, ensure_ascii=False) + "\n")
        return path

    def codex_session(self, sid: str, title: str) -> Path:
        name = f"rollout-2026-08-11T08-00-00-{sid}.jsonl"
        path = self.codex_root / "2026" / "08" / "11" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "session_meta", "timestamp": "2026-08-11T08:00:00Z",
             "payload": {"id": sid, "session_id": sid, "thread_source": "user",
                         "timestamp": "2026-08-11T08:00:00Z", "cwd": "/tmp/project"}},
            {"type": "response_item", "timestamp": "2026-08-11T08:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": title}]}},
        ]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                  for r in rows) + "\n")
        return path

    def delete_only_session(self) -> tuple[dict, Path]:
        session = index.load(force=True)[0]
        destination = Path(index.delete(session["uid"]))
        return session, destination

    # ---------------- 查看 ----------------

    def test_deleted_session_is_listed_with_manifest_details(self):
        source = self.claude_session("alpha", "回收站条目")
        session, destination = self.delete_only_session()

        rows = trash.entries()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], f"claude/{destination.name}")
        self.assertEqual(row["title"], "回收站条目")
        self.assertEqual(row["origin"], str(source))
        self.assertEqual(row["uid"], session["uid"])
        self.assertTrue(row["recorded"])
        self.assertTrue(row["restorable"])
        self.assertEqual(row["size"], destination.stat().st_size)

    def test_summary_totals_cover_every_source(self):
        self.claude_session("alpha", "claude 会话")
        self.codex_session("01a00000-0000-7000-8000-00000000abcd", "codex 会话")
        rows = index.load(force=True)
        for row in rows:
            index.delete(row["uid"])

        data = trash.summary()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["size"], sum(r["size"] for r in data["items"]))
        self.assertEqual({r["source"] for r in data["items"]}, {"claude", "codex"})
        self.assertEqual(data["dir"], str(self.trash_dir))

    def test_entries_ignore_manifest_sidecars(self):
        self.claude_session("alpha", "只算一条")
        self.delete_only_session()
        self.assertEqual(sorted(p.name.endswith(trash.SIDECAR_SUFFIX)
                                for p in (self.trash_dir / "claude").iterdir()),
                         [False, True])
        self.assertEqual(len(trash.entries()), 1)

    # ---------------- 恢复 ----------------

    def test_restore_puts_session_back_and_keeps_uid(self):
        source = self.claude_session("alpha", "可恢复")
        session, destination = self.delete_only_session()
        session_meta.discard(session["uid"])

        result = trash.restore(f"claude/{destination.name}")

        self.assertEqual(result["path"], str(source))
        self.assertEqual(result["uid"], session["uid"])
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        self.assertFalse(trash.manifest_path(destination).exists())
        self.assertEqual(trash.entries(), [])
        index.invalidate()
        restored = index.load()
        self.assertEqual([row["uid"] for row in restored], [session["uid"]])

    def test_restore_brings_back_star_metadata(self):
        self.claude_session("alpha", "带星标")
        session = index.load(force=True)[0]
        session_meta.set_starred(session["uid"], True)
        destination = Path(index.delete(session["uid"]))
        session_meta.discard(session["uid"])
        self.assertFalse(session_meta.snapshot(session["uid"]).get("starred"))

        trash.restore(f"claude/{destination.name}")

        self.assertTrue(session_meta.snapshot(session["uid"])["starred"])

    def test_restore_refuses_to_overwrite_a_session_at_the_origin(self):
        source = self.claude_session("alpha", "第一次")
        _, destination = self.delete_only_session()
        self.claude_session("alpha", "同名新会话")

        with self.assertRaises(FileExistsError):
            trash.restore(f"claude/{destination.name}")
        self.assertTrue(destination.exists())
        self.assertIn("第一次", destination.read_text())
        self.assertIn("同名新会话", source.read_text())
        row = trash.entries()[0]
        self.assertFalse(row["restorable"])
        self.assertEqual(row["reason"], "原路径已存在同名会话")

    def test_legacy_entry_without_manifest_infers_claude_origin(self):
        source = self.claude_session("alpha", "旧条目", cwd="/tmp/legacy.proj")
        _, destination = self.delete_only_session()
        trash.manifest_path(destination).unlink()
        trash._probe_cache.clear()

        row = trash.entries()[0]
        self.assertFalse(row["recorded"])
        self.assertEqual(row["title"], "旧条目")
        self.assertEqual(row["cwd"], "/tmp/legacy.proj")
        self.assertEqual(row["origin"], str(source))

        trash.restore(row["id"])
        self.assertTrue(source.exists())

    def test_legacy_codex_entry_falls_back_to_rollout_date_directory(self):
        source = self.codex_session("01a00000-0000-7000-8000-00000000abcd", "旧 codex")
        _, destination = self.delete_only_session()
        trash.manifest_path(destination).unlink()
        trash._probe_cache.clear()

        row = trash.entries()[0]
        self.assertEqual(row["origin"], str(source))
        trash.restore(row["id"])
        self.assertTrue(source.exists())

    def test_entry_without_any_origin_clue_is_reported_unrestorable(self):
        stray = self.trash_dir / "grok" / "20260811-080000-session"
        stray.mkdir(parents=True)
        (stray / "chat_history.jsonl").write_text("{}\n")

        row = trash.entries()[0]
        self.assertFalse(row["restorable"])
        self.assertEqual(row["reason"], "没有原始位置记录, 无法自动恢复")
        with self.assertRaises(ValueError):
            trash.restore(row["id"])
        self.assertTrue(stray.is_dir())

    # ---------------- 清除 ----------------

    def test_purge_removes_the_entry_and_its_manifest(self):
        self.claude_session("alpha", "要彻底删除")
        _, destination = self.delete_only_session()
        size = destination.stat().st_size

        result = trash.purge(f"claude/{destination.name}")

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["freed"], size)
        self.assertFalse(destination.exists())
        self.assertFalse(trash.manifest_path(destination).exists())
        self.assertEqual(trash.entries(), [])

    def test_purge_also_clears_the_orphaned_subagent_directory(self):
        source = self.claude_session("alpha", "带子代理")
        agents = source.parent / "alpha" / "subagents"
        agents.mkdir(parents=True)
        (agents / "agent-one.jsonl").write_text(json.dumps({
            "type": "user", "uuid": "agent-user", "parentUuid": None,
            "timestamp": "2026-08-11T08:00:00Z",
            "message": {"content": [{"type": "text", "text": "子代理"}]},
        }, ensure_ascii=False) + "\n")
        _, destination = self.delete_only_session()

        trash.purge(f"claude/{destination.name}")

        self.assertFalse((source.parent / "alpha").exists())

    def test_purge_keeps_subagents_of_a_session_that_came_back(self):
        source = self.claude_session("alpha", "先删后恢复")
        agents = source.parent / "alpha" / "subagents"
        agents.mkdir(parents=True)
        (agents / "agent-one.jsonl").write_text(json.dumps({
            "type": "user", "uuid": "agent-user", "parentUuid": None,
            "timestamp": "2026-08-11T08:00:00Z",
            "message": {"content": [{"type": "text", "text": "子代理"}]},
        }, ensure_ascii=False) + "\n")
        _, first = self.delete_only_session()
        trash.restore(f"claude/{first.name}")
        # 同一个会话被再次删除并放回后, 旧清单里的子代理目录仍在使用中。
        session = index.load(force=True)[0]
        second = Path(index.delete(session["uid"]))
        trash.restore(f"claude/{second.name}")
        (self.trash_dir / "claude" / "stale.jsonl").write_text("{}\n")
        trash.manifest_path(self.trash_dir / "claude" / "stale.jsonl").write_text(
            json.dumps({"version": trash.MANIFEST_VERSION,
                        "agents_dir": str(source.parent / "alpha")}))

        trash.purge("claude/stale.jsonl")

        self.assertTrue((agents / "agent-one.jsonl").exists())

    def test_purge_all_empties_the_trash(self):
        self.claude_session("alpha", "第一条")
        self.codex_session("01a00000-0000-7000-8000-00000000abcd", "第二条")
        for row in index.load(force=True):
            index.delete(row["uid"])

        result = trash.purge_all()

        self.assertEqual(result["removed"], 2)
        self.assertGreater(result["freed"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(trash.entries(), [])

    # ---------------- 边界 ----------------

    def test_entry_ids_cannot_escape_the_trash_directory(self):
        outside = Path(self.temp.name) / "keep.jsonl"
        outside.write_text("{}\n")
        link_dir = self.trash_dir / "claude"
        link_dir.mkdir(parents=True)
        (link_dir / "link.jsonl").symlink_to(outside)

        for bad in ("", "claude", "../keep.jsonl", "claude/../../keep.jsonl",
                    "/etc/passwd", "claude/missing.jsonl", "claude/link.jsonl"):
            with self.assertRaises(KeyError, msg=bad):
                trash.purge(bad)
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
