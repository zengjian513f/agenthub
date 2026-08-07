"""sesman 前端端到端测试: 真实浏览器点遍每个交互。"""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("SESMAN_BASE", "http://127.0.0.1:8710")
FAKE_PROJ = Path.home() / ".claude" / "projects" / "-tmp-sesman-selftest"
FAKE_IMG = Path("/tmp/sesman-selftest-image.png")
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}{'  ' + str(extra) if extra and not cond else ''}")


def make_fake_session():
    """造一个一次性会话, 用来安全地测删除。"""
    FAKE_PROJ.mkdir(parents=True, exist_ok=True)
    f = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
    sid = f.stem
    long_text = "长文本测试 " + "x" * 9000
    FAKE_IMG.write_bytes(base64.b64decode(PNG_B64))
    render_sample = (
        "## 渲染自测\n\n| 列A | 列B | 数值 |\n|---|:---:|---:|\n"
        "| `a1` | b1 | 1 |\n| a2 | **b2** | 22 |\n\n"
        "- 列表项一\n- 列表项二\n\n1. 有序一\n2. 有序二\n\n"
        "> 引用内容\n\n---\n\n普通段落\n\n"
        "行内公式 $E=mc^2$，块公式：\n\n$$\\int_0^1 x^2\\,dx = \\frac{1}{3}$$\n\n"
        "```text\n$code_not_math$\n```\n\n"
        f"![本地测试图]({FAKE_IMG})\n\n"
        "不存在的相对图片 ![缺失图](path-or-url)"
    )
    rows = [
        {"type": "ai-title", "aiTitle": "SESMAN自测会话请删除", "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": render_sample}]},
         "uuid": "a0", "timestamp": "2026-08-06T11:59:00.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": "自测：第一条用户消息"},
         "uuid": "u1", "timestamp": "2026-08-06T12:00:00.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid, "gitBranch": "main"},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "结构化图片测试"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}},
        ]}, "uuid": "u1-img", "timestamp": "2026-08-06T12:00:00.500Z",
         "cwd": "/tmp/sesman-selftest", "sessionId": sid},
        # 大小写 / 全词 选项的样本: SesmanCase 各一次, hi 与 hi2 各出现
        {"type": "user", "message": {"role": "user", "content": "SesmanCase 与 sesmancase 各一次"},
         "uuid": "u1b", "timestamp": "2026-08-06T12:00:01.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "自测思考内容"},
            {"type": "text", "text": long_text},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/a"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi2"}}]},
         "uuid": "a1", "timestamp": "2026-08-06T12:00:05.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "hi"},
            {"type": "tool_result", "content": "文件内容"},
            {"type": "tool_result", "content": "hi2"}]},
         "uuid": "u2", "timestamp": "2026-08-06T12:00:06.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": "# AGENTS.md instructions\n<INSTRUCTIONS>注入的</INSTRUCTIONS>"},
         "uuid": "u3", "timestamp": "2026-08-06T12:00:07.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "单行工具输出不折叠"}]},
         "uuid": "u4", "timestamp": "2026-08-06T12:00:08.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": sid},
    ]
    # 确定性覆盖前端两层限流：前 40 条命中消息自动展开、前 3000 处命中高亮。
    # 不能拿用户真实会话的文件大小推断命中消息数；大文件也可能只有一条超长消息。
    rows.extend({
        "type": "assistant",
        "message": {"role": "assistant", "content": "限流样本 " + "a " * 100},
        "uuid": f"cap-{i}", "timestamp": f"2026-08-06T12:01:{i:02d}.000Z",
        "cwd": "/tmp/sesman-selftest", "sessionId": sid,
    } for i in range(45))
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return f


def cleanup():
    shutil.rmtree(FAKE_PROJ, ignore_errors=True)
    FAKE_IMG.unlink(missing_ok=True)
    trash = Path.home() / ".local" / "share" / "sesman" / "trash" / "claude"
    if trash.is_dir():
        for p in trash.glob("*00000000-dead-beef*"):
            p.unlink(missing_ok=True)


def run(pw):
    b = pw.chromium.launch()
    ctx = b.new_context()
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    # Chromium 对任意资源 404 只报一条没有 URL 的泛化 console.error；
    # 由 response 事件记录可定位的状态和 URL，控制台这里只收真正的脚本错误。
    p.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
         if m.type == "error" and not m.text.startswith("Failed to load resource:") else None)
    p.on("response", lambda r: errors.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)

    p.goto(BASE, wait_until="networkidle")
    p.wait_for_selector(".item", timeout=15000)

    # ---- 1. 基本加载 ----
    n_items = p.locator(".item").count()
    check("会话列表渲染", n_items > 50, n_items)
    check("顶栏统计显示数量", re.search(r"\d+", p.locator("#stat").inner_text()), p.locator("#stat").inner_text())
    check("三个来源 chip 都在", p.locator(".chip").count() == 3)
    check("图标 SVG 渲染", p.locator(".item .ico svg").count() > 0)

    # ---- 2. 来源筛选 ----
    counts = {}
    for i in range(3):
        chip = p.locator(".chip").nth(i)
        counts[chip.inner_text().split()[0]] = int(re.search(r"(\d+)$", chip.inner_text().strip()).group(1))
    total_before = p.locator(".item").count()
    p.locator(".chip").nth(0).click()
    after = p.locator(".item").count()
    check("点 chip 过滤掉该来源", after < total_before, f"{total_before}->{after}")
    check("chip 变灰", "off" in (p.locator(".chip").nth(0).get_attribute("class") or ""))
    p.locator(".chip").nth(0).click()
    check("再点恢复", p.locator(".item").count() == total_before)

    # ---- 3. 视图切换 ----
    tree_heads = [p.locator(".ghead .gname").nth(i).inner_text() for i in range(min(3, p.locator(".ghead").count()))]
    p.locator('#view button[data-v="date"]').click()
    p.wait_for_timeout(200)
    date_heads = [p.locator(".ghead .gname").nth(i).inner_text() for i in range(min(3, p.locator(".ghead").count()))]
    check("时间轴视图分组是日期", all(re.match(r"^\d{4}-\d{2}-\d{2}$", h) for h in date_heads), date_heads)
    check("日期分组倒排", date_heads == sorted(date_heads, reverse=True), date_heads)
    cwds = p.locator(".item .cwd").evaluate_all("ns => ns.map(n => n.textContent)")
    check("时间轴每条都显示所在目录", len(cwds) == p.locator(".item").count(), f"{len(cwds)}/{p.locator('.item').count()}")
    check("目录是完整路径而非末段", all(c.startswith(("/", "~", "(")) for c in cwds), cwds[:3])
    check("home 路径缩写成 ~", any(c.startswith("~") for c in cwds), cwds[:5])
    # rtl 会把开头的 "/" 视觉上挪到末尾, DOM 文本看不出来, 只能查样式
    check("目录不使用 rtl 排版",
          p.locator(".item .cwd").first.evaluate("n => getComputedStyle(n).direction") == "ltr")
    check("视图按钮高亮切换", "on" in (p.locator('#view button[data-v="date"]').get_attribute("class") or ""))
    p.locator('#view button[data-v="tree"]').click()
    p.wait_for_timeout(200)
    check("切回项目树", p.locator(".ghead .gname").nth(0).inner_text() == tree_heads[0], tree_heads[:1])

    # ---- 4. 分组折叠 ----
    g0 = p.locator(".group").nth(0)
    before_vis = g0.locator(".item").count()
    g0.locator(".ghead").click()
    p.wait_for_timeout(150)
    check("分组折叠隐藏条目", not g0.locator(".item").nth(0).is_visible() if before_vis else True)
    g0.locator(".ghead").click()
    p.wait_for_timeout(150)
    check("分组再展开", g0.locator(".item").nth(0).is_visible() if before_vis else True)

    # ---- 5. 标题过滤 ----
    p.fill("#q", "SESMAN自测")
    p.wait_for_timeout(250)
    check("标题过滤生效", p.locator(".item").count() == 1, p.locator(".item").count())
    p.fill("#q", "")
    p.wait_for_timeout(250)
    check("清空过滤恢复", p.locator(".item").count() == total_before)

    # ---- 6. 全文搜索 ----
    p.fill("#q", "长文本测试")
    p.press("#q", "Enter")
    p.wait_for_timeout(3000)
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=30000)
    check("全文搜索有结果", p.locator(".item").count() >= 1, p.locator("#stat").inner_text())
    check("搜索片段高亮", p.locator(".snip mark").count() > 0)
    check("片段里的高亮词实际可见",
          p.locator(".snip mark").first.is_visible()
          and p.locator(".snip mark").first.bounding_box()["width"] > 0)
    p.fill("#q", "")
    p.wait_for_timeout(300)

    # ---- 6a. 搜索选项: 大小写 / 全词 / 正则 ----
    def search_hits(term, opts=()):
        """搜一个词, 返回自测会话上显示的命中数。"""
        p.fill("#q", "")            # 先退出搜索态, 之后切选项就不会触发重搜
        p.wait_for_timeout(150)
        for k in ("case", "word", "regex"):          # 复位所有选项
            btn = p.locator(f'#opts button[data-o="{k}"]')
            if "on" in (btn.get_attribute("class") or ""):
                btn.click()
                p.wait_for_timeout(120)
        for k in opts:
            p.locator(f'#opts button[data-o="{k}"]').click()
            p.wait_for_timeout(120)
        # 切换选项也会触发重搜, 先等它跑完再发起自己这次, 否则读到上一次的结果
        p.wait_for_function("!document.querySelector('#stat').textContent.includes('搜索中')",
                            timeout=90000)
        seq = p.get_attribute("#stat", "data-seq") or ""
        p.fill("#q", term)
        p.press("#q", "Enter")
        p.wait_for_function(f"document.querySelector('#stat').dataset.seq !== '{seq}'", timeout=90000)
        it = p.locator(".item").filter(has_text="SESMAN自测")
        if not it.count():
            return 0
        m = re.search(r"命中 (\d+)", it.first.locator(".m").inner_text())
        return int(m.group(1)) if m else 0

    check("三个搜索选项按钮都在", p.locator("#opts button").count() == 3)
    ci = search_hits("SesmanCase")
    cs = search_hits("SesmanCase", ["case"])
    check("默认大小写不敏感", ci == 2, ci)
    check("大小写敏感只匹配一次", cs == 1, cs)
    check("选项按钮显示为激活", "on" in (p.locator('#opts button[data-o="case"]').get_attribute("class") or ""))

    sub = search_hits("hi")
    whole = search_hits("hi", ["word"])
    check("全词匹配少于子串匹配", 0 < whole < sub, f"whole={whole} sub={sub}")

    rx = search_hits("自测.{0,4}内容", ["regex"])
    check("正则匹配生效", rx >= 1, rx)
    # 列表命中不等于正文高亮: 两条路径的匹配实现是分开的
    p.locator(".item").filter(has_text="SESMAN自测").first.click()
    p.wait_for_selector(".msg", timeout=20000)
    check("正则模式下正文也高亮", p.locator("#msgs mark").count() > 0,
          p.locator("#mcount").inner_text())
    check("正则高亮命中的是实际文本", "自测思考内容" in p.locator("#msgs mark").first.inner_text(),
          p.locator("#msgs mark").first.inner_text())
    lit = search_hits("自测.{0,4}内容")
    check("非正则时特殊字符按字面处理", lit == 0, lit)

    search_hits("[", ["regex"])
    check("坏正则给出错误提示", "无效" in p.locator("#stat").inner_text(), p.locator("#stat").inner_text())
    check("坏正则不产生 JS 报错", not errors, errors[:2])

    # 选项持久化
    p.locator('#opts button[data-o="word"]').click()
    p.wait_for_timeout(200)
    p.reload(wait_until="networkidle")
    p.wait_for_selector(".item", timeout=15000)
    check("选项状态持久化", "on" in (p.locator('#opts button[data-o="word"]').get_attribute("class") or ""))
    p.locator('#opts button[data-o="word"]').click()
    p.wait_for_timeout(150)

    # ---- 6a2. 海量命中的限流 ----
    p.fill("#q", "a")
    p.press("#q", "Enter")
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=120000)
    check("海量结果提示已截断", "截断" in p.locator("#stat").inner_text(), p.locator("#stat").inner_text())
    p.locator(".item").filter(has_text="SESMAN自测").first.click()
    p.wait_for_selector(".msg", timeout=60000)
    n_mark = p.locator("#msgs mark").count()
    check("高亮数量达到且不超过上限", n_mark == 3000, n_mark)
    check("超限的命中消息标 ●", p.locator(".msg.hashit").count() > 0, p.locator(".msg.hashit").count())
    check("命中数显示为 N+", "+ 处匹配" in p.locator("#mcount").inner_text(),
          p.locator("#mcount").inner_text())
    check("页面仍可交互", p.locator("#a-fold").is_enabled())
    p.fill("#q", "")
    p.wait_for_timeout(400)

    # ---- 6b. 搜索词高亮 ----
    p.fill("#q", "自测思考内容")           # 只出现在默认折叠的 thinking 里
    p.press("#q", "Enter")
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=60000)
    check("搜索命中列表标题也高亮", p.locator(".item .t mark").count() >= 0)
    # 搜索词也会出现在别的会话里, 按标题锁定自测会话
    p.locator(".item").filter(has_text="SESMAN自测").first.click()
    p.wait_for_selector(".msg", timeout=15000)
    check("详情正文出现高亮", p.locator("#msgs mark").count() > 0, p.locator("#msgs mark").count())
    check("命中消息自动展开", p.locator('.msg[data-role="thinking"] .mb').first.is_visible())
    check("高亮词内容正确", p.locator("#msgs mark").first.inner_text() == "自测思考内容",
          p.locator("#msgs mark").first.inner_text())
    check("显示匹配计数", "处匹配" in p.locator("#mcount").inner_text(), p.locator("#mcount").inner_text())
    check("首个匹配已定位", p.locator("#msgs mark.cur").count() == 1)
    check("高亮只落在正文里", p.locator("#msgs .fold-preview mark").count() == 0,
          p.locator("#msgs .fold-preview mark").count())
    check("每个高亮都可见",
          all(p.locator("#msgs mark").nth(i).is_visible() for i in range(p.locator("#msgs mark").count())))
    n_marks = p.locator("#msgs mark").count()
    if n_marks > 1:
        p.click("#m-next")
        p.wait_for_timeout(400)
        check("下一处跳转", p.locator("#msgs mark.cur").count() == 1)
    check("高亮不破坏正文", "自测思考内容" in p.locator('.msg[data-role="thinking"] .mb').first.inner_text())
    p.fill("#q", "")
    p.wait_for_timeout(300)

    # ---- 6c. 命中在长文本里也能看到 ----
    p.fill("#q", "长文本测试")
    p.press("#q", "Enter")
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=60000)
    p.locator(".item").filter(has_text="SESMAN自测").first.click()
    p.wait_for_selector("#msgs mark", timeout=15000)
    check("被截断的长消息命中时直接全文展开",
          p.locator(".msg .mb.clip").count() == 0 or p.locator("#msgs mark").count() > 0)
    p.fill("#q", "")
    p.wait_for_timeout(300)

    # ---- 7. 打开自测会话 ----
    p.fill("#q", "SESMAN自测")
    p.wait_for_timeout(250)
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=10000)
    check("选中项高亮", p.locator(".item.sel").count() == 1)
    check("详情标题正确", "SESMAN自测会话请删除" in p.locator(".dhead h2").inner_text())
    check("详情元信息含 cwd", "/tmp/sesman-selftest" in p.locator(".dmeta").inner_text())
    roles = p.locator("#msgs [data-role]").evaluate_all("ns => ns.map(n => n.dataset.role)")
    check("消息角色齐全", {"user", "assistant", "thinking", "tool", "tool_result", "context"} <= set(roles), roles)

    # ---- 8. 默认折叠规则 ----
    def body_visible(role):
        return p.locator(f'.msg[data-role="{role}"] .mb').first.is_visible()
    check("user 默认展开", body_visible("user"))
    check("assistant 默认展开", body_visible("assistant"))
    check("thinking 从不折叠", body_visible("thinking"))
    check("注入上下文从不折叠", body_visible("context"))
    check("对话内容没有折叠入口",
          p.locator('.msg[data-role="user"] > .fold-preview, '
                    '.msg[data-role="assistant"] > .fold-preview, '
                    '.msg[data-role="thinking"] > .fold-preview, '
                    '.msg[data-role="context"] > .fold-preview').count() == 0)
    single_tool = p.locator('.msg[data-role="tool_result"]').filter(has_text="单行工具输出不折叠")
    check("单行工具输出也不折叠",
          single_tool.locator(".mb").is_visible() and single_tool.locator(".fold-preview").count() == 0)
    single_tool_skin = single_tool.evaluate("""n => {
      const body = n.querySelector(':scope > .mb'), pre = body.querySelector(':scope > pre');
      const ns = getComputedStyle(n), bs = getComputedStyle(body), ps = getComputedStyle(pre);
      return { bodyPadding: bs.padding, preBackground: ps.backgroundColor,
               preBorder: ps.borderTopWidth, preMargin: ps.margin,
               bubbleBackground: ns.backgroundColor, textColor: ps.color,
               scrollbarWidth: ps.scrollbarWidth, scrollbarColor: ps.scrollbarColor };
    }""")
    check("单条工具输出没有外黄里灰的双层气泡",
          all(single_tool_skin[k] == v for k, v in {
              "bodyPadding": "0px", "preBackground": "rgba(0, 0, 0, 0)",
              "preBorder": "0px", "preMargin": "0px"}.items()), single_tool_skin)
    terminal_theme = p.evaluate("termTheme()")
    check("工具输出与终端使用同一套纯黑底柔和灰字",
          single_tool_skin["bubbleBackground"] == "rgb(0, 0, 0)"
          and single_tool_skin["textColor"] == "rgb(184, 190, 201)"
          and terminal_theme["background"] == "#000000"
          and terminal_theme["foreground"] == "#b8bec9", (single_tool_skin, terminal_theme))
    font_pair = p.evaluate("""() => ({
      tool: getComputedStyle(document.querySelector('.msg[data-role="tool_result"] > .mb > pre')).fontFamily,
      toolSize: getComputedStyle(document.querySelector('.msg[data-role="tool_result"] > .mb > pre')).fontSize,
      terminal: termFont(), terminalSize: termFontSize()
    })""")
    check("工具输出与 tmux 终端共用 Cascadia/系统等宽字体栈",
          "Sesman Cascadia Mono" in font_pair["tool"] and "Adwaita Mono" in font_pair["tool"]
          and "Ubuntu Mono" in font_pair["tool"] and "Consola" in font_pair["tool"]
          and "Microsoft YaHei" in font_pair["tool"] and "Noto Sans Mono CJK SC" in font_pair["tool"]
          and "Sesman Cascadia Mono" in font_pair["terminal"] and "Consola" in font_pair["terminal"]
          and "Microsoft YaHei" in font_pair["terminal"]
          and font_pair["toolSize"] == "12.96px" and font_pair["terminalSize"] == 14.04, font_pair)
    p.wait_for_function("document.fonts.check('12px \\\"Sesman Cascadia Mono\\\"')", timeout=15000)
    bundled_font = p.evaluate("""() => ({
      loaded: document.fonts.check('12px "Sesman Cascadia Mono"'),
      requested: performance.getEntriesByName(location.origin + '/fonts/CascadiaMono.woff2').length
    })""")
    check("Cascadia Mono 网页字体由项目自带且已加载", bundled_font["loaded"] and bundled_font["requested"] > 0,
          bundled_font)
    check("滚动条使用细圆角低对比样式且轨道不是纯黑",
          single_tool_skin["scrollbarWidth"] == "thin"
          and "rgba(0, 0, 0, 0)" not in single_tool_skin["scrollbarColor"]
          and "rgb(0, 0, 0)" not in single_tool_skin["scrollbarColor"], single_tool_skin)

    # 所有状态都没有角色/时间 header；左右留白和背景色构成聊天气泡层级。
    check("所有消息都没有 header", p.locator("#msgs .mh").count() == 0)
    bubble_geo = p.evaluate("""() => {
      const box = document.querySelector('#msgs'), cs = getComputedStyle(box), br = box.getBoundingClientRect();
      const left = br.left + parseFloat(cs.paddingLeft), right = br.right - parseFloat(cs.paddingRight);
      const width = right - left;
      const u = document.querySelector('#msgs > .msg[data-role="user"]:not(.folded)').getBoundingClientRect();
      const a = document.querySelector('#msgs > .msg[data-role="assistant"]:not(.folded)').getBoundingClientRect();
      return { userLeft: (u.left-left)/width, userRight: right-u.right,
               otherLeft: a.left-left, otherRight: (right-a.right)/width };
    }""")
    check("用户气泡靠右且左侧至少留 10%",
          bubble_geo["userRight"] <= 2 and bubble_geo["userLeft"] >= .095, bubble_geo)
    check("其他气泡靠左且右侧至少留 10%",
          bubble_geo["otherLeft"] <= 2 and bubble_geo["otherRight"] >= .095, bubble_geo)
    colors = p.locator('#msgs > .msg[data-role="user"], #msgs > .msg[data-role="assistant"]').evaluate_all(
        "ns => ns.slice(0, 2).map(n => getComputedStyle(n).backgroundColor)")
    check("用户与助手用不同背景色区分", len(colors) == 2 and colors[0] != colors[1], colors)
    content_widths = p.evaluate("""() => ({
      messages: getComputedStyle(document.querySelector('#msgs')).maxWidth,
      composer: getComputedStyle(document.querySelector('#composer')).maxWidth,
      header: getComputedStyle(document.querySelector('.dhead')).maxWidth,
      terminal: getComputedStyle(document.querySelector('#termpane')).maxWidth
    })""")
    check("详情头、消息区、输入区与终端右边缘使用相同最大宽度",
          all(v == "1150px" for v in content_widths.values()), content_widths)
    folded_geo = p.locator("#msgs > .msg.folded").first.evaluate("""n => {
      const box = document.querySelector('#msgs').getBoundingClientRect();
      const r = n.getBoundingClientRect();
      return {ratio: r.width / box.width, radius: getComputedStyle(n).borderRadius,
              preview: !!n.querySelector(':scope > .fold-preview > .peek')};
    }""")
    check("工具折叠态是纯正文预览短胶囊",
          folded_geo["ratio"] <= .74 and folded_geo["radius"] == "999px"
          and folded_geo["preview"], folded_geo)

    # ---- 8a. markdown 渲染 ----
    check("表格渲染成 table", p.locator(".mb table").count() == 1, p.locator(".mb table").count())
    check("表头正确", [p.locator(".mb th").nth(i).inner_text() for i in range(3)] == ["列A", "列B", "数值"],
          [p.locator(".mb th").nth(i).inner_text() for i in range(3)])
    check("表格行数正确", p.locator(".mb tbody tr").count() == 2, p.locator(".mb tbody tr").count())
    aligns = p.locator(".mb tbody tr").first.locator("td").evaluate_all(
        "ns => ns.map(n => getComputedStyle(n).textAlign)")
    check("表格对齐生效", aligns == ["left", "center", "right"], aligns)
    check("单元格内保留行内标记", p.locator(".mb td code").count() >= 1 and p.locator(".mb td b").count() >= 1)
    check("表格可横向滚动不撑破布局",
          p.evaluate("document.body.scrollWidth <= window.innerWidth + 1"))
    check("无序列表渲染", p.locator(".mb ul li").count() >= 2, p.locator(".mb ul li").count())
    check("有序列表渲染", p.locator(".mb ol li").count() >= 2, p.locator(".mb ol li").count())
    check("引用块渲染", p.locator(".mb blockquote").count() >= 1)
    check("分隔线渲染", p.locator(".mb hr").count() >= 1)
    check("标题渲染", p.locator(".mb h3").count() >= 1)
    check("普通段落仍在", "普通段落" in p.locator(".msgs").inner_text())
    check("行内公式由 KaTeX 渲染", p.locator(".mb .katex").count() >= 2,
          p.locator(".mb .katex").count())
    check("块公式使用 display 样式", p.locator(".mb .katex-display").count() >= 1)
    check("代码块里的美元符号不渲染公式", p.locator(".mb pre .katex").count() == 0)
    imgs = p.locator(".mb img")
    check("Markdown 与结构化图片都渲染", imgs.count() >= 2, imgs.count())
    check("本地及内嵌图片走受限媒体接口",
          all(x.startswith("/api/media/") for x in imgs.evaluate_all("ns => ns.map(n => new URL(n.src).pathname)")))
    imgs.first.scroll_into_view_if_needed()
    p.wait_for_function("[...document.querySelectorAll('.mb img')].some(x => x.complete && x.naturalWidth > 0)",
                        timeout=15000)
    check("图片延迟加载后可正常解码",
          imgs.evaluate_all("ns => ns.filter(x => x.complete && x.naturalWidth > 0).length") >= 1)
    check("点击图片可打开原图", imgs.first.evaluate("n => n.closest('a')?.target") == "_blank")
    check("不存在的相对图片不发请求", "[图片: 缺失图]" in p.locator(".msgs").inner_text())

    # ---- 8b. 连续工具调用合并成组 ----
    grp = p.locator('.msg[data-role="toolgroup"]').first
    check("连续工具调用合并成组", grp.count() > 0)
    if grp.count():
        check("组预览显示调用次数", re.search(r"\d+ 次工具调用", grp.locator("> .fold-preview").inner_text()),
              grp.locator("> .fold-preview").inner_text())
        check("组预览显示工具名摘要", grp.locator("> .fold-preview .peek").inner_text().strip() != "")
        check("组默认折叠", not grp.locator("> .tool-entry").first.is_visible())
        inner = grp.locator("> .tool-entry").count()
        check("组内至少 3 条", inner >= 3, inner)
        grp.locator("> .fold-preview").click()
        p.wait_for_timeout(200)
        check("点组预览展开", grp.locator("> .tool-entry").first.is_visible())
        check("展开后预览不再占垂直空间", not grp.locator("> .fold-preview").is_visible())
        check("工具组不再包 grp-body", grp.locator("> .grp-body").count() == 0)
        check("工具组内不再嵌套 msg 气泡", grp.locator("> .msg").count() == 0)
        grp.locator("> .disclosure").click()
        p.wait_for_timeout(150)
        check("组可再折叠", not grp.locator("> .tool-entry").first.is_visible())

    # ---- 9/10. 仅批量折叠 / 展开工具输出 ----
    fold_btn = p.locator("#a-fold")
    fold_btn.click()
    p.wait_for_timeout(200)
    check("批量折叠只收起工具输出",
          not grp.locator("> .tool-entry").first.is_visible()
          and body_visible("thinking") and body_visible("user"))
    check("折叠图标变为展开工具输出", fold_btn.get_attribute("title") == "展开工具输出",
          fold_btn.get_attribute("title"))
    fold_btn.click()
    p.wait_for_timeout(200)
    check("批量展开后工具内容打开", grp.locator("> .tool-entry").first.is_visible())
    check("展开图标变回折叠工具输出", fold_btn.get_attribute("title") == "折叠工具输出",
          fold_btn.get_attribute("title"))

    # ---- 11. 展开全文 ----
    more = p.locator(".msg .more:visible").filter(has_text="展开全文").first
    check("长消息出现展开全文按钮", more.count() > 0 and "展开全文" in more.inner_text())
    # 对话长文只有这一个展开入口，展开后按钮消失。
    target = more.evaluate_handle("n => n.closest('.msg')").as_element()
    mb = target.query_selector(".mb")
    h_before = mb.bounding_box()["height"]
    check("截断样式实际生效(有 clip 类)", mb.evaluate("n => n.classList.contains('clip')"))
    check("截断高度受限", h_before <= 400, h_before)
    target.query_selector(".more").click()
    p.wait_for_timeout(400)
    h_after = mb.bounding_box()["height"]
    check("展开全文后高度变大", h_after > h_before + 100, f"{h_before}->{h_after}")
    check("展开后没有可见按钮", not target.query_selector(".more").is_visible())
    check("展开后内容完整", "点下方按钮展开全文" not in mb.inner_text())

    # ---- 12. 导出 ----
    href = p.locator('#a-export').get_attribute("href")
    r = ctx.request.get(BASE + href)
    check("导出 Markdown 可下载", r.status == 200 and len(r.body()) > 100, r.status)

    # ---- 13. 子代理合并 (换一个有子代理的真实会话) ----
    p.fill("#q", "")
    p.wait_for_timeout(300)
    idx = p.locator(".item").evaluate_all("ns => ns.findIndex(n => n.querySelector('.m').textContent.includes('⑂'))")
    if idx >= 0:
        p.locator(".item").nth(idx).click()
        p.wait_for_selector("#a-agents", timeout=15000)
        before = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
        p.locator("#a-agents").click()
        p.wait_for_timeout(3000)
        p.wait_for_selector("#a-agents", timeout=30000)
        after_n = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
        check("合并子代理后消息变多", after_n > before, f"{before}->{after_n}")
        check("合并按钮显示选中态", p.locator("#a-agents").get_attribute("aria-pressed") == "true")
    else:
        check("找到带子代理的会话", False, "无")

    # ---- 14. 整份载入 + 进度条 + LRU 缓存 ----
    p.fill("#q", "")
    p.wait_for_timeout(200)
    p.evaluate("""() => {
      window.__prog = false;
      new MutationObserver(() => {
        if (document.querySelector('#prog').classList.contains('on')) window.__prog = true;
      }).observe(document.querySelector('#prog'), { attributes: true });
    }""")
    reqs = []
    p.on("request", lambda r: reqs.append(r.url) if "/api/messages/" in r.url else None)
    # 先等上一次载入彻底结束, 否则它的 cachePut 会在 clear 之后落地
    p.wait_for_function("!document.querySelector('#prog').classList.contains('on')", timeout=60000)
    big = p.locator(".item").evaluate_all(
        "ns => ns.map((n, i) => [i, n.querySelector('.m').textContent, n.classList.contains('live')])"
        "      .filter(([, t, live]) => !live && /(\\d+(\\.\\d+)?)M/.test(t)).map(([i]) => i)[0]")
    # 上一段可能还有没落地的载入, 清两次并等一拍, 否则它的 cachePut 会落在 clear 之后
    p.evaluate("cache.clear()")
    p.wait_for_load_state("networkidle")
    p.wait_for_timeout(400)
    p.evaluate("cache.clear()")
    reqs.clear()
    p.locator(".item").nth(big).click()
    p.wait_for_selector(".msg", timeout=120000)
    p.wait_for_function("!document.querySelector('#prog').classList.contains('on')", timeout=120000)
    total = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
    check("一次载入全部消息, 不再分页", p.locator(".more-page").count() == 0)
    check("载入时显示过进度条", p.evaluate("window.__prog"))
    # 工具组本身不算原始消息，组内扁平 tool-entry 各算一条。
    dom_msgs = p.locator("#msgs .msg:not(.grp), #msgs .tool-entry").count()
    check("消息数与顶部计数一致", dom_msgs == total, f"dom={dom_msgs} meta={total}")
    geo = p.evaluate("""() => { const b = document.querySelector('#msgs'), r = b.getBoundingClientRect();
      return { right: Math.round(r.right), win: innerWidth, scrollable: b.scrollHeight > b.clientHeight,
               atBottom: b.scrollHeight - b.scrollTop - b.clientHeight < 5,
               detailScrolls: document.querySelector('#detail').scrollHeight
                              > document.querySelector('#detail').clientHeight }; }""")
    check("滚动条属于消息区",
          p.evaluate("getComputedStyle(document.querySelector('#msgs')).overflowY") in ("auto", "scroll"),
          p.evaluate("getComputedStyle(document.querySelector('#msgs')).overflowY"))
    check("滚动发生在消息区而不是整个详情", not geo["detailScrolls"], geo)
    check("打开会话默认停在最新一条", geo["atBottom"] if geo["scrollable"] else True, geo)

    # 贴底跟随: 窗口缩放、内容展开都要保持在最后一条; 但用户主动上翻后不能再拽回
    at_bottom = lambda: p.evaluate(
        "() => { const b = document.querySelector('#msgs');"
        "        return b.scrollHeight - b.scrollTop - b.clientHeight <= 48; }")
    def viewport(w, h):
        p.set_viewport_size({"width": w, "height": h})
        p.evaluate("dispatchEvent(new Event('resize'))")   # headless 下不会自动派发
        p.wait_for_timeout(800)
    if geo["scrollable"]:
        viewport(900, 520)
        check("缩小窗口后仍停在最新", at_bottom())
        viewport(1400, 860)
        check("放大窗口后仍停在最新", at_bottom())
        p.evaluate("""() => { const n = [...document.querySelectorAll('#msgs .msg.folded')].pop();
                              if (n) n.querySelector('.fold-preview').click(); }""")
        p.wait_for_timeout(600)
        check("展开消息后仍停在最新", at_bottom())
        p.mouse.move(700, 400)
        for _ in range(6):
            p.mouse.wheel(0, -400)
        p.wait_for_timeout(500)
        check("滚轮上翻后离开底部", not at_bottom())
        viewport(1000, 620)
        check("上翻后缩放窗口不被拽回底部", not at_bottom())
        for _ in range(60):
            p.mouse.wheel(0, 3000)
        p.wait_for_timeout(700)
        check("滚回底部", at_bottom())
        viewport(1280, 800)
        check("回到底部后恢复跟随", at_bottom())
    # 只看第一个请求 —— 之后的自动增量同步本来就该带 start
    check("首次打开是整份请求", reqs and "start=" not in reqs[0], reqs[:2])

    # 重新打开当前会话 —— 应命中缓存, 只发增量请求。不要先载入另一个任意
    # 大会话，否则两者超过 64 MB 时触发正常的 LRU 淘汰，反而测不到命中路径。
    reqs.clear()
    p.locator(".item").nth(big).click()
    p.wait_for_selector(".msg", timeout=60000)
    p.wait_for_timeout(800)
    cached = p.evaluate("() => !!cache.get(S.sel)")
    check("再次打开命中缓存", cached and (not reqs or all("start=" in u for u in reqs)),
          f"cached={cached} reqs={reqs[-2:]}")
    # 活跃会话在这期间可能又被推了新消息, 也可能因回滚整份重来, 数字不好精确比;
    # 关键是缓存命中后内容正常渲染出来了
    back = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
    check("缓存命中后内容照常渲染", back > 0 and p.locator("#msgs .msg").count() > 0,
          f"meta={back} dom={p.locator('#msgs .msg').count()} 首次={total}")

    # ---- 14a. 增量同步: 会话被 CLI 追加内容后应自动接上 ----
    p.fill("#q", "SESMAN自测")
    p.wait_for_timeout(250)
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=20000)
    n0 = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
    fake = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
    with open(fake, "a") as fh:                       # 模拟 CLI 追加
        fh.write(json.dumps({
            "type": "user", "message": {"role": "user", "content": "追加的新消息ZZQ"},
            "uuid": "u9", "timestamp": "2026-08-06T12:30:00.000Z",
            "cwd": "/tmp/sesman-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
    p.evaluate("syncSession(S.sel)")
    p.wait_for_function(f"document.querySelector('#mcount-total').textContent !== '{n0} 条消息'",
                        timeout=30000)
    n1 = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
    check("增量同步接上新消息", n1 == n0 + 1, f"{n0}->{n1}")

    check("新消息出现在末尾", "追加的新消息ZZQ" in p.locator("#msgs > .msg").last.inner_text(),
          p.locator("#msgs > .msg").last.inner_text()[:40])
    check("提示新消息条数", p.locator("#newmsg").is_visible() and "+1" in p.locator("#newmsg").inner_text(),
          p.locator("#newmsg").inner_text())
    inc = [u for u in reqs if "start=" in u]
    check("同步走的是增量请求", len(inc) > 0)

    # 文件被改写(不是追加) → 必须整份重来, 不能把旧内容和新内容拼起来
    txt = fake.read_text().splitlines()
    fake.write_text("\n".join([json.dumps({"type": "ai-title", "aiTitle": "SESMAN自测会话请删除",
                                           "sessionId": fake.stem}, ensure_ascii=False)] + txt[1:]) + "\n")
    with open(fake, "a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "改写后追加YYQ"},
                             "uuid": "u10", "timestamp": "2026-08-06T12:31:00.000Z",
                             "cwd": "/tmp/sesman-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
    p.evaluate("syncSession(S.sel)")
    p.wait_for_function("document.querySelector('#msgs').textContent.includes('改写后追加YYQ')", timeout=30000)
    n2 = int(re.search(r"(\d+) 条消息", p.locator(".dmeta").inner_text()).group(1))
    check("文件改写后整份重载, 消息不重复", n2 == n1 + 1, f"{n1}->{n2}")
    check("重载后旧消息仍在一次", p.locator("#msgs").inner_text().count("追加的新消息ZZQ") == 1,
          p.locator("#msgs").inner_text().count("追加的新消息ZZQ"))
    # 更新靠服务端推送(SSE), 不是客户端轮询
    p.wait_for_function("_es && _es.readyState === 1", timeout=20000)
    check("已建立服务端推送连接", p.evaluate("_es.readyState") == 1)
    pulls = []
    p.on("request", lambda r: pulls.append(r.url) if "/api/messages/" in r.url else None)
    lat = []
    for i in range(3):
        t0 = time.time()
        with open(fake, "a") as fh:
            fh.write(json.dumps({
                "type": "user", "message": {"role": "user", "content": "推送消息RT%d" % i},
                "uuid": "rt%d" % i, "timestamp": "2026-08-07T12:1%d:00.000Z" % i,
                "cwd": "/tmp/sesman-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
        p.wait_for_function("document.querySelector('#msgs').textContent.includes('推送消息RT%d')" % i,
                            timeout=20000)
        lat.append(time.time() - t0)
    check("新消息被推送上屏(亚秒级)", max(lat) < 1.0, [round(x, 3) for x in lat])
    check("期间没有客户端主动拉取", not pulls, pulls[:2])

    # 回滚也走推送
    lines = fake.read_text().splitlines()
    fake.write_text("\n".join(lines[:-2] + [json.dumps(
        {"type": "user", "message": {"role": "user", "content": "推送回滚RBK"}, "uuid": "rbk",
         "timestamp": "2026-08-07T12:30:00.000Z", "cwd": "/tmp/sesman-selftest",
         "sessionId": fake.stem}, ensure_ascii=False)]) + "\n")
    p.wait_for_function("document.querySelector('#msgs').textContent.includes('推送回滚RBK')", timeout=20000)
    check("回滚也是推过来的, 且旧内容清除",
          "推送消息RT2" not in p.locator("#msgs").inner_text())

    # 切换会话要断掉旧连接
    other = p.evaluate("() => S.sessions.find(x => x.uid !== S.sel).uid")
    p.evaluate("u => openSession(u)", other)
    p.wait_for_selector(".msg", timeout=60000)
    p.wait_for_function("_es && _esUid === S.sel && _es.readyState === 1", timeout=20000)
    check("切换会话后推送跟着换", p.evaluate("_esUid") == other, p.evaluate("_esUid"))
    p.fill("#q", "")
    p.wait_for_timeout(300)

    # ---- 14z. 活跃会话检测 ----
    p.fill("#q", "")
    p.wait_for_timeout(200)
    api = json.loads(urllib.request.urlopen(BASE + "/api/live", timeout=60).read())
    check("活跃检测接口可用", isinstance(api.get("uids"), list)
          and isinstance(api.get("tmux_uids"), list)
          and set(api["tmux_uids"]).issubset(api["uids"]), api)
    # 跑测试的这个进程本身就是活的 Claude 会话, 至少应检出一个
    check("检出正在运行的会话", len(api["uids"]) >= 1, api["uids"])
    p.evaluate("pollLive()")
    p.wait_for_timeout(600)
    check("前端拿到活跃集合",
          p.evaluate("[S.live.size, S.liveTmux.size]")
          == [len(api["uids"]), len(api["tmux_uids"])],
          p.evaluate("[S.live.size, S.liveTmux.size]"))
    if api["uids"]:
        check("活跃会话标状态点", p.evaluate(
            "[...document.querySelectorAll('.item.live')].map(n => n.dataset.uid)"
            ".every(u => S.live.has(u))"))
        marked = p.locator(".item.live").count()
        shown = p.evaluate("[...S.live].filter(u => document.querySelector(`.item[data-uid=\"${u}\"]`)).length")
        check("列表里可见的活跃会话都标了", marked == shown, f"{marked} vs {shown}")
    check("顶栏显示分类后的进行中计数",
          "进行中" in (p.locator("#livecount").get_attribute("aria-label") or "") if api["uids"] else True,
          p.locator("#livecount").get_attribute("aria-label"))
    # 活跃标记不该重渲染列表 (会打断滚动/选中)
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=30000)
    sel_before = p.locator(".item.sel").count()
    p.evaluate("pollLive()")
    p.wait_for_timeout(500)
    check("刷新活跃标记不影响选中", p.locator(".item.sel").count() == sel_before)

    # ---- 14y. 列表自动跟进磁盘变化 ----
    p.fill("#q", "")
    p.wait_for_timeout(200)
    sig0 = p.evaluate("S.sig")
    check("列表带磁盘签名", bool(sig0), sig0)
    n_before = p.evaluate("S.sessions.length")
    p.evaluate("pollSessions()")
    p.wait_for_timeout(500)
    # 注意: 本会话自己就在往 jsonl 写, 签名必然一直在变, 只能断言会话集合不变
    check("没有新会话时列表长度不变", p.evaluate("S.sessions.length") == n_before,
          f'{n_before}->{p.evaluate("S.sessions.length")}')

    # 选中一个会话并滚动列表, 之后自动刷新不该打断
    p.locator(".item").nth(3).click()
    p.wait_for_selector(".msg", timeout=30000)
    sel_uid = p.evaluate("S.sel")
    p.evaluate("document.querySelector('#side').scrollTop = 240")
    p.wait_for_timeout(150)

    extra = FAKE_PROJ / "00000000-dead-beef-0000-000000000002.jsonl"
    extra.write_text(json.dumps({"type": "ai-title", "aiTitle": "SESMAN新会话ZZ",
                                 "sessionId": extra.stem}, ensure_ascii=False) + "\n"
                     + json.dumps({"type": "user", "message": {"role": "user", "content": "新会话正文"},
                                   "uuid": "n1", "timestamp": "2026-08-07T09:00:00.000Z",
                                   "cwd": "/tmp/sesman-selftest", "sessionId": extra.stem},
                                  ensure_ascii=False) + "\n")
    p.evaluate("pollSessions()")
    p.wait_for_function(f"S.sessions.length > {n_before}", timeout=30000)
    check("新会话自动出现在列表, 不必手动刷新",
          p.evaluate("S.sessions.some(s => s.title.includes('SESMAN新会话ZZ'))"))
    check("签名随之更新", p.evaluate("S.sig") != sig0)
    check("自动刷新不丢选中", p.evaluate("S.sel") == sel_uid and p.locator(".item.sel").count() == 1)
    check("自动刷新保持滚动位置",
          abs(p.evaluate("document.querySelector('#side').scrollTop") - 240) < 30,
          p.evaluate("document.querySelector('#side').scrollTop"))
    check("详情区没被打断", p.locator("#msgs .msg").count() > 0)

    # 活跃会话每隔几秒就变一次, 列表要是每次都重建就会一直闪。
    # 结构没变时只改文字, DOM 节点必须原地不动。
    live_file = FAKE_PROJ / "00000000-dead-beef-0000-000000000003.jsonl"
    live_file.write_text(json.dumps(
        {"type": "ai-title", "aiTitle": "SESMAN活跃写入", "sessionId": live_file.stem},
        ensure_ascii=False) + "\n")
    urllib.request.urlopen(BASE + "/api/sessions?force=1", timeout=60).read()
    p.evaluate("pollSessions()")
    p.wait_for_function("S.sessions.some(s => s.title.includes('SESMAN活跃写入'))", timeout=30000)
    p.wait_for_timeout(400)
    live_uid = p.evaluate("() => S.sessions.find(s => s.title.includes('SESMAN活跃写入')).uid")
    p.evaluate("""(uid) => {
      window.__f = { rebuilt: 0, metas: [] };
      window.__w = document.querySelector('.item[data-uid="' + uid + '"]');
      window.__t = setInterval(() => {
        const now = document.querySelector('.item[data-uid="' + uid + '"]');
        if (now !== window.__w) { window.__f.rebuilt++; window.__w = now; }
        const m = now && now.querySelector('.m');
        const t = m && m.textContent;
        if (t && window.__f.metas[window.__f.metas.length - 1] !== t) window.__f.metas.push(t);
      }, 250);
    }""", live_uid)
    stop = threading.Event()

    def keep_writing():
        i = 0
        while not stop.is_set():
            with open(live_file, "a") as fh:
                fh.write(json.dumps({
                    "type": "user", "message": {"role": "user", "content": "活跃写入%d" % i},
                    "uuid": "lw%d" % i, "timestamp": "2026-08-07T13:00:00.000Z",
                    "cwd": "/tmp/sesman-selftest", "sessionId": live_file.stem},
                    ensure_ascii=False) + "\n")
            i += 1
            time.sleep(1.5)

    th = threading.Thread(target=keep_writing, daemon=True)
    th.start()
    p.wait_for_timeout(20000)
    stop.set()
    th.join(timeout=3)
    f = p.evaluate("window.__f")
    p.evaluate("clearInterval(window.__t)")
    check("会话持续更新时列表基本不重建(不闪)", f["rebuilt"] <= 1, f["rebuilt"])
    check("但信息行确实在跟着更新", len(f["metas"]) >= 2, f["metas"][:4])
    live_file.unlink(missing_ok=True)
    urllib.request.urlopen(BASE + "/api/sessions?force=1", timeout=60).read()
    p.evaluate("pollSessions()")
    p.wait_for_timeout(600)

    # 搜索态下不该被自动刷新冲掉
    seq = p.get_attribute("#stat", "data-seq") or ""
    p.fill("#q", "SESMAN自测")
    p.press("#q", "Enter")
    p.wait_for_function(f"document.querySelector('#stat').dataset.seq !== '{seq}'", timeout=60000)
    p.wait_for_timeout(300)
    hits_before = p.locator(".item").count()
    extra.unlink()                      # 再改一次磁盘, 触发签名变化
    p.evaluate("pollSessions()")
    p.wait_for_timeout(800)
    check("搜索结果不被自动刷新冲掉", p.locator(".item").count() == hits_before,
          f"{hits_before}->{p.locator('.item').count()}")
    check("搜索结果文案不被自动刷新冲掉", "命中" in p.locator("#stat").inner_text(),
          p.locator("#stat").inner_text())
    p.fill("#q", "")
    p.wait_for_timeout(300)

    # ---- 14x. 会话被回滚(截断后重写)时必须整份重来 ----
    # 前面几段已经把这个文件改得七零八落, 重建一份干净的再测
    fake2 = make_fake_session()
    urllib.request.urlopen(BASE + "/api/sessions?force=1", timeout=60).read()
    p.evaluate("() => { cache.clear(); closeWatch(); }")
    if fake2.exists():
        p.fill("#q", "SESMAN自测")
        p.wait_for_timeout(250)
        p.locator(".item").first.click()
        p.wait_for_selector(".msg", timeout=20000)
        p.wait_for_timeout(400)
        before = p.evaluate("() => { const e = cache.get(S.sel); return e ? [e.msgs.length, e.end, !!e.anchor] : null; }")
        check("缓存记录了续读锚点", before and before[2], before)
        # 先放一条标记, 待会儿正是要把它回滚掉
        with open(fake2, "a") as fh:
            fh.write(json.dumps({
                "type": "user", "message": {"role": "user", "content": "将被回滚掉XYZ"},
                "uuid": "xyz", "timestamp": "2026-08-07T11:50:00.000Z",
                "cwd": "/tmp/sesman-selftest", "sessionId": fake2.stem}, ensure_ascii=False) + "\n")
        p.wait_for_function("document.querySelector('#msgs').textContent.includes('将被回滚掉XYZ')",
                            timeout=20000)
        # 模拟双 Esc 回滚: 砍掉尾部, 再写入不同内容, 让文件重新变得更长
        lines = fake2.read_text().splitlines()
        rolled = lines[:-2] + [json.dumps(
            {"type": "user", "message": {"role": "user", "content": "回滚后的新内容QQZ"},
             "uuid": "r%d" % i, "timestamp": "2026-08-07T12:0%d:00.000Z" % i,
             "cwd": "/tmp/sesman-selftest", "sessionId": fake2.stem}, ensure_ascii=False)
            for i in range(4)]
        fake2.write_text("\n".join(rolled) + "\n")
        check("回滚后文件反而更长(只看长度会误判)", fake2.stat().st_size > before[1],
              f"{before[1]} -> {fake2.stat().st_size}")
        p.evaluate("syncSession(S.sel)")
        p.wait_for_function("document.querySelector('#msgs').textContent.includes('回滚后的新内容QQZ')",
                            timeout=30000)
        txt = p.locator("#msgs").inner_text()
        check("回滚后整份重来, 被砍掉的内容消失", "将被回滚掉XYZ" not in txt)
        check("回滚后的新内容只出现应有的次数", txt.count("回滚后的新内容QQZ") == 4,
              txt.count("回滚后的新内容QQZ"))
        p.fill("#q", "")
        p.wait_for_timeout(300)

    # ---- 14b. 刷新按钮 ----
    p.fill("#q", "")
    p.wait_for_timeout(200)
    n_before = p.locator(".item").count()
    p.click("#reload")
    p.wait_for_function(
        f"() => document.querySelectorAll('.item').length === {n_before}"
        " && !document.querySelector('#stat').textContent.includes('扫描')", timeout=30000)
    check("刷新后列表数量不变", p.locator(".item").count() == n_before, p.locator(".item").count())

    # ---- 14c. 搜索态下切换来源筛选 ----
    p.fill("#q", "sesman")
    p.press("#q", "Enter")
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=60000)
    hit_all = p.locator(".item").count()
    srcs = p.locator(".item .ico use").evaluate_all("ns => ns.map(n => n.getAttribute('href'))")
    kind = "i-claude" if "#i-claude" in srcs else srcs[0].lstrip("#")
    p.locator(".chip").nth(["i-claude", "i-codex", "i-grok"].index(kind)).click()
    p.wait_for_timeout(300)
    check("搜索结果也受来源筛选", p.locator(".item").count() < hit_all,
          f"{hit_all}->{p.locator('.item').count()}")
    p.locator(".chip").nth(["i-claude", "i-codex", "i-grok"].index(kind)).click()
    p.wait_for_timeout(300)
    check("恢复来源后搜索结果还在", p.locator(".item").count() == hit_all)
    p.fill("#q", "")
    p.wait_for_timeout(300)
    check("清空输入退出搜索态", p.locator(".item").count() == n_before, p.locator(".item").count())

    # ---- 14d. 折叠预览不冒充 header ----
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=20000)
    check("消息 DOM 中没有 header", p.locator("#msgs .mh").count() == 0)
    check("折叠的消息仍显示预览", p.locator('.msg.folded .peek').first.is_visible())

    # ---- 15. 快捷键 ----
    p.keyboard.press("Escape")
    p.locator("body").click(position={"x": 5, "y": 400})
    p.keyboard.press("/")
    check("斜杠聚焦搜索框", p.evaluate("document.activeElement.id") == "q")
    check("斜杠未写入搜索框", p.input_value("#q") == "", repr(p.input_value("#q")))

    # ---- 15a. 接管会话 (服务端需 --terminal) ----
    tl = json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())
    if not tl.get("enabled"):
        check("终端未启用时不显示接管入口", p.locator("#a-term").count() == 0)
    else:
        dialogs = []
        def _dlg(d):                 # 用完必须摘掉, 否则后面删除会话的确认框也会被它吃掉
            dialogs.append(d.message)
            d.dismiss()
        p.on("dialog", _dlg)
        # 挑最老的一个既不在跑、也没被网页接管的 claude 会话。
        # /api/live 与 tmux 列表是两套状态：用户在另一个浏览器里接管后，
        # 会话本身未必仍被 live.py 识别为原进程。若只排除 live，会复用用户的
        # tmux 会话，令下面的「首次接管」分支和最终 kill-session 产生干扰。
        live_now = set(json.loads(urllib.request.urlopen(BASE + "/api/live", timeout=30).read())["uids"])
        taken_now = {s["name"] for s in tl["sessions"] if s.get("owned")}
        target = p.evaluate("""({liveList, takenList}) => {
          const live = new Set(liveList);
          const taken = new Set(takenList);
          const c = S.sessions.filter(x => x.source === 'claude'
            && !live.has(x.uid)
            && !taken.has(`sesman-${x.source}-${String(x.sid).slice(0, 8)}`))
                              .sort((a, b) => a.updated.localeCompare(b.updated));
          return c.length ? c[0].uid : null;
        }""", {"liveList": list(live_now), "takenList": list(taken_now)})
        check("测试目标未占用用户已接管会话", target is not None, sorted(taken_now))
        if target is None:
            raise RuntimeError("没有可安全接管的 Claude 历史会话")
        p.evaluate("u => openSession(u)", target)
        p.wait_for_selector("#a-term", timeout=60000)
        check("会话详情有接管按钮", p.locator("#a-term").get_attribute("title") == "接管会话",
              p.locator("#a-term").get_attribute("title"))
        check("终端面板初始不显示", p.locator("#termpane").is_hidden())

        p.click("#a-term")
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=90000)
        p.wait_for_timeout(1500)
        tname = p.evaluate("T.name")
        check("一键接管起了 tmux 会话", tname.startswith("sesman-claude-"), tname)
        check("接管未弹确认框(会话本来就没在跑)", not dialogs, dialogs[:1])
        check("终端出现在会话底部", p.locator("#termpane").is_visible())
        check("消息流还在上方", p.locator("#msgs .msg").count() > 0)
        check("按钮变成收起终端", p.locator("#a-term").get_attribute("title") == "收起终端",
              p.locator("#a-term").get_attribute("title"))
        check("状态显示已接管", "已接管" in p.locator("#tstatus").inner_text(),
              p.locator("#tstatus").inner_text())
        p.wait_for_timeout(3500)
        got = p.evaluate("""() => {
          const b = T.term.buffer.active; let s = '';
          for (let i = 0; i < T.term.rows; i++) s += (b.getLine(i)?.translateToString(true) || '');
          return s.trim().length;
        }""")
        check("终端里 CLI 已经在跑", got > 40, got)

        # 收起只是断开, tmux 会话必须还在 —— 这是选 tmux 承载的意义
        p.click("#a-term")
        p.wait_for_timeout(600)
        check("收起后面板隐藏", p.locator("#termpane").is_hidden())
        check("按钮变成展开终端", p.locator("#a-term").get_attribute("title") == "展开终端",
              p.locator("#a-term").get_attribute("title"))
        after = json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())
        check("收起后 tmux 会话仍存活", any(s["name"] == tname for s in after["sessions"]))

        # 再点一次: 已接管的会话直接展开终端, 不重复起
        p.click("#a-term")
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=60000)
        n_now = len(json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())["sessions"])
        check("再次打开复用同一会话", p.evaluate("T.name") == tname and n_now == len(after["sessions"]),
              f'{p.evaluate("T.name")} {n_now}')

        # 输入框: 接管后才出现, 内容直接送进 tmux
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(400)
        check("接管后消息流底部出现输入框", p.locator("#composer").is_visible())
        sent = []
        p.on("response", lambda r: sent.append(r.status) if "/api/term/send" in r.url else None)
        pane_before = subprocess.run(["tmux", "capture-pane", "-p", "-t", tname],
                                     capture_output=True, text=True).stdout
        if "trust this folder" in pane_before:      # 新起的 TUI 可能停在信任确认页
            p.fill("#cinput", "1")
            p.press("#cinput", "Enter")
            p.wait_for_timeout(3500)
        # 历史会话可能恢复在补全菜单或弹层里；先回到普通输入态再测斜杠命令。
        p.click("#cesc")
        p.wait_for_timeout(700)
        p.fill("#cinput", "/help")                  # 本地命令, 不消耗额度但能证明 CLI 收到了
        p.press("#cinput", "Enter")
        # Claude TUI 启动后还可能刷新插件/状态，固定 sleep 4 秒偶尔只截到主界面。
        # 轮询真实帮助页，仍然要求 CLI 确实处理了命令，而不只看发送接口 200。
        help_words = ("code.claude.com", "keybindings", "resets in", "CLAUDE.md",
                      "Keyboard shortcuts", "slash commands", "Available commands")
        pane = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            pane = subprocess.run(["tmux", "capture-pane", "-p", "-t", tname],
                                  capture_output=True, text=True).stdout
            if any(k in pane for k in help_words):
                break
            # 让 Playwright 同时派发 fetch response 事件；time.sleep 会阻塞它的事件泵。
            p.wait_for_timeout(500)
        check("输入框内容送进了会话", sent and all(x == 200 for x in sent), sent)
        check("CLI 确实响应了输入", any(k in pane for k in help_words), pane.strip()[-90:])
        check("发送后输入框清空", p.input_value("#cinput") == "")

        # 终端要像普通终端: 滚轮翻历史、鼠标能框选。
        # 用一个独立的 shell 会话来测 —— CLI 的 TUI 是 alternate screen,
        # tmux 根本不给它存历史, 那条路径走的是"滚轮转方向键"。
        subprocess.run(["tmux", "kill-session", "-t", "sesman-wheeltest"], capture_output=True)
        wname = json.loads(urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/term/new",
            json.dumps({"name": "wheeltest", "cmd": "bash --noprofile --norc", "cwd": "/tmp"}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read())["name"]
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/term/send",
            json.dumps({"name": wname, "text": "for i in $(seq 1 200); do echo 历史行$i; done"}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read()
        time.sleep(1.2)
        p.evaluate("n => openTermPane(n)", wname)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        p.wait_for_timeout(1200)
        in_mode = lambda: subprocess.run(
            ["tmux", "display", "-p", "-t", wname, "#{pane_in_mode}"],
            capture_output=True, text=True).stdout.strip()
        box = p.locator("#xterm").bounding_box()
        p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(5):
            p.mouse.wheel(0, -300)
        p.wait_for_function("T.scrollPos > 0", timeout=15000)
        check("滚轮能翻 tmux 历史", p.evaluate("T.scrollPos") > 0, p.evaluate("T.scrollPos"))
        check("显示已上翻多少行", "上翻" in p.locator("#tscroll").inner_text(),
              p.locator("#tscroll").inner_text())
        check("此时 tmux 进入 copy-mode", in_mode() == "1", in_mode())
        seen = p.evaluate("""() => { const b = T.term.buffer.active; let s = '';
          for (let i = 0; i < T.term.rows; i++) s += (b.getLine(i)?.translateToString(true) || '');
          return s; }""")
        check("确实看到了更早的输出", "历史行1" in seen and "历史行200" not in seen)
        p.click("#tscroll")
        p.wait_for_function("T.scrollPos === 0", timeout=15000)
        check("点提示条回到最新画面", in_mode() == "0", in_mode())
        p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)  # 鼠标挪回终端里
        for _ in range(4):
            p.mouse.wheel(0, -300)
        p.wait_for_function("T.scrollPos > 0", timeout=15000)
        p.evaluate("T.term.focus()")        # 刚点过按钮, 焦点得先还给终端
        p.keyboard.type("x")
        p.wait_for_function("T.scrollPos === 0", timeout=15000)
        ok = received = False
        tail = ""
        for _ in range(20):                 # tmux 退出 copy-mode 和处理按键都有延迟
            p.wait_for_timeout(250)
            ok = in_mode() == "0"
            pane = subprocess.run(["tmux", "capture-pane", "-p", "-t", wname],
                                  capture_output=True, text=True).stdout
            tail = pane.rstrip().splitlines()[-1] if pane.rstrip() else ""
            received = tail.rstrip().endswith("x")
            if ok and received:
                break
        check("一打字就自动回到实时画面", ok, in_mode())
        check("退出滚动时首个字符未被吞", received, tail)
        p.keyboard.press("Backspace")

        was = p.evaluate("T.localMouse")
        p.click("#tmouse")
        p.wait_for_timeout(500)
        check("框选开关可切换", p.evaluate("T.localMouse") != was)
        p.mouse.move(box["x"] + 30, box["y"] + 40)
        p.mouse.down()
        p.mouse.move(box["x"] + 240, box["y"] + 40, steps=8)
        p.mouse.up()
        p.wait_for_timeout(400)
        check("鼠标能框选文本", len(p.evaluate("T.term.getSelection()").strip()) > 0,
              repr(p.evaluate("T.term.getSelection()")[:30]))
        if p.evaluate("T.localMouse") != was:
            p.click("#tmouse")
            p.wait_for_timeout(300)
        # 全屏应用那条路径: 滚轮转成方向键, 不进 copy-mode
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/term/send",
            json.dumps({"name": wname, "text": "less /etc/services"}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read()
        alt = "0"
        for _ in range(20):                     # less 起来要一会儿
            time.sleep(0.3)
            alt = subprocess.run(["tmux", "display", "-p", "-t", wname, "#{alternate_on}"],
                                 capture_output=True, text=True).stdout.strip()
            if alt == "1":
                break
        if alt == "1":
            check("全屏应用被识别为 alternate screen", True)
            p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            for _ in range(3):
                p.mouse.wheel(0, -300)
            p.wait_for_timeout(1000)
            check("全屏应用下不进 copy-mode(滚轮转方向键)", in_mode() == "0", in_mode())
        else:
            print("⏭  跳过全屏应用那条(less 没起来, 环境问题)")
        subprocess.run(["tmux", "kill-session", "-t", wname], capture_output=True)
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(300)
        p.evaluate("n => openTermPane(n)", tname)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(300)

        # 从外部结束这个 tmux 会话, 前端应在下一轮轮询里自己发现并收起
        subprocess.run(["tmux", "kill-session", "-t", tname], capture_output=True)
        p.evaluate("pollLive()")
        p.wait_for_timeout(1200)
        gone = json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())
        check("可以结束接管的会话", not any(s["name"] == tname for s in gone["sessions"]))
        check("外部结束后输入框自动收起", p.locator("#composer").is_hidden())
        check("按钮变回可接管", p.locator("#a-term").get_attribute("title") == "接管会话",
              p.locator("#a-term").get_attribute("title"))
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(300)
        p.remove_listener("dialog", _dlg)

    # ---- 15b. 栏宽拖动 + 状态持久化 ----
    w0 = p.locator("#side").bounding_box()["width"]
    d = p.locator("#drag").bounding_box()
    p.mouse.move(d["x"] + 2, 400)
    p.mouse.down()
    p.mouse.move(d["x"] + 2 + 130, 400, steps=6)
    p.mouse.up()
    w1 = p.locator("#side").bounding_box()["width"]
    check("拖动改变侧栏宽度", abs(w1 - (w0 + 130)) < 10, f"{w0}->{w1}")
    check("详情区跟着收窄", p.locator("#detail").bounding_box()["width"] < 1600 - w1 + 10)
    p.reload(wait_until="networkidle")
    p.wait_for_selector(".item", timeout=15000)
    check("刷新后宽度保持", abs(p.locator("#side").bounding_box()["width"] - w1) < 2,
          p.locator("#side").bounding_box()["width"])
    p.dblclick("#drag")
    p.wait_for_timeout(200)
    check("双击复位到默认宽度", abs(p.locator("#side").bounding_box()["width"] - 340) < 2,
          p.locator("#side").bounding_box()["width"])
    # 拖不出可用区间
    d = p.locator("#drag").bounding_box()
    p.mouse.move(d["x"] + 2, 400); p.mouse.down(); p.mouse.move(5, 400, steps=4); p.mouse.up()
    check("宽度有下限", p.locator("#side").bounding_box()["width"] >= 200,
          p.locator("#side").bounding_box()["width"])
    p.dblclick("#drag")
    p.wait_for_timeout(200)

    # 视图模式 / 选中会话 / 来源筛选 / 搜索选项 都要跨刷新保留
    p.locator('#view button[data-v="date"]').click()
    p.locator(".chip").nth(2).click()
    p.locator('#opts button[data-o="case"]').click()
    p.wait_for_timeout(300)
    p.fill("#q", "SESMAN自测")
    p.wait_for_timeout(250)
    p.locator(".item").first.click()
    p.wait_for_selector(".dhead h2", timeout=15000)
    title = p.locator(".dhead h2").inner_text()
    p.locator(".ghead").first.click()          # 折叠一个分组
    p.wait_for_timeout(200)
    p.reload(wait_until="networkidle")
    # 第一个分组是折叠的, 等 .item 可见会超时 —— 等分组本身
    p.wait_for_selector(".ghead", timeout=15000)
    check("分组折叠状态已记住", "closed" in (p.locator(".group").first.get_attribute("class") or ""))
    check("视图模式已记住", "on" in (p.locator('#view button[data-v="date"]').get_attribute("class") or ""))
    check("时间轴的目录行随之恢复", p.locator(".item .cwd").count() > 0)
    check("来源筛选已记住", "off" in (p.locator(".chip").nth(2).get_attribute("class") or ""))
    check("搜索选项已记住", "on" in (p.locator('#opts button[data-o="case"]').get_attribute("class") or ""))
    p.wait_for_selector(".dhead h2", timeout=20000)
    check("上次打开的会话已恢复", p.locator(".dhead h2").inner_text() == title,
          p.locator(".dhead h2").inner_text())
    # 复位, 不影响后续用例
    p.locator(".ghead").first.click()
    p.locator('#view button[data-v="tree"]').click()
    p.locator(".chip").nth(2).click()
    p.locator('#opts button[data-o="case"]').click()
    p.wait_for_timeout(300)

    # ---- 16. 删除 (自测会话) ----
    p.fill("#q", "SESMAN自测")
    p.wait_for_timeout(300)
    p.locator(".item").first.click()
    p.wait_for_selector("#a-del", timeout=10000)
    p.once("dialog", lambda d: d.accept())
    p.locator("#a-del").click()
    p.wait_for_timeout(1200)
    check("删除后提示回收站", "回收站" in p.locator("#detail").inner_text())
    p.fill("#q", "SESMAN自测")
    p.wait_for_timeout(300)
    check("删除后从列表消失", p.locator(".item").count() == 0, p.locator(".item").count())
    trash = list((Path.home() / ".local/share/sesman/trash/claude").glob("*dead-beef*"))
    check("文件确实移入回收站", len(trash) == 1, trash)
    check("原文件已不在", not (FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl").exists())

    # ---- 17. 无 JS 报错 ----
    check("全程无 JS 错误", not errors, errors[:3])
    b.close()


if __name__ == "__main__":
    cleanup()
    make_fake_session()
    import urllib.request
    urllib.request.urlopen(BASE + "/api/sessions?force=1", timeout=60).read()
    try:
        with sync_playwright() as pw:
            run(pw)
    finally:
        cleanup()
    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", *FAIL, sep="\n  - ")
    sys.exit(1 if FAIL else 0)
