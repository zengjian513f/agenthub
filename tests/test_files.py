import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import files, server


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.image = self.cwd / "output" / "curve.png"
        self.image.parent.mkdir()
        self.image.write_bytes(b"image-fixture")
        self.messages = [
            {"role": "tool", "name": "Read", "text": json.dumps({"file_path": str(self.image)})},
            {"role": "assistant", "text": "曲线已出（`curve.png`，上图）。"},
        ]

    def resolve(self, ref):
        return files.resolve(self.messages, str(self.cwd), ref)

    def test_short_image_name_resolves_prior_tool_path(self):
        self.assertEqual(self.resolve("curve.png"), self.image)

    def test_file_lookup_uses_current_window_before_full_native_history(self):
        handler = object.__new__(server.Handler)
        view = {"uid": "claude:fixture"}
        with patch.object(server.index, "messages_for", return_value={"messages": self.messages}) as read:
            self.assertEqual(handler._file_messages(view, [str(self.image)]), self.messages)
            read.assert_called_once_with(view, windowed=True)
        with patch.object(server.index, "messages_for", return_value={"messages": self.messages}) as read:
            handler._file_messages(view, ['curve.png'])
            self.assertEqual(read.call_count, 2)  # Earlier basename collisions must be checked.
        with patch.object(server.index, "messages_for", side_effect=[
                {"messages": []}, {"messages": self.messages}]) as read:
            self.assertEqual(handler._file_messages(view, ['curve.png']), self.messages)
            self.assertEqual(read.call_count, 2)

    def test_download_streams_large_files_with_original_unicode_name(self):
        large = self.cwd / '视频.mp4'
        size = files.MAX_BYTES + 7
        with large.open('wb') as stream:
            stream.truncate(size)
        handler = object.__new__(server.Handler)
        headers, chunks = {}, []
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        class Sink:
            def write(self, chunk):
                chunks.append(len(chunk))
        handler.wfile = Sink()
        with patch.object(server.audit, 'record'):
            handler._download_file(large)
        self.assertEqual(sum(chunks), size)
        self.assertLessEqual(max(chunks), 1024 * 1024)
        self.assertIn("filename*=UTF-8''%E8%A7%86%E9%A2%91.mp4", headers['Content-Disposition'])
        self.assertEqual(headers['Content-Length'], str(size))

    def test_batch_only_returns_existing_unambiguous_session_references(self):
        self.messages.append({"role": "assistant", "text":
                              "门/筛选臂同期还在缓慢爬升，missing.png，`./output`"})
        private = self.cwd / "private.txt"
        private.write_text("unmentioned")
        requested = ["curve.png", "missing.png", "/筛选臂同期还在缓慢爬升", "./output", str(private)]
        with patch.object(files, "references", wraps=files.references) as scan:
            self.assertEqual(files.resolve_many(self.messages, str(self.cwd), requested),
                             {"curve.png": str(self.image), "./output": str(self.image.parent)})
            self.assertEqual(scan.call_count, 1)
        (self.cwd / 'curve.png').write_bytes(b'other')
        self.assertEqual(files.resolve_many(self.messages, str(self.cwd), ['curve.png']), {})
        self.image.unlink()
        self.assertEqual(files.resolve_many(self.messages, str(self.cwd), [str(self.image)]), {})

    def test_batch_route_checks_inputs_and_selected_session(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda body, status=200: replies.append((status, body))
        session = {"uid": "claude:fixture", "cwd": str(self.cwd)}
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "messages_for", return_value={"messages": self.messages}):
            handler._resolve_files({"uid": session["uid"], "refs": ["curve.png", "missing.txt"]})
            self.assertEqual(replies[-1][0], 200)
            self.assertEqual(replies[-1][1]['resolved'], {"curve.png": str(self.image)})
            self.assertEqual(replies[-1][1]['targets'], [
                {'ref': 'curve.png', 'path': str(self.image), 'kind': 'file'}])
            handler._resolve_files({'uid': session['uid'], 'refs': ['./output']})
            self.assertEqual(replies[-1][1]['targets'], [])  # Not mentioned yet.
            self.messages.append({'role': 'assistant', 'text': '目录 (`./output`)'})
            handler._resolve_files({'uid': session['uid'], 'refs': ['./output']})
            self.assertEqual(replies[-1][1]['targets'], [
                {'ref': './output', 'path': str(self.image.parent), 'kind': 'directory'}])
            for refs in (None, "curve.png", [None], ["x"] * 257, ["x" * 4097]):
                handler._resolve_files({"refs": refs})
                self.assertEqual(replies[-1][0], 400)
            handler._resolve_files({"refs": ["curve.png"], "agent": "unknown"})
            self.assertEqual(replies[-1][0], 404)
        with patch.object(server.index, "get", return_value=None):
            handler._resolve_files({"uid": "unknown", "refs": ["curve.png"]})
            self.assertEqual(replies[-1][0], 404)

    def test_duplicate_names_are_not_guessed_even_in_cwd(self):
        (self.cwd / "curve.png").write_bytes(b"unrelated")
        with self.assertRaisesRegex(ValueError, "多个同名"):
            self.resolve("curve.png")
        self.assertEqual(self.resolve(str(self.image)), self.image)

    def test_unmentioned_and_missing_paths_are_rejected(self):
        private = self.cwd / "private.txt"
        private.write_text("must not open")
        for ref in (str(private), "private.txt", "output/../private.txt", "https://example.com/private.txt"):
            with self.subTest(ref=ref), self.assertRaises(FileNotFoundError):
                self.resolve(ref)
        self.image.unlink()
        with self.assertRaises(FileNotFoundError):
            self.resolve("curve.png")

    def test_paths_spaces_unicode_markdown_and_line_suffixes(self):
        target = self.cwd / "output" / "中文 report.py"
        target.write_text("print('hello')")
        for text, ref in [
            ("[`源码`](<output/中文 report.py:12>)", "output/中文 report.py:12"),
            (f"`{target}#L12C3`", str(target) + "#L12C3"),
            (f"`{target}`", str(target)),
        ]:
            with self.subTest(text=text):
                self.messages.append({"role": "assistant", "text": text})
                self.assertEqual(self.resolve(ref), target)
        plain = self.cwd / "source.py"
        plain.write_text("pass")
        self.messages.append({"role": "assistant", "text": "源码source.py:12，目录./output。"})
        self.assertEqual(self.resolve("source.py:12"), plain)
        self.assertEqual(self.resolve("./output"), self.image.parent)
        parenthesized = self.cwd / "output" / "curve(final).png"
        parenthesized.write_bytes(b"image")
        self.messages.append({"role": "assistant", "text": '[图](output/curve(final).png "caption")'})
        self.assertEqual(self.resolve("output/curve(final).png"), parenthesized)

    def test_read_types_limits_and_directory(self):
        data, mime, headers = files.read(self.image)
        self.assertEqual((data, mime), (b"image-fixture", "image/png"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        html = self.cwd / "report.html"
        html.write_text("<script>alert(1)</script>")
        self.assertEqual(files.read(html)[1], "text/plain; charset=utf-8")
        self.assertIn("sandbox", files.read(html)[2]["Content-Security-Policy"])
        binary = self.cwd / "data.bin"
        binary.write_bytes(b"\x00\xff")
        self.assertIn("attachment", files.read(binary)[2]["Content-Disposition"])
        self.assertIn(b"curve.png", files.read(self.image.parent)[0])
        with patch.object(files, "MAX_BYTES", 2), self.assertRaises(ValueError):
            files.read(self.image)

    def test_http_route_uses_selected_view_and_rejects_unknown_session(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._send = lambda *args: replies.append(args)
        handler._json = lambda body, status=200: replies.append((status, body))
        session = {"uid": "claude:fixture", "cwd": str(self.cwd)}
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "messages_for", return_value={"messages": self.messages}):
            handler._api_get("/api/session/file", {"uid": [session["uid"]], "ref": ["curve.png"]})
            self.assertEqual(replies[-1][:3], (200, b"image-fixture", "image/png"))
            handler._api_get("/api/session/file", {"ref": ["private.txt"]})
            self.assertEqual(replies[-1][0], 404)
            handler._api_get("/api/session/file", {"agent": ["missing"], "ref": ["curve.png"]})
            self.assertEqual(replies[-1][0], 404)
        with patch.object(server.index, "get", return_value=None):
            handler._api_get("/api/session/file", {"ref": ["curve.png"]})
            self.assertEqual(replies[-1][0], 404)


if __name__ == "__main__":
    unittest.main()
