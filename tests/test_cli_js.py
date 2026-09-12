"""Grok 本地排队与原生 user 正文的对账，不启动浏览器。"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

CLI_JS = Path(__file__).resolve().parent.parent / "agenthub" / "static" / "cli.js"


@unittest.skipUnless(shutil.which("node"), "需要 node 才能加载 cli.js")
class GrokQueuedTextMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = r"""
const fs = require('fs');
const vm = require('vm');
const ctx = { console, globalThis: null };
ctx.globalThis = ctx;
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const grok = ctx.AGENTHUB_CLIS.grok;
const cases = JSON.parse(process.argv[2]);
const out = cases.map(([pending, native]) => grok.queuedTextMatches(pending, native));
process.stdout.write(JSON.stringify(out));
"""
        cls._script = script

    def _match(self, *pairs):
        proc = subprocess.run(
            ["node", "-e", self._script, str(CLI_JS), json.dumps(list(pairs))],
            check=True, capture_output=True, text=True, timeout=10)
        return json.loads(proc.stdout)

    def test_leftover_tui_draft_prefix_matches_web_prompt(self):
        pending = (
            "明显没有修好，tmux里的内容多很多。但是，切换到tmux再切回气泡页面后，"
            "少的内容补回来了。\n\n附件1: ./agenthub_attachments/66/image.png\n"
            "附件2: ./agenthub_attachments/66/image__1.png")
        native = "mqxj " + pending
        self.assertEqual(
            self._match(
                (pending, native),
                (pending, pending),
                ("ok", "this is okay but different"),
                ("foo", "bar foo baz"),
                ("", "mqxj"),
            ),
            [True, True, False, False, False])
