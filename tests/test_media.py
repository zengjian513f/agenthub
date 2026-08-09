import tempfile
import unittest
from pathlib import Path

from sesman import media


class AttachmentDiscoveryTests(unittest.TestCase):
    def test_attachment_manifest_path_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sesman_attachments/4/截图 文件.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"not-decoded-here")
            found = media.discover(
                "正文\n\n附件1:./sesman_attachments/4/截图 文件.png", tmp)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["alt"], "附件1 · 截图 文件.png")
            self.assertTrue(found[0]["gallery"])

    def test_attachment_manifest_accepts_full_width_colon_but_not_code_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.png"
            image.write_bytes(b"image")
            self.assertEqual(len(media.discover("附件2：./image.png", tmp)), 1)
            self.assertEqual(media.discover("```text\n附件2:./image.png\n```", tmp), [])


if __name__ == "__main__":
    unittest.main()
