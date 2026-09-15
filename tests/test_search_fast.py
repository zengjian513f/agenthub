"""搜索快路径(折叠前置过滤 + 内存正文缓存)必须与逐会话全量正则逐字节等价。"""
import json
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _sre

from agenthub import adapters, index, session_meta


def reference_search(query, sources=None, limit=60, word=False, case=False,
                     regex=False, progress=None, matches=None):
    """优化前的 index.search 原样复制: 每个会话都跑 pat.finditer(正文)。"""
    if not query.strip():
        return {"results": [], "truncated": False, "total_pool": 0, "scanned": 0}
    pat = index.build_pattern(query, word, case, regex)
    hits, truncated, scanned = [], False, 0
    pool = [s for s in index.load() if not sources or s["source"] in sources]
    if progress:
        progress(0, len(pool))
    for done, s in enumerate(pool, 1):
        if len(hits) >= limit:
            truncated = True
            break
        snippet, count, capped = None, 0, False
        hay = index._search_text(s)
        for m in pat.finditer(hay):
            count += 1
            if snippet is None:
                lo, hi = max(0, m.start() - 40), m.end() + 150
                snippet = " ".join(index._ANSI_T.sub("", hay[lo:hi]).split())
            if count >= index.HIT_CAP:
                capped = True
                break
        if count:
            hits.append({**s, "hits": count, "hits_capped": capped, "snippet": snippet})
            if matches:
                matches([hits[-1]])
        scanned = done
        if progress:
            progress(done, len(pool))
    hits.sort(key=lambda x: x["updated"], reverse=True)
    return {"results": hits, "truncated": truncated, "total_pool": len(pool), "scanned": scanned}


NASTY = ("guard Guard GUARD ddp_guard ddp_Guard İstanbul ıi Iİ ſs Sſ K k Kelvin "
         "Σίσυφος ΣΑΣ σας ς σ Straße STRASSE ẞ ß µs μs 文件管理 管理 agenthub rust "
         "Agenthub-Rust \x1b[31mred\x1b[0m #tag a#a ba#a#a x_y x.y 12345 \n\t ")
ALPHABET = list("guardGUARDİıiIſsSKkΣσςßẞµμ文件管理 _#.\n-" + "Kſ")


class FoldTests(unittest.TestCase):
    def test_fold_is_length_preserving_and_agrees_with_sre_case_folding(self):
        self.assertIsNotNone(index._FOLD_TABLE)
        tolower = _sre.unicode_tolower
        extra = __import__("re._casefix", fromlist=["_EXTRA_CASES"])._EXTRA_CASES
        for code in range(sys.maxunicode + 1):
            folded = index._fold(chr(code))
            self.assertEqual(len(folded), 1, hex(code))
            self.assertEqual(folded, index._fold(chr(tolower(code))), hex(code))
        for key, values in extra.items():
            for value in values:
                self.assertEqual(index._fold(chr(key)), index._fold(chr(value)), (key, value))
        self.assertEqual(index._fold("ΣΑΣ"), index._fold("σας"))
        self.assertEqual(index._fold("İ"), "i")

    def test_scan_literal_matches_finditer_on_random_unicode(self):
        rng = random.Random(20260915)
        small = list("aA#İıiIſsSKkΣσςß_ \u212a")
        # 自重叠查询 + 前一次边界失败后, 真正的命中就从 p+1 开始; 折叠的假阳性
        # (ẞ/ß 不对称) 也必须由原模式在原文上否决。
        directed = [("ba#a#a", "a#a"), ("xİ#i#ı", "i#i"), ("aK kK k", "k k"), ("ẞßß", "ß"),
                    ("ßẞẞ", "ẞ"), ("ſs ſs", "s"), ("ΣΑΣ σας", "ς"), ("a a a", "a a"),
                    ("İİİ", "i"), ("x.y x.y", "x.y"), ("a#a#a#a", "a#a")]
        samples = [(hay, query) for hay, query in directed]
        for _ in range(600):
            alphabet = ALPHABET if rng.random() < 0.5 else small
            samples.append(("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60))),
                            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 3)))))
        for hay, query in samples:
            for word in (False, True):
                for case in (False, True):
                    pat = index.build_pattern(query, word=word, case=case)
                    expected = [m.span() for m in pat.finditer(hay)]
                    got = [m.span() for m in index._scan_literal(
                        pat, hay, index._fold(hay), index._fold(query))]
                    self.assertEqual(got, expected, (hay, query, word, case))

    def test_required_literals_are_necessary_for_regex_matches(self):
        rng = random.Random(7)
        atoms = ["guard", "İ", "ſ", "σ", "K", ".*", "\\w+", "(?:a|b)", "[gG]u", "x?", "\\bK",
                 "(?i)s", "文件", "#"]
        for _ in range(300):
            query = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 4)))
            hay = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 40)))
            for case in (False, True):
                try:
                    pat = index.build_pattern(query, case=case, regex=True)
                except re.error:
                    continue
                needles = index._required_literals(query, regex=True)
                folded = index._fold(hay)
                if pat.search(hay):
                    for needle in needles:
                        self.assertIn(needle, folded, (query, hay, case))
        self.assertEqual(index._required_literals("agenthub.*rust", True), ["agenthub", "rust"])
        self.assertEqual(index._required_literals("(a|b)+", True), [])
        self.assertEqual(index._required_literals("ddp_Guard", False), ["ddp_guard"])


class SessionMetaReadCacheTests(unittest.TestCase):
    def test_read_reuses_parse_until_file_identity_changes(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            meta = session_meta.META_FILE
            write = lambda rows: meta.write_text(json.dumps({"version": 1, "sessions": rows}))
            write({"a": {"starred": True}})
            self.assertEqual(session_meta._read(), {"a": {"starred": True}})
            with patch.object(session_meta, "_read_file",
                              side_effect=AssertionError("unchanged file must not be re-read")):
                self.assertEqual(session_meta._read(), {"a": {"starred": True}})
            # Same inode and size, only the content (and mtime) differ.
            write({"b": {"starred": True}})
            future = meta.stat().st_mtime_ns + 5_000_000
            import os
            os.utime(meta, ns=(future, future))
            self.assertEqual(session_meta._read(), {"b": {"starred": True}})
            # Callers get their own top-level dict.
            session_meta._read()["b"] = None
            self.assertEqual(session_meta._read(), {"b": {"starred": True}})
            meta.unlink()
            self.assertEqual(session_meta._read(), {})


class SearchFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.claude_root = root / "claude"
        self.codex_root = root / "codex"
        self.grok_root = root / "grok"
        self.codex_index = root / "session_index.jsonl"
        self.cache_file = root / "cache" / "index.json"
        self.meta_file = root / "share" / "session-meta.json"
        for directory in (self.claude_root, self.codex_root, self.grok_root):
            directory.mkdir()
        self.fresh_adapters = {
            "claude": adapters.ClaudeAdapter(),
            "codex": adapters.CodexAdapter(),
            "grok": adapters.GrokAdapter(),
        }
        self.old_state = index._state
        self.old_cursor_cache = index._cursor_cache
        self.patchers = [
            patch.object(adapters, "CLAUDE_ROOT", self.claude_root),
            patch.object(adapters, "CODEX_ROOT", self.codex_root),
            patch.object(adapters, "CODEX_INDEX", self.codex_index),
            patch.object(adapters, "GROK_ROOT", self.grok_root),
            patch.object(index, "ADAPTERS", self.fresh_adapters),
            patch.object(index, "CACHE_FILE", self.cache_file),
            patch.object(index, "TRASH_DIR", root / "trash"),
            patch.object(index, "CHECK_TTL", -1.0),
            patch.object(session_meta, "META_FILE", self.meta_file),
            patch.object(session_meta, "DATA_DIR", self.meta_file.parent),
        ]
        for patcher in self.patchers:
            patcher.start()
        index._state = index._empty_state()
        index._cursor_cache = {}
        index._search_cache_clear()
        index._history_memo.clear()

    def tearDown(self):
        index._state = self.old_state
        index._cursor_cache = self.old_cursor_cache
        index._search_cache_clear()
        index._history_memo.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def write_rows(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")

    def claude_session(self, name, texts, stamp="2026-08-11T08:00:00Z"):
        path = self.claude_root / "-tmp-project" / f"{name}.jsonl"
        rows, parent = [], None
        for i, text in enumerate(texts):
            role = "user" if i % 2 == 0 else "assistant"
            rows.append({"type": role, "uuid": f"{name}-{i}", "parentUuid": parent,
                         "sessionId": name, "cwd": "/tmp/project", "timestamp": stamp,
                         "message": {"content": [{"type": "text", "text": text}]}})
            parent = f"{name}-{i}"
        self.write_rows(path, rows)
        return path

    def codex_session(self, sid, texts, parent="", cutoff=0, stamp="2026-08-11T08:00:00Z"):
        path = self.codex_root / "2026" / "08" / "11" / f"rollout-{sid}.jsonl"
        payload = {"id": sid, "session_id": sid, "thread_source": "user",
                   "timestamp": stamp, "cwd": "/tmp/project"}
        if parent:
            payload["forked_from_id"] = parent
            payload["history_base"] = {"thread_id": parent, "end_byte_offset": cutoff}
        rows = [{"type": "session_meta", "timestamp": stamp, "payload": payload}]
        for i, text in enumerate(texts):
            role = "user" if i % 2 == 0 else "assistant"
            kind = "input_text" if role == "user" else "output_text"
            rows.append({"type": "response_item", "timestamp": stamp,
                         "payload": {"type": "message", "role": role,
                                     "content": [{"type": kind, "text": text}]}})
        self.write_rows(path, rows)
        return path

    def append_claude(self, path, name, text, parent_index):
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "user", "uuid": f"{name}-extra-{parent_index}",
                                 "parentUuid": f"{name}-{parent_index}", "sessionId": name,
                                 "timestamp": "2026-08-11T09:00:00Z",
                                 "message": {"content": [{"type": "text", "text": text}]}},
                                ensure_ascii=False) + "\n")

    def append_codex(self, path, text):
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}}, ensure_ascii=False) + "\n")


class EquivalenceTests(SearchFixture):
    def populate(self):
        rng = random.Random(1)
        for i in range(6):
            words = [rng.choice(NASTY.split(" ")) for _ in range(rng.randint(3, 12))]
            texts = [" ".join(words[j:j + 3]) for j in range(0, len(words), 3)]
            if i % 2:
                self.claude_session(f"claude-{i}", texts, stamp=f"2026-08-11T0{i}:00:00Z")
            else:
                self.codex_session(f"codex-{i}", texts, stamp=f"2026-08-11T0{i}:00:00Z")
        self.claude_session("nasty", [NASTY, NASTY[::-1], "guard " * 250], stamp="2026-08-11T07:30:00Z")
        self.codex_session("nasty-codex", ["a#a ba#a#a", "İstanbul ıi", "Σίσυφος ΣΑΣ σας"],
                           stamp="2026-08-11T07:40:00Z")

    def test_search_matches_reference_for_every_mode(self):
        self.populate()
        queries = ["guard", "Guard", "ddp_guard", "İ", "ı", "i", "ſ", "s", "K", "k", "σ", "ς", "Σ",
                   "ß", "ẞ", "µ", "文件", "a#a", "#a", "x.y", "red", "zzzz_no_such_term_qq",
                   "agenthub.*rust", "gu.rd", "(?i)K", "a|b", "\\bguard\\b", "[Gg]uard", "^guard",
                   "文件管理", " ", "\\w+"]
        modes = [dict(), dict(word=True), dict(case=True), dict(regex=True),
                 dict(word=True, case=True), dict(regex=True, word=True),
                 dict(regex=True, case=True)]
        compared = 0
        for query in queries:
            for mode in modes:
                for sources in (None, ["codex"], ["claude", "grok"]):
                    for limit in (60, 2):
                        want_batches, want_progress = [], []
                        try:
                            expected = reference_search(
                                query, sources, limit, **mode,
                                progress=lambda d, t: want_progress.append((d, t)),
                                matches=want_batches.extend)
                        except re.error:
                            with self.assertRaises(re.error):
                                index.search(query, sources, limit, **mode)
                            continue
                        got_batches, got_progress = [], []
                        got = index.search(query, sources, limit, **mode,
                                           progress=lambda d, t: got_progress.append((d, t)),
                                           matches=got_batches.extend)
                        self.assertEqual(json.dumps(got, sort_keys=True),
                                         json.dumps(expected, sort_keys=True),
                                         (query, mode, sources, limit))
                        self.assertEqual(got_batches, want_batches, (query, mode))
                        self.assertEqual(got_progress, want_progress, (query, mode))
                        compared += 1
        self.assertGreater(compared, 500)
        self.assertTrue(any(r["hits_capped"] for r in index.search("guard")["results"]))

    def test_second_search_skips_stamps_until_disk_changes(self):
        codex = self.codex_session("skip-codex", ["first needle"])
        claude = self.claude_session("skip-claude", ["first needle"])
        self.assertEqual(len(index.search("needle")["results"]), 2)
        with patch.object(index, "_window_cache_stamp",
                          side_effect=AssertionError("unchanged disk must not re-stamp")):
            self.assertEqual(len(index.search("needle")["results"]), 2)
            self.assertEqual(index.search("zzzz")["results"], [])
        self.append_codex(codex, "second needle")
        rows = index.search("second")["results"]
        self.assertEqual([r["path"] for r in rows], [str(codex)])
        self.assertEqual(rows[0]["hits"], 1)
        self.append_claude(claude, "skip-claude", "third needle", 0)
        rows = index.search("third")["results"]
        self.assertEqual([r["path"] for r in rows], [str(claude)])
        self.assertEqual(index.search("needle")["results"][0]["hits"], 2)

    def test_sessions_do_not_share_or_leak_cached_text(self):
        one = self.codex_session("one", ["alpha only"])
        two = self.codex_session("two", ["beta only"])
        rows = {r["path"]: r for r in index.load(force=True)}
        text_one = index._search_text(rows[str(one)])
        text_two = index._search_text(rows[str(two)])
        self.assertIn("alpha", text_one)
        self.assertNotIn("beta", text_one)
        self.assertIn("beta", text_two)
        self.assertNotIn("alpha", text_two)
        self.append_codex(one, "alpha again")
        rows = {r["path"]: r for r in index.load()}
        self.assertIn("alpha again", index._search_text(rows[str(one)]))
        self.assertIs(index._search_text(rows[str(two)]), text_two)
        self.assertEqual(len(index._search_text_cache), 2)

    def test_claude_timeline_change_reprojects_without_file_change(self):
        path = self.claude_session("rewind", ["root", "reply", "later reply"])
        row = index.load(force=True)[0]
        self.assertIn("later reply", index.search("later")["results"][0]["snippet"])
        session_meta.begin_timeline_rewind(row["uid"], "rewind-2", path.stat().st_size)
        session_meta.finish_timeline_rewind(row["uid"], "rewind-1")
        self.assertEqual(index.search("later")["results"], [])
        self.assertEqual(index.search("reply")["results"][0]["hits"], 1)

    def test_memory_cache_is_bounded_by_real_bytes(self):
        paths = [self.codex_session(f"big-{i}", ["x" * 20000 + f" needle{i}"]) for i in range(6)]
        rows = index.load(force=True)
        one_entry = sys.getsizeof("x" * 20000) * 2
        with patch.object(index, "SEARCH_CACHE_MEMORY_BYTES", int(one_entry * 3.5)):
            for row in rows:
                index._search_text(row)
            self.assertEqual(len(index._search_text_cache), 3)
            self.assertLessEqual(index._search_cache_bytes, int(one_entry * 3.5))
            self.assertEqual(index._search_cache_bytes, sum(
                index._search_cache_entry_bytes(e) for e in index._search_text_cache.values()))
            kept = {uid for uid in index._search_text_cache}
            self.assertEqual(kept, {row["uid"] for row in rows[-3:]})
            # Evicted entries are re-read from the disk cache and still search correctly.
            for i in range(6):
                self.assertEqual(len(index.search(f"needle{i}")["results"]), 1)
        self.assertTrue(all(p.exists() for p in paths))

    def test_codex_inheritance_chain_is_seeded_from_disk_and_revalidated(self):
        parent = self.codex_session("chain-parent", ["parentneedle"])
        child = self.codex_session("chain-child", ["child text"], parent="chain-parent",
                                   cutoff=parent.stat().st_size)
        self.assertEqual({r["path"] for r in index.search("parentneedle")["results"]},
                         {str(parent), str(child)})
        # Restart: memory caches gone, only the disk cache (with its stamp) remains.
        index._search_cache_clear()
        index._history_memo.clear()
        ad = self.fresh_adapters["codex"]
        with patch.object(ad, "_history_segments",
                          side_effect=AssertionError("cold start must reuse the saved chain")):
            rows = index.search("parentneedle")["results"]
        self.assertEqual({r["path"] for r in rows}, {str(parent), str(child)})
        self.assertIn(str(child), index._history_memo)
        # A rewritten parent invalidates the seeded chain and the inherited text.
        parent.write_text(parent.read_text().replace("parentneedle", "parentupdate"))
        with patch.object(ad, "_history_segments", wraps=ad._history_segments) as segments:
            rows = index.search("parentupdate")["results"]
        self.assertEqual({r["path"] for r in rows}, {str(parent), str(child)})
        self.assertEqual(index.search("parentneedle")["results"], [])
        self.assertGreaterEqual(segments.call_count, 1)

    def test_deleted_session_leaves_cache_accounting_consistent(self):
        self.codex_session("gone", ["needle"])
        self.codex_session("stays", ["needle"])
        rows = index.load(force=True)
        for row in rows:
            index._search_text(row)
        gone = next(r for r in rows if "gone" in r["path"])
        with patch.object(index.trash, "record"):
            index.delete(gone["uid"])
        self.assertNotIn(gone["uid"], index._search_text_cache)
        self.assertEqual(index._search_cache_bytes, sum(
            index._search_cache_entry_bytes(e) for e in index._search_text_cache.values()))


if __name__ == "__main__":
    unittest.main()
