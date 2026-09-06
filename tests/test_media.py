import tempfile
import unittest
from pathlib import Path

from agenthub import media


class MediaDiscoveryTests(unittest.TestCase):
    def test_tool_output_does_not_turn_ls_image_paths_into_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "report.png"
            image.write_bytes(b"not-decoded-by-registration")
            message = {"role": "tool_result", "text": f"files:\n{image}\n"}

            media.enrich_message(message, tmp)

        self.assertNotIn("media", message)

    def test_chat_path_and_explicit_markdown_still_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "report.png"
            image.write_bytes(b"not-decoded-by-registration")
            assistant = {"role": "assistant", "text": f"结果图片：{image}"}
            tool = {"role": "tool_result", "text": f"![结果]({image})"}

            media.enrich_message(assistant, tmp)
            media.enrich_message(tool, tmp)

        self.assertEqual(len(assistant["media"]), 1)
        self.assertTrue(assistant["media"][0]["gallery"])
        self.assertEqual(len(tool["media"]), 1)
        self.assertEqual(tool["media"][0]["ref"], str(image))


if __name__ == "__main__":
    unittest.main()
