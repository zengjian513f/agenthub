"""前端的几条硬约定，不跑浏览器也能验。

这些都是被生产教过的：
- app.js 顶层 `$('#x').onclick = ...`，元素不在页面里就是对 null 赋值，
  异常从顶层抛出去，**后面所有绑定全部不再执行**。中央 Hub 因此整页哑掉过，
  表现只是"设置对话框打不开"，看不出跟某次发布有关。
- 带 `?v=__AGENTHUB_ASSET_VERSION__` 的文件如果没算进 ASSET_VERSION，
  改了它浏览器照样用缓存里的旧版本，而且各机器 build 号还一致，查不出来。
- 静态文件的字节直接进 ASSET_VERSION，行尾被改写过的 checkout 会算出另一个
  build，跟别的机器永远对不上（Windows 节点上真发生过）。
"""
import ast
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = PROJECT_ROOT / "agenthub" / "static"
SERVER = PROJECT_ROOT / "agenthub" / "server.py"

CACHE_BUSTED = re.compile(r'(?:src|href)="([^"?]+)\?v=__AGENTHUB_ASSET_VERSION__"')
LOCAL_SCRIPT = re.compile(r'<script[^>]*\ssrc="([^"?]+)(?:\?[^"]*)?"')
ELEMENT_ID = re.compile(r'\sid="([^"]+)"')
# 顶层语句：行首就是代码，没有缩进。函数体里的 $('#x') 可能指向 JS 自己插进去
# 的节点，不能一概要求它在 index.html 里。
TOP_LEVEL_LOOKUP = re.compile(
    r"""^(?:\$|document\.querySelector)\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""")
TOP_LEVEL_BY_ID = re.compile(
    r"""^document\.getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]\s*\)""")


def asset_version_names() -> list[str]:
    """从 server.py 里把 ASSET_VERSION 的文件清单抠出来。"""
    tree = ast.parse(SERVER.read_text())
    for node in tree.body:
        targets = getattr(node, "targets", [])
        if not (targets and isinstance(targets[0], ast.Name)
                and targets[0].id == "ASSET_VERSION"):
            continue
        return [n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    raise AssertionError("server.py 里找不到 ASSET_VERSION")


class BuildHashTests(unittest.TestCase):
    def test_every_cache_busted_file_is_part_of_the_build_hash(self):
        """带 ?v= 的文件没算进哈希，等于给自己发了个永不失效的缓存。"""
        covered = set(asset_version_names())
        missing = []
        for page in sorted(STATIC.glob("*.html")):
            for name in CACHE_BUSTED.findall(page.read_text()):
                if name not in covered:
                    missing.append(f"{page.name} 引用的 {name}")
        self.assertEqual(missing, [], "这些文件参与缓存失效却没进 ASSET_VERSION，"
                         "改了它们 build 号不变、浏览器继续用旧的：\n"
                         + "\n".join(sorted(set(missing))))

    def test_the_build_hash_only_lists_files_that_exist(self):
        """清单里留了个不存在的文件名，服务导入期就 FileNotFoundError。"""
        missing = [name for name in asset_version_names() if not (STATIC / name).is_file()]
        self.assertEqual(missing, [], f"ASSET_VERSION 里这些文件不在 {STATIC}")

    def test_static_text_is_stored_with_unix_line_endings(self):
        """ASSET_VERSION 按字节算，行尾一变 build 就跟别的机器对不上。"""
        offenders = []
        for path in sorted(STATIC.rglob("*")):
            if not path.is_file() or path.suffix not in (
                    ".js", ".css", ".html", ".json", ".webmanifest", ".svg"):
                continue
            data = path.read_bytes()
            if b"\r\n" in data:
                offenders.append(f"{path.relative_to(STATIC)} 有 CRLF")
            if data.startswith(b"\xef\xbb\xbf"):
                offenders.append(f"{path.relative_to(STATIC)} 有 BOM")
        self.assertEqual(offenders, [], "\n".join(offenders))


class TopLevelDomTests(unittest.TestCase):
    """页面和它顶层脚本之间的契约。"""

    def pages(self):
        for page in sorted(STATIC.glob("*.html")):
            text = page.read_text()
            scripts = [STATIC / name for name in LOCAL_SCRIPT.findall(text)
                       if not name.startswith(("vendor/", "http"))]
            yield page, set(ELEMENT_ID.findall(text)), [s for s in scripts if s.is_file()]

    def test_top_level_lookups_find_an_element_in_the_page(self):
        """顶层对 null 赋值会打断整支脚本，后面的事件绑定一个都不会装上。"""
        offenders = []
        for page, ids, scripts in self.pages():
            for script in scripts:
                for number, line in enumerate(script.read_text().splitlines(), 1):
                    found = TOP_LEVEL_LOOKUP.match(line) or TOP_LEVEL_BY_ID.match(line)
                    if found and found.group(1) not in ids:
                        offenders.append(
                            f"{script.name}:{number} 取 #{found.group(1)}，"
                            f"但 {page.name} 里没有这个元素")
        self.assertEqual(offenders, [], "顶层拿不到元素就是 null，"
                         "整支脚本会在这里断掉：\n" + "\n".join(offenders))

    def test_the_check_would_notice_a_missing_element(self):
        """这条检查要是连合成的坏例子都认不出来，那它就只是一直绿而已。"""
        self.assertEqual(TOP_LEVEL_LOOKUP.match("$('#gone').onclick = f;").group(1), "gone")
        self.assertEqual(
            TOP_LEVEL_BY_ID.match("document.getElementById('gone').hidden = true;").group(1),
            "gone")
        self.assertIsNone(TOP_LEVEL_LOOKUP.match("  $('#inside-a-function')"),
                          "缩进的行是函数体，不该管")


class PaneVisibilityTests(unittest.TestCase):
    """`hidden` 属性靠浏览器默认样式的 display:none，作者样式一压就没了。

    实测（Chromium 151）：`.x { display: flex }` 配 `hidden` 属性，算出来是
    flex —— UA 那条规则没有 !important，作者样式赢。设置面板就这么出过事，
    切到"机器"那一页，"外观"整页仍堆在上面；而 e2e 只断言机器行渲染出来了，
    照样全绿。style.css 里已经有十几条 `X[hidden] { display: none }` 的单点
    补丁，说明这个坑反复踩，所以这里把它变成一条规则。
    """

    @staticmethod
    def parse_rules(css: str) -> tuple[set[str], set[str]]:
        """返回 (设了 display 的选择器, 有 [hidden] 兜底的选择器)。"""
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        with_display: set[str] = set()
        with_hidden_rule: set[str] = set()
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector, body = match.group(1).strip(), match.group(2)
            for part in (s.strip() for s in selector.split(",")):
                if not part or part.startswith("@"):
                    continue
                if part.endswith("[hidden]"):
                    with_hidden_rule.add(part[:-len("[hidden]")].strip())
                elif re.fullmatch(r"[#.][A-Za-z0-9_-]+", part) \
                        and re.search(r"(?<![\w-])display\s*:", body):
                    with_display.add(part)
        return with_display, with_hidden_rule

    @staticmethod
    def hidden_elements_in(html: str):
        """静态就带 hidden 属性的元素，连同它的 id 和 class 选择器。"""
        for tag in re.findall(r"<[a-zA-Z][^>]*>", html):
            if not re.search(r"\shidden(?=[\s>/])", tag):
                continue
            found = re.search(r'\sid="([^"]+)"', tag)
            names = [f"#{found.group(1)}"] if found else []
            classes = re.search(r'\sclass="([^"]*)"', tag)
            names += [f".{c}" for c in (classes.group(1).split() if classes else [])]
            yield tag[:70], names

    @classmethod
    def offenders_for(cls, css: str, pages: dict[str, str]) -> list[str]:
        with_display, with_hidden_rule = cls.parse_rules(css)
        offenders = []
        for name, html in pages.items():
            for tag, names in cls.hidden_elements_in(html):
                pressing = [n for n in names if n in with_display]
                if not pressing or any(n in with_hidden_rule for n in names):
                    continue
                offenders.append(f"{name} {tag}\n    {pressing} 设了 display，"
                                 f"却没有任何一条 {pressing[0]}[hidden] 兜底")
        return offenders

    def test_a_hidden_element_is_not_overridden_by_a_display_rule(self):
        pages = {p.name: p.read_text() for p in sorted(STATIC.glob("*.html"))}
        offenders = self.offenders_for((STATIC / "style.css").read_text(), pages)
        self.assertEqual(offenders, [], "这些元素设了 hidden 也不会消失：\n"
                         + "\n".join(offenders))

    def test_the_check_knows_a_real_override_from_a_guarded_one(self):
        """判据要是认不出合成的坏例子，这条就只是一直绿。"""
        bad = self.offenders_for(".pane { display: flex; }",
                                 {"t.html": '<div class="pane" hidden></div>'})
        self.assertEqual(len(bad), 1, bad)

        by_class = self.offenders_for(".pane { display: flex; }\n.pane[hidden] { display: none; }",
                                      {"t.html": '<div class="pane" hidden></div>'})
        self.assertEqual(by_class, [], "类上有兜底就不该报")

        by_id = self.offenders_for(".pane { display: flex; }\n#toast[hidden] { display: none; }",
                                   {"t.html": '<div id="toast" class="pane" hidden></div>'})
        self.assertEqual(by_id, [], "兜底挂在 id 上同样算数")

        untouched = self.offenders_for(".pane { color: red; }",
                                       {"t.html": '<div class="pane" hidden></div>'})
        self.assertEqual(untouched, [], "没设 display 就压不住 hidden，不该报")


if __name__ == "__main__":
    unittest.main()
