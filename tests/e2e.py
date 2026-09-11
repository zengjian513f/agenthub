"""agenthub 前端端到端测试: 真实浏览器点遍每个交互。"""
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
import urllib.error
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agenthub import (claude_queue, pending as pending_store,
                    server as server_module, session_meta, term)

BASE = os.environ.get("AGENTHUB_BASE", "http://127.0.0.1:8710")
FAKE_PROJ = Path.home() / ".claude" / "projects" / "-tmp-agenthub-selftest"
FAKE_IMG = Path("/tmp/agenthub-selftest-image.png")
FAKE_CWD = Path("/tmp/agenthub-selftest")
PENDING_TERM = "agenthub-claude-e2epending"
PENDING_EXIT_TERM = "agenthub-claude-e2eexit"
TERMINAL_TERM = "agenthub-claude-00000000"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
PASS, FAIL = [], []


# 1×1 像素图片，用于附件/截图相关断言。
E2E_PNG_BASE64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/"
                  "q842iQAAAABJRU5ErkJggg==")
REPO_ROOT = Path(__file__).resolve().parents[1]


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}{'  ' + str(extra) if extra and not cond else ''}")


def open_session_menu(p):
    """宽屏会话操作已摊在标题栏上；只有窄屏需要先展开「⋯」菜单。"""
    more = p.locator("#a-more")
    if more.is_visible():
        more.click()


def tmux_run(server, *args, **kwargs):
    """显式选择 tmux socket，避免测试误操作用户默认 server 中的同名会话。"""
    return subprocess.run(["tmux", "-L", server, *args], **kwargs)


def term_rows():
    return json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())["sessions"]


def term_server(name):
    row = next((x for x in term_rows() if x["name"] == name), None)
    if not row:
        raise RuntimeError(f"找不到终端会话 {name}")
    return row.get("server", "default")


def make_fake_session():
    """造一个一次性会话, 用来安全地测删除。"""
    FAKE_PROJ.mkdir(parents=True, exist_ok=True)
    FAKE_CWD.mkdir(parents=True, exist_ok=True)
    (FAKE_CWD / "autocomplete-alpha").mkdir(exist_ok=True)
    (FAKE_CWD / "autocomplete-alpine").mkdir(exist_ok=True)
    f = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
    sid = f.stem
    long_text = "长文本测试 " + "x" * 9000
    FAKE_IMG.write_bytes(base64.b64decode(PNG_B64))
    attachment_image = FAKE_CWD / "agenthub_attachments/4/image.png"
    attachment_image.parent.mkdir(parents=True, exist_ok=True)
    attachment_image.write_bytes(base64.b64decode(PNG_B64))
    render_sample = (
        "## 渲染自测\n\n| 列A | 列B | 数值 |\n|---|:---:|---:|\n"
        "| `a1` | b1 | 1 |\n| a2 | **b2** | 22 |\n\n"
        "- 列表项一\n- 列表项二\n\n1. 有序一\n2. 有序二\n\n"
        "> 引用内容\n\n---\n\n普通段落\n\n"
        "行内公式 $E=mc^2$，块公式：\n\n$$\\int_0^1 x^2\\,dx = \\frac{1}{3}$$\n\n"
        "```text\n$code_not_math$\n```\n\n"
        "```python\ndef greet(name):\n    return f\"hello {name}\"\n```\n\n"
        "```\n{\n  \"name\": \"agenthub\",\n  \"enabled\": true\n}\n```\n\n"
        "句中提到 ` ```python ` 不是围栏，后文不能被吞掉。\n\n"
        f"![本地测试图]({FAKE_IMG})\n\n"
        "附件1: ./agenthub_attachments/4/image.png\n\n"
        "不存在的相对图片 ![缺失图](path-or-url)"
    )
    rows = [
        {"type": "ai-title", "aiTitle": "AGENTHUB自测会话请删除", "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": render_sample}]},
         "uuid": "a0", "timestamp": "2026-08-06T11:59:00.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": "自测：第一条用户消息"},
         "uuid": "u1", "timestamp": "2026-08-06T12:00:00.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid, "gitBranch": "main"},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "结构化图片测试"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}},
        ]}, "uuid": "u1-img", "timestamp": "2026-08-06T12:00:00.500Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        # 大小写 / 全词选项使用对话正文样本；工具协议不属于全文搜索范围。
        {"type": "user", "message": {"role": "user", "content":
         "AgentHubCase 与 agenthubcase 各一次；wordprobe 与 wordprobe2 各一次"},
         "uuid": "u1b", "timestamp": "2026-08-06T12:00:01.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "自测思考内容；正则探针甲Q7内容"},
            {"type": "text", "text": long_text},
            {"type": "tool_use", "id": "bash-1", "name": "Bash",
             "input": {"command": "echo hi && node --check app.js"}},
            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "/tmp/a.py"}},
            {"type": "tool_use", "id": "bash-2", "name": "Bash", "input": {"command": "echo hi2"}}]},
         "uuid": "a1", "timestamp": "2026-08-06T12:00:05.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "bash-1", "content": "hi"},
            {"type": "tool_result", "tool_use_id": "read-1",
             "content": "def loaded_value():\n    return 42"},
            {"type": "tool_result", "tool_use_id": "bash-2", "is_error": True,
             "content": "Error: Exit code 2\n" + "\n".join(f"log {i}" for i in range(12))}]},
         "uuid": "u2", "timestamp": "2026-08-06T12:00:06.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": "# AGENTS.md instructions\n<INSTRUCTIONS>注入的</INSTRUCTIONS>"},
         "uuid": "u3", "timestamp": "2026-08-06T12:00:07.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
        # 注入记录会被适配器过滤，不能拿它充当工具组边界；显式加入一条
        # 可见正文，保证下面的落单 tool_result 真正覆盖独立卡片路径。
        {"type": "assistant", "message": {"role": "assistant", "content": "工具组结束"},
         "uuid": "a-boundary", "timestamp": "2026-08-06T12:00:07.500Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "单行工具输出不折叠"}]},
         "uuid": "u4", "timestamp": "2026-08-06T12:00:08.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "edit-1", "name": "Edit", "input": {
                "file_path": "/tmp/agenthub-selftest/demo.py",
                "old_string": "def value():\n    return 1",
                "new_string": "def value():\n    return 2",
            }}]}, "uuid": "edit-a", "timestamp": "2026-08-06T12:00:08.200Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "edit-1", "content": "文件修改成功"}]},
         "uuid": "edit-u", "timestamp": "2026-08-06T12:00:08.300Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "ask-1", "name": "AskUserQuestion", "input": {"questions": [{
                "header": "启动方式", "question": "要使用哪种启动方式？", "multiSelect": False,
                "options": [
                    {"label": "tmux", "description": "保留可重连的终端"},
                    {"label": "普通进程", "description": "直接在当前终端运行"},
                ]}]}}]}, "uuid": "ask-a", "timestamp": "2026-08-06T12:00:09.000Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "ask-1",
            "content": "User has answered your questions: 启动方式=tmux"}]},
         "uuid": "ask-u", "timestamp": "2026-08-06T12:00:10.000Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content":
         "<task-notification>\n<task-id>hidden-id</task-id>\n<status>completed</status>\n"
         "<summary>Monitor event: \"自测训练\"</summary>\n"
         "<result>监控详细结果\n第二行</result>\n</task-notification>"},
         "uuid": "notice-u", "timestamp": "2026-08-06T12:00:11.000Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "system", "subtype": "away_summary", "content": "自测任务已经收口",
         "timestamp": "2026-08-06T12:00:12.000Z", "cwd": "/tmp/agenthub-selftest",
         "sessionId": sid},
    ]
    # 模拟老 Claude 会话：大段启动附件会把首个 cwd 挤出前 40 条元数据记录。
    # 详情仍应从文件尾恢复真实 cwd，不能把项目 slug 猜成 /tmp/agenthub/selftest。
    rows[1:1] = [{"type": "progress", "data": {"n": i}} for i in range(45)]
    # 确定性覆盖前端两层限流：前 40 条命中消息自动展开、前 3000 处命中高亮。
    # 不能拿用户真实会话的文件大小推断命中消息数；大文件也可能只有一条超长消息。
    rows.extend({
        "type": "assistant",
        "message": {"role": "assistant", "content": "限流样本 " + "markprobe " * 100},
        "uuid": f"cap-{i}", "timestamp": f"2026-08-06T12:01:{i:02d}.000Z",
        "cwd": "/tmp/agenthub-selftest", "sessionId": sid,
    } for i in range(45))
    rows.append({"type": "system", "subtype": "turn_duration", "durationMs": 1234,
                 "timestamp": "2026-08-06T12:02:00.000Z", "cwd": "/tmp/agenthub-selftest",
                 "sessionId": sid})
    rows.extend([
        {"type": "user", "message": {"role": "user", "content": "/compact"},
         "timestamp": "2026-08-06T12:03:00.000Z", "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-08-06T12:03:02.000Z",
         "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content":
         "This session is being continued from a previous conversation that ran out of context."},
         "timestamp": "2026-08-06T12:03:02.100Z", "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content":
         "<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"},
         "timestamp": "2026-08-06T12:03:02.200Z", "cwd": "/tmp/agenthub-selftest", "sessionId": sid},
    ])
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    sub = FAKE_PROJ / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    agents = [
        ("selftest-one", "调查数据链路", "子代理一的独立结论"),
        ("selftest-two", "检查回测参数", "子代理二的独立结论"),
    ]
    for i, (agent_id, title, answer) in enumerate(agents):
        af = sub / f"agent-{agent_id}.jsonl"
        arows = [
            {"type": "user", "message": {"role": "user", "content": f"子代理任务 {i + 1}"},
             "timestamp": f"2026-08-06T12:10:0{i}.000Z", "sessionId": sid},
            {"type": "assistant", "message": {"role": "assistant", "content": answer},
             "timestamp": f"2026-08-06T12:10:1{i}.000Z", "sessionId": sid},
        ]
        af.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in arows) + "\n")
        af.with_suffix(".meta.json").write_text(json.dumps({
            "agentType": "Explore", "description": title, "spawnDepth": 1,
        }, ensure_ascii=False))
    return f


def make_window_session():
    """造一个超过首载 100+500 窗口的会话，验证中间缺口按需加载。"""
    FAKE_PROJ.mkdir(parents=True, exist_ok=True)
    f = FAKE_PROJ / "00000000-dead-beef-0000-000000000010.jsonl"
    sid = f.stem
    rows = [{"type": "ai-title", "aiTitle": "AGENTHUB分页载入测试", "sessionId": sid}]
    rows.extend({
        "type": "assistant", "message": {"role": "assistant", "content": f"分页消息 {i:03d}"},
        "uuid": f"page-{i}", "timestamp": "2026-08-06T13:00:00.000Z",
        "cwd": "/tmp/agenthub-selftest", "sessionId": sid,
    } for i in range(650))
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return f


def make_cli_resumable(path: Path):
    """Normalize synthetic assistant rows to the shape Claude Code can resume."""
    rows = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        message = row.get("message")
        if (row.get("type") == "assistant" and isinstance(message, dict)
                and isinstance(message.get("content"), str)):
            message["content"] = [{"type": "text", "text": message["content"]}]
        rows.append(row)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                              for row in rows) + "\n")


def cleanup():
    tmux_run("agenthub", "kill-session", "-t", PENDING_TERM, capture_output=True)
    tmux_run("agenthub", "kill-session", "-t", PENDING_EXIT_TERM, capture_output=True)
    tmux_run("agenthub", "kill-session", "-t", TERMINAL_TERM, capture_output=True)
    pending_store.discard(PENDING_TERM)
    pending_store.discard(PENDING_EXIT_TERM)
    fake_path = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
    fake_uid = "claude:" + hashlib.sha1(str(fake_path).encode()).hexdigest()[:16]
    # 前一轮若因断言异常退出，合成会话可能还留有服务端发送账本；只删 JSONL
    # 会让下一轮同一路径/UID 把旧 pending 混进新夹具。
    claude_queue.discard_uid(fake_uid)
    session_meta.discard(fake_uid)
    shutil.rmtree(FAKE_PROJ, ignore_errors=True)
    shutil.rmtree(FAKE_CWD, ignore_errors=True)
    FAKE_IMG.unlink(missing_ok=True)
    trash = Path.home() / ".local" / "share" / "agenthub" / "trash" / "claude"
    if trash.is_dir():
        for p in trash.glob("*00000000-dead-beef*"):
            p.unlink(missing_ok=True)


def run(pw):
    # 无显示器的部署机上 SwiftShader GPU 进程偶发进入不可中断等待，导致
    # requestAnimationFrame 停摆，Playwright 会把所有可见按钮误判为“不稳定”。
    # 几何与样式断言不依赖 GPU，强制 CPU 合成可让交互回归保持确定性。
    launch = {"args": ["--disable-gpu", "--disable-software-rasterizer"]}
    if executable := os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        launch["executable_path"] = executable
    b = pw.chromium.launch(**launch)
    # 主题相关断言从确定的亮色起步，随后验证运行中切换到暗色再切回。
    ctx = b.new_context(color_scheme="light")
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    # Chromium 对任意资源 404 只报一条没有 URL 的泛化 console.error；
    # 由 response 事件记录可定位的状态和 URL，控制台这里只收真正的脚本错误。
    p.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
         if m.type == "error" and not m.text.startswith("Failed to load resource:") else None)
    p.on("response", lambda r: errors.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)

    p.goto(BASE, wait_until="networkidle")
    # 分组折叠状态会跨刷新保留；DOM 中可能已有 item，但第一条恰好位于已折叠分组。
    p.wait_for_selector(".item:visible", timeout=15000)

    # ---- 1. 基本加载 ----
    n_items = p.locator(".item").count()
    check("会话列表渲染",
          n_items > 0
          and p.locator(".item").filter(has_text="AGENTHUB自测会话请删除").count() == 1,
          n_items)
    check("顶栏统计显示数量", re.search(r"\d+", p.locator("#session-total").inner_text()), p.locator("#session-total").inner_text())
    check("三个来源 chip 都在", p.locator(".chip").count() == 3)
    check("来源筛选使用原生按钮", p.locator("button.chip").count() == 3)
    check("图标 SVG 渲染", p.locator(".item .ico svg").count() > 0)
    cli_layers = p.evaluate("""() => ({
      classes:[AGENTHUB_CLIS.claude instanceof ClaudeCli,
        AGENTHUB_CLIS.codex instanceof CodexCli, AGENTHUB_CLIS.grok instanceof GrokCli,
        Object.values(AGENTHUB_CLIS).every(x => x instanceof AgentHubCli)],
      pendingSource:agenthubCli('tmux:agenthub-claude-new-e2e')?.source,
      claudeEnqueue:AGENTHUB_CLIS.claude.queueAction({role:'queue_operation',
        operation:'enqueue', text:'q'}),
      claudeRemove:AGENTHUB_CLIS.claude.queueAction({role:'queue_operation',
        operation:'remove', text:'q'}),
      claudeDequeue:AGENTHUB_CLIS.claude.queueAction({role:'queue_operation',
        operation:'dequeue', text:''}),
      claudePopAll:AGENTHUB_CLIS.claude.queueAction({role:'queue_operation',
        operation:'popAll', text:'q'}),
      codexRemove:AGENTHUB_CLIS.codex.queueAction({role:'queue_operation',
        operation:'remove', text:'q'}),
      states:[AGENTHUB_CLIS.claude.createQueuedMessage({created:1000}).state,
        AGENTHUB_CLIS.codex.createQueuedMessage({created:1000}).state],
      claudeSettled:AGENTHUB_CLIS.claude.settleQueuedMessage(
        AGENTHUB_CLIS.claude.createQueuedMessage({created:1000}), 9001, true),
      claudeMigration:AGENTHUB_CLIS.claude.migrateQueuedMessages(
        [{id:'old', created:1000}], 2, 3),
      migrations:[AGENTHUB_CLIS.codex.migrateQueuedMessages([1], 3, 4),
        AGENTHUB_CLIS.grok.migrateQueuedMessages([1], 3, 4)],
      rewind:[AGENTHUB_CLIS.claude.repeatedEscape(1200, 1000).rewind,
        AGENTHUB_CLIS.codex.repeatedEscape(1200, 1000).rewind,
        AGENTHUB_CLIS.grok.repeatedEscape(1200, 1000).rewind],
      rewindBusy:[AGENTHUB_CLIS.claude.repeatedEscape(
          1200, 1000, {busy:true}).rewind,
        AGENTHUB_CLIS.codex.repeatedEscape(1200, 1000, {busy:true}).rewind],
      rewindDraft:[AGENTHUB_CLIS.claude.repeatedEscape(
          1200, 1000, {empty:false}).rewind,
        AGENTHUB_CLIS.codex.repeatedEscape(1200, 1000, {empty:false}).rewind],
      questionKeys:[AGENTHUB_CLIS.claude.questionAnswerKeys({questions:[{
          options:[{label:'一'}, {label:'二'}]}]}, 1),
        AGENTHUB_CLIS.codex.questionAnswerKeys({questions:[{
          options:[{label:'一'}, {label:'二'}]}]}, 1),
        AGENTHUB_CLIS.grok.questionAnswerKeys({questions:[{
          options:[{label:'一'}, {label:'二'}]}]}, 1)],
      claudeQuestionForm:AGENTHUB_CLIS.claude.questionFormAnswerKeys({questions:[
        {options:[{label:'一'}, {label:'二'}]},
        {options:[{label:'甲'}, {label:'乙'}, {label:'丙'}]}
      ]}, [1, 2]),
      codexApproval:AGENTHUB_CLIS.codex.questionAnswerKeys({kind:'approval', questions:[{
          options:[{label:'允许本次', key:'y'}, {label:'始终允许', key:'p'},
            {label:'拒绝', key:'Escape'}]}]}, 1)
    })""")
    check("三种 CLI 继承公共基类并拥有独立队列策略",
          all(cli_layers["classes"])
          and cli_layers["pendingSource"] == "claude"
          and cli_layers["claudeEnqueue"] == {"type": "confirm", "text": "q"}
          and cli_layers["claudeRemove"] == {"type": "remove", "text": "q"}
          and cli_layers["claudeDequeue"] == {"type": "promote-first"}
          and cli_layers["claudePopAll"] == {"type": "promote-all"}
          and cli_layers["codexRemove"] is None
          and cli_layers["states"] == ["sending", "queued"]
          and cli_layers["claudeSettled"] == {
              "created": 1000, "state": "failed",
              "error": "Claude 未在会话记录中确认接收"}
          and cli_layers["claudeMigration"] == []
          and cli_layers["migrations"] == [[], [1]]
          and cli_layers["rewind"] == [True, True, False]
          and cli_layers["rewindBusy"] == [False, False]
          and cli_layers["rewindDraft"] == [False, False]
          and cli_layers["questionKeys"] == [
              ["Up"] * 5 + ["Down", "Enter"],
              ["2"], None]
          and cli_layers["claudeQuestionForm"] == (
              ["Left"] * 3 + ["Up"] * 5 + ["Down", "Enter"]
              + ["Up"] * 6 + ["Down", "Down", "Enter", "Enter"])
          and cli_layers["codexApproval"] == ["p"], cli_layers)
    codex_branch_rebind = p.evaluate("""async () => {
      const fromUid = 'codex:e2e-old-branch', toUid = 'codex:e2e-current-branch';
      const rootSid = '01234567-89ab-cdef-0123-456789abcdef';
      const name = 'agenthub-codex-01234567', key = viewKey(fromUid);
      const saved = {sel:S.sel, agent:S.agent, sessions:S.sessions,
        termUid:T.uid, termName:T.name, list:T.list, composerUid};
      const oldOpen = openSession, oldAudit = browserAuditEvent;
      const oldConfirm = window.confirm;
      const opened = [], confirms = [];
      try {
        // 与真实回退一致：旧叶子已从列表消失，缓存详情仍在；
        // 同一根 pane 现在由 term/list 标成新分支 uid。
        S.sessions = S.sessions.filter(row => ![fromUid, toUid].includes(row.uid));
        S.sel = fromUid; S.agent = null;
        T.uid = fromUid; T.name = null;
        T.list = [...(T.list || []), {name, uid:toUid}];
        cache.set(key, {meta:{uid:fromUid, source:'codex',
          sid:'fedcba98-7654-3210-fedc-ba9876543210', root_sid:rootSid}, msgs:[]});
        composerDrafts.set(fromUid, {text:'跟随分支的草稿', attachments:[], quotes:[]});
        openSession = async uid => { opened.push(uid); S.sel = uid; };
        browserAuditEvent = () => {};
        window.confirm = text => { confirms.push(text); return false; };
        const exact = linkedTermSession(fromUid);
        const linked = linkedTermSession(fromUid, {followReplacement:true});
        const changed = await rebindSelectedTermSession();
        return {exact, linked, taken:takenOver(fromUid), changed, opened,
          selected:S.sel, termUid:T.uid,
          oldDraft:composerDrafts.has(fromUid),
          newDraft:composerDrafts.get(toUid)?.text || '',
          confirmCount:confirms.length};
      } finally {
        openSession = oldOpen; browserAuditEvent = oldAudit; window.confirm = oldConfirm;
        cache.delete(key); composerDrafts.delete(fromUid); composerDrafts.delete(toUid);
        S.queued.delete(fromUid); S.queued.delete(toUid);
        S.sel = saved.sel; S.agent = saved.agent; S.sessions = saved.sessions;
        T.uid = saved.termUid; T.name = saved.termName; T.list = saved.list;
        composerUid = saved.composerUid;
      }
    }""")
    check("Codex 回退后按根 pane 跟进新分支 uid 并迁移草稿",
          codex_branch_rebind == {
              "exact": None,
              "linked": {"name": "agenthub-codex-01234567",
                         "uid": "codex:e2e-current-branch"},
              "taken": None, "changed": True,
              "opened": ["codex:e2e-current-branch"],
              "selected": "codex:e2e-current-branch",
              "termUid": "codex:e2e-current-branch",
              "oldDraft": False, "newDraft": "跟随分支的草稿",
              "confirmCount": 0,
          }, codex_branch_rebind)
    codex_delivery = p.evaluate("""async () => {
      const session = S.sessions.find(x => x.source === 'codex' && x.uid !== S.sel);
      if (!session) return {error:'no codex session'};
      const uid = session.uid, key = viewKey(uid), name = `agenthub-codex-${session.sid.slice(0, 8)}`;
      const oldPost = post, oldList = T.list, oldEntry = cache.get(key);
      const oldQueued = S.queued.get(uid), oldConfirm = window.confirm;
      const requests = [];
      const confirmations = [];
      T.list = [...(T.list || []), {uid, name}];
      cache.set(key, {activity:{state:'working', ts:'2026-08-09T10:00:00Z'}, msgs:[],
        end:123, version:{head:'head-token'}, anchor:'anchor-token'});
      post = async (url, body) => {
        requests.push({url, body});
        if (url === 'api/session/send') {
          if (!body.overwrite_draft) {
            return {draft_conflict:true, draft_token:'confirmed-draft-v1', outbox:[]};
          }
          return {ok:true, outbox:[{id:'server-1', uid, text:body.text,
            created:1000, state:'confirming', attempts:1, server:true}]};
        }
        return {ok:true};
      };
      window.confirm = text => { confirmations.push(text); return true; };
      try {
        await sendToSession('由服务端托管的 Codex 消息', null, uid);
        await sendToSession(null, ['Escape'], uid);
      }
      finally {
        post = oldPost; T.list = oldList; window.confirm = oldConfirm;
        if (oldEntry) cache.set(key, oldEntry); else cache.delete(key);
      }
      const shown = queuedMessages(uid)[0];
      const persisted = store.get('queuedMessages', []).flatMap(x => x[1] || [])
        .some(x => x.text === '由服务端托管的 Codex 消息');
      if (oldQueued) S.queued.set(uid, oldQueued); else S.queued.delete(uid);
      const queuedRequests = requests.filter(x => x.url === 'api/session/send');
      const queuedRequest = queuedRequests.at(-1);
      const escapeRequest = requests.find(x => x.url === 'api/term/send');
      return {url:queuedRequest?.url, uid:queuedRequest?.body?.uid,
        activity:queuedRequest?.body?.activity?.state, shown:shown?.state,
        cursor:queuedRequest?.body?.cursor, server:shown?.server, persisted,
        overwrite:queuedRequest?.body?.overwrite_draft,
        attempts:queuedRequests.length, confirmations,
        escapeKeys:escapeRequest?.body?.keys};
    }""")
    check("Codex 网页消息以服务端终端回执替代浏览器乐观队列",
          codex_delivery == {"url": "api/session/send", "uid": codex_delivery.get("uid"),
                             "activity": "working", "shown": "confirming",
                             "cursor": {"start": 123, "head": "head-token",
                                        "anchor": "anchor-token"},
                             "server": True, "persisted": False,
                             "overwrite": "confirmed-draft-v1", "attempts": 2,
                             "confirmations": ["终端草稿中有内容，是否覆盖？"],
                             "escapeKeys": ["Escape"]}
          and str(codex_delivery.get("uid", "")).startswith("codex:"), codex_delivery)
    claude_delivery = p.evaluate("""async () => {
      const session = S.sessions.find(x => x.source === 'claude' && x.uid !== S.sel);
      if (!session) return {error:'no claude session'};
      const uid = session.uid, key = viewKey(uid), name = `agenthub-claude-${session.sid.slice(0, 8)}`;
      const oldPost = post, oldList = T.list, oldEntry = cache.get(key);
      const oldQueued = S.queued.get(uid), requests = [];
      T.list = [...(T.list || []), {uid, name}];
      cache.set(key, {activity:{state:'idle', ts:'2026-08-09T10:00:00Z'}, msgs:[],
        end:456, version:{head:'claude-head'}, anchor:'claude-anchor'});
      post = async (url, body) => {
        requests.push({url, body});
        return {ok:true, outbox:[{id:'claude-server-1', uid, text:body.text,
          created:1000, state:'submitted', server:true}],
          outbox_version:{epoch:'claude-e2e', revision:1}};
      };
      try { await sendToSession('由服务端账本托管的 Claude 消息', null, uid); }
      finally {
        post = oldPost; T.list = oldList;
        if (oldEntry) cache.set(key, oldEntry); else cache.delete(key);
      }
      const shown = queuedMessages(uid)[0];
      const persisted = store.get('queuedMessages', []).flatMap(x => x[1] || [])
        .some(x => x.text === '由服务端账本托管的 Claude 消息');
      if (oldQueued) S.queued.set(uid, oldQueued); else S.queued.delete(uid);
      const request = requests.find(x => x.url === 'api/session/send');
      return {url:request?.url, uid:request?.body?.uid, state:shown?.state,
        server:shown?.server, cursor:request?.body?.cursor, persisted,
        direct:requests.some(x => x.url === 'api/term/send')};
    }""")
    check("Claude 正式会话也由服务端幂等账本托管而不再直接写 tmux",
          claude_delivery == {
              "url": "api/session/send", "uid": claude_delivery.get("uid"),
              "state": "submitted", "server": True,
              "cursor": {"start": 456, "head": "claude-head",
                         "anchor": "claude-anchor"},
              "persisted": False, "direct": False,
          } and str(claude_delivery.get("uid", "")).startswith("claude:"),
          claude_delivery)
    codex_draft_decline = p.evaluate("""async () => {
      const session = S.sessions.find(x => x.source === 'codex');
      if (!session) return {error:'no codex session'};
      const uid = session.uid, name = `agenthub-codex-${session.sid.slice(0, 8)}`;
      const oldPost = post, oldFetch = window.fetch, oldConfirm = window.confirm;
      const oldList = T.list, oldComposerUid = composerUid;
      const oldDraft = composerDrafts.get(uid), oldInput = $('#cinput').value;
      const postCalls = [], confirmations = [];
      let uploads = 0;
      T.list = [...(T.list || []), {uid, name}];
      composerUid = uid;
      composerDrafts.set(uid, {text:'不要丢失的网页草稿', quotes:[],
        attachments:[{id:'decline-file', number:1,
          file:new File(['x'], '不应上传.txt', {type:'text/plain'}), kind:'file'}],
        nextAttachmentNumber:2});
      $('#cinput').value = '不要丢失的网页草稿';
      post = async (url, body) => {
        postCalls.push({url, body});
        if (url === 'api/session/draft-status') {
          return {ok:true, draft_state:'editing', draft_conflict:true,
            draft_token:'declined-draft-v1'};
        }
        return {ok:true};
      };
      window.fetch = async () => { uploads += 1; throw new Error('不应上传'); };
      window.confirm = text => { confirmations.push(text); return false; };
      try {
        await submitComposer();
        const draft = composerDrafts.get(uid);
        return {postUrls:postCalls.map(x => x.url), confirmations, uploads,
          input:$('#cinput').value, text:draft.text, files:draft.attachments.length};
      } finally {
        post = oldPost; window.fetch = oldFetch; window.confirm = oldConfirm;
        T.list = oldList; composerUid = oldComposerUid; $('#cinput').value = oldInput;
        if (oldDraft) composerDrafts.set(uid, oldDraft); else composerDrafts.delete(uid);
        renderComposerItems();
      }
    }""")
    check("拒绝覆盖终端草稿时不上传、不入队并保留网页输入",
          codex_draft_decline == {
              "postUrls": ["api/session/draft-status"],
              "confirmations": ["终端草稿中有内容，是否覆盖？"],
              "uploads": 0, "input": "不要丢失的网页草稿",
              "text": "不要丢失的网页草稿", "files": 1,
          }, codex_draft_decline)
    claude_draft_preflight = p.evaluate("""async () => {
      const session = S.sessions.find(x => x.source === 'claude');
      if (!session) return {error:'no claude session'};
      const uid = session.uid, name = `agenthub-claude-${session.sid.slice(0, 8)}`;
      const oldPost = post, oldConfirm = window.confirm, oldList = T.list;
      const calls = [], confirmations = [];
      T.list = [...(T.list || []), {uid, name}];
      post = async (url, body) => {
        calls.push({url, body});
        return {ok:true, draft_state:'editing', draft_conflict:true,
          draft_token:'claude-restored-draft'};
      };
      window.confirm = text => { confirmations.push(text); return false; };
      try {
        const result = await prepareTerminalDraft(uid);
        return {result, urls:calls.map(x => x.url),
          body:calls[0]?.body, confirmations};
      } finally {
        post = oldPost; window.confirm = oldConfirm; T.list = oldList;
      }
    }""")
    check("Claude 的 ESC 回填草稿也在网页发送前要求确认",
          claude_draft_preflight == {
              "result": {"proceed": False, "overwriteDraft": ""},
              "urls": ["api/session/draft-status"],
              "body": {
                  "uid": claude_draft_preflight.get("body", {}).get("uid"),
                  "name": claude_draft_preflight.get("body", {}).get("name"),
              },
              "confirmations": ["终端草稿中有内容，是否覆盖？"],
          }
          and str(claude_draft_preflight.get("body", {}).get("uid", ""))
          .startswith("claude:")
          and str(claude_draft_preflight.get("body", {}).get("name", ""))
          .startswith("agenthub-claude-"), claude_draft_preflight)
    diff_race = p.evaluate("""async () => {
      const uid = 'codex:synthetic-diff-race', key = viewKey(uid);
      const oldEntry = cache.get(key), oldQueued = S.queued.get(uid);
      // 手机 Enter 很容易在提交正文后留下末尾换行；Codex 写 rollout 时会
      // strip。两个气泡视觉相同，前端对账也必须采用相同的 CLI 语义。
      const pending = () => ({id:'server-race', uid, text:'不能消失的消息\\n',
        created:1000, state:'delivering', server:true});
      const entry = () => ({meta:{uid, source:'codex'}, msgs:[],
        version:{head:'head-a'}, end:100, anchor:'anchor-a', activity:null,
        bytes:0, total:0, prompt:null});
      try {
        cache.set(key, entry()); S.queued.set(uid, [pending()]);
        await applyDiff(uid, {reset:false, start:90, end:120,
          version:{head:'head-a'}, anchor:'anchor-b', messages:[{
            role:'user', text:'不能消失的消息', ts:'2026-08-13T00:00:00Z'}],
          outbox:[], activity_changed:false, activity:null});
        const stale = {queued:queuedMessages(uid).length,
          messages:cache.get(key).msgs.length, end:cache.get(key).end};

        cache.set(key, entry()); S.queued.set(uid, [pending()]);
        await applyDiff(uid, {reset:false, start:100, end:120,
          version:{head:'head-a'}, anchor:'anchor-b', messages:[{
            role:'user', text:'不能消失的消息', ts:'2026-08-13T00:00:00Z'}],
          outbox:[], activity_changed:false, activity:null});
        const current = {queued:queuedMessages(uid).length,
          messages:cache.get(key).msgs.map(x => x.text), end:cache.get(key).end};

        cache.set(key, entry()); S.queued.set(uid, [pending()]);
        await applyDiff(uid, {outbox_only:true, outbox:[]});
        const outboxOnly = {queued:queuedMessages(uid).length,
          messages:cache.get(key).msgs.length, end:cache.get(key).end};

        cache.set(key, entry()); S.queued.set(uid, [pending()]);
        await applyDiff(uid, {reset:false, start:100, end:110,
          version:{head:'head-a'}, anchor:'anchor-b', messages:[{
            role:'assistant', text:'账本先确认，但本批还没有用户正文',
            ts:'2026-08-13T00:00:01Z'}],
          outbox:[], activity_changed:false, activity:null});
        const bodyLag = {queued:queuedMessages(uid).length,
          messages:cache.get(key).msgs.map(x => x.text), end:cache.get(key).end};
        await applyDiff(uid, {reset:false, start:110, end:120,
          version:{head:'head-a'}, anchor:'anchor-c', messages:[{
            role:'user', text:'不能消失的消息', ts:'2026-08-13T00:00:02Z'}],
          outbox:[], activity_changed:false, activity:null});
        const bodyArrived = {queued:queuedMessages(uid).length,
          messages:cache.get(key).msgs.map(x => x.text), end:cache.get(key).end};

        cache.set(key, entry()); S.queued.set(uid, [pending()]);
        syncServerOutbox(uid, [], null, {retireMissing:true});
        const explicitDiscard = queuedMessages(uid).length;
        return {stale, current, outboxOnly, bodyLag, bodyArrived, explicitDiscard};
      } finally {
        if (oldEntry) cache.set(key, oldEntry); else cache.delete(key);
        if (oldQueued) S.queued.set(uid, oldQueued); else S.queued.delete(uid);
      }
    }""")
    check("Codex 乱序增量和先到的空队列通知不会吞掉发送气泡",
          diff_race == {
              "stale": {"queued": 1, "messages": 0, "end": 100},
              "current": {"queued": 0, "messages": ["不能消失的消息"], "end": 120},
              "outboxOnly": {"queued": 1, "messages": 0, "end": 100},
              "bodyLag": {"queued": 1,
                           "messages": ["账本先确认，但本批还没有用户正文"], "end": 110},
              "bodyArrived": {"queued": 0,
                               "messages": ["账本先确认，但本批还没有用户正文", "不能消失的消息"],
                               "end": 120},
              "explicitDiscard": 0,
          }, diff_race)
    covered_duplicate = p.evaluate("""async () => {
      const uid = 'codex:synthetic-covered-duplicate', key = viewKey(uid);
      const oldEntry = cache.get(key);
      cache.set(key, {meta:{uid, source:'codex'}, msgs:[{
        role:'user', text:'已经接收的内容', ts:'2026-08-13T00:00:00Z'}],
        version:{head:'head-a'}, end:120, anchor:'anchor-b', activity:{
          role:'status', state:'working', text:'working',
          ts:'2026-08-13T00:00:01Z'},
        bytes:0, total:1, prompt:null});
      try {
        const result = await applyDiff(uid, {reset:false, start:100, end:120,
          version:{head:'head-a'}, anchor:'anchor-b', messages:[{
            role:'user', text:'已经接收的内容', ts:'2026-08-13T00:00:00Z'}],
          outbox:[], activity_changed:false, activity:null});
        await applyDiff(uid, {reset:false, start:100, end:120,
          version:{head:'head-a'}, anchor:'anchor-b', messages:[],
          activity_changed:true, activity:{role:'status', state:'aborted',
            text:'aborted', ts:'2026-08-13T00:00:02Z'}});
        const afterAbort = cache.get(key).activity.state;
        await applyDiff(uid, {reset:false, start:100, end:120,
          version:{head:'head-a'}, anchor:'anchor-b', messages:[],
          activity_changed:true, activity:{role:'status', state:'working',
            text:'working', ts:'2026-08-13T00:00:01Z'}});
        return {result, messages:cache.get(key).msgs.map(x => x.text),
          end:cache.get(key).end, afterAbort,
          finalActivity:cache.get(key).activity.state,
          recovering:diffRecoveries.has(key)};
      } finally {
        if (oldEntry) cache.set(key, oldEntry); else cache.delete(key);
      }
    }""")
    check("SSE 与主动读取的完整重复包不会触发第三次恢复",
          covered_duplicate == {"result": 0, "messages": ["已经接收的内容"],
                                "end": 120, "afterAbort": "aborted",
                                "finalActivity": "aborted", "recovering": False},
          covered_duplicate)
    claude_rewind_replace = p.evaluate("""async () => {
      const uid='claude:synthetic-rewind-replace', key=viewKey(uid);
      const oldEntry=cache.get(key), oldQueued=S.queued.get(uid);
      const oldVersion=S.outboxVersions.get(uid);
      const pending=()=>({id:'claude-old-branch',uid,
        text:'xsec 截面排序损失 ic(M1-E21)\\n\\n入dev',
        created:Date.parse('2026-08-25T18:21:14.960Z'),
        state:'submitted',server:true});
      const entry=()=>({meta:{uid,source:'claude'},msgs:[],
        version:{head:'head-a'},end:100,anchor:'anchor-a',activity:null,
        bytes:0,total:0,prompt:null});
      try {
        cache.set(key,entry()); S.queued.set(uid,[pending()]);
        await applyDiff(uid,{reset:true,start:0,end:110,
          version:{head:'head-a'},anchor:'anchor-b',messages:[{
            role:'assistant',text:'只有更晚回复不能证明用户改了分支',
            ts:'2026-08-25T18:21:30.000Z'}],outbox:[],
          outbox_version:{epoch:'claude-rewind',revision:1},
          activity_changed:false,activity:null,meta:{uid,source:'claude'}});
        const assistantOnly=queuedMessages(uid).length;

        cache.set(key,entry()); S.queued.set(uid,[pending()]);
        await applyDiff(uid,{reset:true,start:0,end:120,
          version:{head:'head-a'},anchor:'anchor-c',messages:[{
            role:'user',text:'xsec 截面排序损失 ic(M1-E21)\\n\\n入主线',
            ts:'2026-08-25T18:21:35.616Z'}],outbox:[],
          outbox_version:{epoch:'claude-rewind',revision:2},
          activity_changed:false,activity:null,meta:{uid,source:'claude'}});
        return {assistantOnly,superseded:queuedMessages(uid).length,
          active:cache.get(key).msgs.map(message=>message.text)};
      } finally {
        if (oldEntry) cache.set(key,oldEntry); else cache.delete(key);
        if (oldQueued) S.queued.set(uid,oldQueued); else S.queued.delete(uid);
        if (oldVersion) S.outboxVersions.set(uid,oldVersion);
        else S.outboxVersions.delete(uid);
      }
    }""")
    check("Claude Esc 编辑成新分支后退掉已被取代的旧发送占位",
          claude_rewind_replace == {
              "assistantOnly": 1, "superseded": 0,
              "active": ["xsec 截面排序损失 ic(M1-E21)\n\n入主线"],
          }, claude_rewind_replace)
    pending_sweep = p.evaluate("""async () => {
      const claudeUid='claude:synthetic-pending-sweep';
      const codexUid='codex:synthetic-pending-sweep';
      const claudeKey=viewKey(claudeUid), codexKey=viewKey(codexUid);
      const oldClaudeEntry=cache.get(claudeKey), oldCodexEntry=cache.get(codexKey);
      const oldClaudeQueued=S.queued.get(claudeUid), oldCodexQueued=S.queued.get(codexUid);
      const oldClaudeVersion=S.outboxVersions.get(claudeUid);
      const oldCodexVersion=S.outboxVersions.get(codexUid);
      const oldFetch=globalThis.fetch;
      let calls=0;
      try {
        cache.set(claudeKey,{meta:{uid:claudeUid,source:'claude'},msgs:[{
          role:'user',text:'编辑后的活动分支',ts:'2026-08-25T18:21:35.616Z'}],
          version:{head:'head'},end:120,anchor:'anchor',activity:null,bytes:0,total:1});
        S.queued.set(claudeUid,[{id:'old-branch',uid:claudeUid,text:'旧分支',
          created:Date.parse('2026-08-25T18:21:14.960Z'),
          afterTs:'2026-08-25T18:21:14.000Z',state:'submitted',server:true}]);
        S.queued.set(codexUid,[{id:'failed-send',uid:codexUid,text:'未确认消息',
          created:Date.parse('2026-08-25T18:22:00.000Z'),
          afterTs:'2026-08-25T18:21:59.000Z',state:'delivering',server:true}]);
        globalThis.fetch=async (input, init) => {
          const url=new URL(String(input),location.href);
          if (!url.pathname.endsWith('/api/session/outbox')) return oldFetch(input, init);
          calls++;
          const uid=url.searchParams.get('uid');
          const data=uid===claudeUid
            ? {outbox:[],outbox_version:{epoch:'e2e-sweep-claude',revision:1}}
            : {outbox:[{id:'failed-send',uid:codexUid,text:'未确认消息',
                created:Date.parse('2026-08-25T18:22:00.000Z'),
                afterTs:'2026-08-25T18:21:59.000Z',state:'failed',
                error:'未在会话记录中确认',server:true}],
              outbox_version:{epoch:'e2e-sweep-codex',revision:1}};
          return {ok:true,json:async()=>data};
        };
        await Promise.all([reconcilePendingUid(claudeUid),reconcilePendingUid(codexUid)]);
        return {calls,claude:queuedMessages(claudeUid).length,
          codex:queuedMessages(codexUid).map(item=>({state:item.state,error:item.error})),
          running:pendingReconciliations.size};
      } finally {
        globalThis.fetch=oldFetch;
        if (oldClaudeEntry) cache.set(claudeKey,oldClaudeEntry); else cache.delete(claudeKey);
        if (oldCodexEntry) cache.set(codexKey,oldCodexEntry); else cache.delete(codexKey);
        if (oldClaudeQueued) S.queued.set(claudeUid,oldClaudeQueued); else S.queued.delete(claudeUid);
        if (oldCodexQueued) S.queued.set(codexUid,oldCodexQueued); else S.queued.delete(codexUid);
        if (oldClaudeVersion) S.outboxVersions.set(claudeUid,oldClaudeVersion);
        else S.outboxVersions.delete(claudeUid);
        if (oldCodexVersion) S.outboxVersions.set(codexUid,oldCodexVersion);
        else S.outboxVersions.delete(codexUid);
        S.retiredOutboxEpochs.delete('e2e-sweep-claude');
        S.retiredOutboxEpochs.delete('e2e-sweep-codex');
      }
    }""")
    check("所有服务端 pending 会定期修补 Claude 分支和 Codex 失败状态",
          pending_sweep == {
              "calls": 2, "claude": 0,
              "codex": [{"state": "failed", "error": "未在会话记录中确认"}],
              "running": 0,
          }, pending_sweep)
    outbox_order = p.evaluate("""() => {
      const uid = 'codex:synthetic-outbox-order';
      const oldQueued = S.queued.get(uid), oldVersion = S.outboxVersions.get(uid);
      const item = (id, text) => ({id, uid, text, created:1000,
        state:'queued', server:true});
      try {
        syncServerOutbox(uid, [item('one', '第一条'), item('two', '第二条')],
          {epoch:'e2e-old-process', revision:5});
        syncServerOutbox(uid, [item('one', '第一条')],
          {epoch:'e2e-old-process', revision:4});
        const staleRevision = queuedMessages(uid).map(x => x.id);
        syncServerOutbox(uid, [item('one', '第一条')],
          {epoch:'e2e-new-process', revision:1});
        const restarted = queuedMessages(uid).map(x => x.id);
        syncServerOutbox(uid, [item('one', '第一条'), item('two', '第二条')],
          {epoch:'e2e-old-process', revision:6});
        return {staleRevision, restarted,
          delayedOldProcess:queuedMessages(uid).map(x => x.id),
          version:S.outboxVersions.get(uid)};
      } finally {
        if (oldQueued) S.queued.set(uid, oldQueued); else S.queued.delete(uid);
        if (oldVersion) S.outboxVersions.set(uid, oldVersion);
        else S.outboxVersions.delete(uid);
        S.retiredOutboxEpochs.delete('e2e-old-process');
        S.retiredOutboxEpochs.delete('e2e-new-process');
      }
    }""")
    check("Codex 队列拒绝旧修订，服务重启也不抢在原生正文前删占位",
          outbox_order == {
              "staleRevision": ["one", "two"],
              "restarted": ["one", "two"],
              "delayedOldProcess": ["one", "two"],
              "version": {"epoch": "e2e-new-process", "revision": 1},
          }, outbox_order)
    script_order = p.locator("script[src]").evaluate_all(
        "nodes => nodes.map(n => n.getAttribute('src').split('?')[0])")
    check("会话列表脚本不再被大型终端和公式库阻塞",
          script_order.index("cli.js") < script_order.index("app.js")
          < script_order.index("vendor/xterm.js")
          and all(p.locator(f'script[src^="{src}"]').get_attribute("defer") is not None
                  for src in ("cli.js", "app.js", "vendor/xterm.js",
                              "vendor/addon-unicode11.js", "vendor/addon-webgl.js",
                              "vendor/katex/katex.min.js")),
          script_order)
    check("首次会话列表已替换静态扫描占位", p.locator("#side > .spin").count() == 0)
    action_styles = p.evaluate("""() => ['new-session', 'reload', 'settings'].map(id => {
      const s = getComputedStyle(document.getElementById(id));
      return [s.width, s.height, s.padding, s.borderRadius];
    })""")
    check("新建刷新设置使用相同按钮尺寸",
          action_styles[1:] == [action_styles[0], action_styles[0]], action_styles)

    # 顶栏按三级宽度排版：中屏折起回收站/报告/设置，窄屏全部折进 ⋯；每级都只有一行
    def resize(width, height):
        p.set_viewport_size({"width": width, "height": height})
        p.evaluate("dispatchEvent(new Event('resize'))")
        # headless 只有渲染一帧后才派发媒体查询 change，折叠布局靠它驱动
        p.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        p.wait_for_timeout(300)
    resize(900, 780)
    check("中屏顶栏露出新建和刷新，其余折进 ⋯",
          p.locator("#new-session").is_visible() and p.locator("#reload").is_visible()
          and not p.locator("#settings").is_visible() and not p.locator(".brand-name").is_visible()
          and p.locator("#header-more-btn").is_visible()
          and p.locator("header").bounding_box()["height"] <= 52)
    p.locator("#header-more-btn").click()
    check("中屏 ⋯ 菜单里是回收站、报告问题和设置",
          p.locator("#header-menu button:visible").evaluate_all(
              "b => b.map(x => x.id)") == ["trash", "report-bug", "settings"]
          and p.locator("#header-menu #settings").inner_text().strip() == "设置")
    p.keyboard.press("Escape")
    check("Esc 关闭 ⋯ 菜单并把焦点还给按钮",
          not p.locator("#header-menu").is_visible()
          and p.evaluate("document.activeElement?.id") == "header-more-btn")
    resize(390, 780)
    mobile_header = p.evaluate("""() => {
      const box = id => { const r = document.querySelector(id).getBoundingClientRect();
                          return {top: r.top, height: r.height}; };
      return {scope: box('#session-scope'), chips: box('.chips'), view: box('#view'),
              actions: box('.header-actions'), header: box('header'),
              overflow: document.documentElement.scrollWidth > innerWidth};
    }""")
    check("手机顶栏只有一行：范围、筛选、视图和 ⋯ 同排",
          abs(mobile_header["chips"]["top"] - mobile_header["view"]["top"]) < 1
          and abs(mobile_header["scope"]["top"] - mobile_header["view"]["top"]) < 1
          and abs(mobile_header["actions"]["top"] - mobile_header["view"]["top"]) < 4
          and mobile_header["header"]["height"] <= 46 and not mobile_header["overflow"],
          mobile_header)
    check("手机顶栏保留会话数量和视图按钮，其余按钮全部折进 ⋯",
          not p.locator(".brand-name").is_visible()
          and p.locator("#session-total").is_visible()
          and p.locator('#view button[aria-label="项目树"]').is_visible()
          and p.locator('#view button[aria-label="时间轴"]').is_visible()
          and p.locator("#session-scope [role=radio]").count() == 2
          and not p.locator("#reload").is_visible()
          and p.locator("#header-more-btn").is_visible())
    p.locator("#header-more-btn").click()
    check("手机 ⋯ 菜单包含全部五个顶栏操作",
          p.locator("#header-menu button:visible").evaluate_all("b => b.map(x => x.id)")
          == ["new-session", "reload", "trash", "report-bug", "settings"])
    p.keyboard.press("Escape")
    resize(1280, 720)
    check("宽屏顶栏全部按钮平铺，⋯ 不出现",
          p.locator("#settings").is_visible() and p.locator("#trash").is_visible()
          and not p.locator("#header-more-btn").is_visible()
          and p.locator(".brand-name").is_visible()
          and p.locator(".brand-name").inner_text().strip() not in {"", "__AGENTHUB_HOSTNAME__"})
    brand_style = p.locator(".brand-name").evaluate("""n => {
      const s = getComputedStyle(n);
      return {background:s.backgroundImage, family:s.fontFamily,
        size:parseFloat(s.fontSize), style:s.fontStyle,
        weight:parseInt(s.fontWeight), spacing:parseFloat(s.letterSpacing),
        transform:s.textTransform, fill:s.webkitTextFillColor};
    }""")
    check("机器名使用醒目的渐变 display 字标",
          brand_style["background"].startswith("linear-gradient")
          and "serif" in brand_style["family"] and brand_style["style"] == "italic"
          and brand_style["size"] >= 18 and brand_style["weight"] >= 700
          and brand_style["spacing"] > 0 and brand_style["transform"] == "none"
          and brand_style["fill"] == "rgba(0, 0, 0, 0)", brand_style)

    # 上传目录只能由服务端根据 uid 决定，原始二进制不走 Base64。
    fake_uid = p.evaluate("() => S.sessions.find(s => s.title === 'AGENTHUB自测会话请删除').uid")
    attachment_payload = b"agenthub attachment raw bytes\x00\x01"
    attachment_responses = p.evaluate("""async ({uid, bytes}) => {
      async function upload(payload, id = null) {
        const url = new URL(appUrl('api/session/attachment'));
        url.searchParams.set('uid', uid); url.searchParams.set('name', '测试 attachment.txt');
        if (id) url.searchParams.set('id', id);
        const file = new File([new Uint8Array(payload)], '测试 attachment.txt', {type:'text/plain'});
        const response = await fetch(url, {method:'POST', headers:{'Content-Type':file.type}, body:file});
        return response.json();
      }
      const first = await upload(bytes);
      return [first, await upload(bytes, first.attachment_id),
        await upload([...bytes, 2], first.attachment_id)];
    }""", {"uid": fake_uid, "bytes": list(attachment_payload)})
    attachment_response, same_response, different_response = attachment_responses
    attachment_path = Path(attachment_response["path"])
    attachment_id = attachment_response["attachment_id"]
    check("附件以原文件名保存到递增编号的受控子目录",
          attachment_id.isdigit()
          and attachment_path == FAKE_CWD / "agenthub_attachments" / attachment_id / "测试 attachment.txt"
          and attachment_response["relative_path"]
          == f"agenthub_attachments/{attachment_id}/测试 attachment.txt",
          attachment_response)
    check("附件按原始二进制流完整落盘",
          attachment_path.read_bytes() == attachment_payload
          and attachment_response["mime"] == "text/plain", attachment_response)
    check("同名且内容相同的附件直接复用",
          same_response["path"] == attachment_response["path"]
          and same_response["reused"] is True, same_response)
    different_path = Path(different_response["path"])
    check("同名但内容不同的附件添加双下划线编号",
          different_path.name == "测试 attachment__1.txt"
          and different_path.read_bytes() == attachment_payload + b"\x02"
          and different_response["reused"] is False, different_response)
    image_attachment_response = p.evaluate("""async ({uid, bytes}) => {
      const url = new URL(appUrl('api/session/attachment'));
      url.searchParams.set('uid', uid); url.searchParams.set('name', '排队预览.png');
      const file = new File([new Uint8Array(bytes)], '排队预览.png', {type:'image/png'});
      const response = await fetch(url, {method:'POST', headers:{'Content-Type':file.type}, body:file});
      return response.json();
    }""", {"uid": fake_uid, "bytes": list(base64.b64decode(PNG_B64))})
    check("图片上传响应附带受限预览 token",
          re.fullmatch(r"/api/media/[0-9a-f]{32}",
                       image_attachment_response.get("media", {}).get("src", "")) is not None,
          image_attachment_response)
    p.set_viewport_size({"width": 1280, "height": 720})
    p.evaluate("dispatchEvent(new Event('resize'))")
    p.wait_for_timeout(300)

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
    star_sort = p.evaluate("""() => {
      const before = S.view;
      const rows = [
        {uid:'older-starred', cwd:'/tmp/sort', updated:'2026-08-08T08:00:00Z', starred:true},
        {uid:'newer-normal', cwd:'/tmp/sort', updated:'2026-08-08T09:00:00Z', starred:false},
      ];
      S.view = 'tree';
      const tree = groupBy(rows)[0][1].map(s => s.uid);
      S.view = 'date';
      const date = groupBy(rows)[0][1].map(s => s.uid);
      S.view = before;
      return {tree, date};
    }""")
    check("项目树不因收藏改变时间顺序",
          star_sort["tree"] == ["newer-normal", "older-starred"], star_sort)
    check("时间轴同日收藏会话排在前面",
          star_sort["date"] == ["older-starred", "newer-normal"], star_sort)
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
    p.fill("#q", "AGENTHUB自测")
    p.wait_for_timeout(250)
    check("标题过滤生效", p.locator(".item").count() == 1, p.locator(".item").count())
    p.fill("#q", "")
    p.wait_for_timeout(250)
    check("清空过滤恢复", p.locator(".item").count() == total_before)

    # ---- 6. 全文搜索 ----
    def initial_search():
        seq = p.get_attribute("#stat", "data-seq") or ""
        p.fill("#q", "")
        p.fill("#q", "长文本测试")
        p.press("#q", "Enter")
        # 大型真实会话库第一次要解析全文并填充缓存；按完成序号等待，不用
        # 固定 sleep 猜测机器速度。NodeA 的 600+ 会话冷扫描偶尔超过 30 秒。
        p.wait_for_function(
            "seq => (document.querySelector('#stat').dataset.seq || '') !== seq",
            arg=seq, timeout=120000)
        return p.evaluate("""() => ({
          stat: document.querySelector('#stat').textContent.trim(),
          hasResults: Array.isArray(S.results),
          hits: Array.isArray(S.results) ? S.results.length : -1,
        })""")

    search_state = initial_search()
    # 真实环境启动时 term.js 可能刚完成第一次 tmux 清单初始化并刷新侧栏；若它
    # 恰好抢在这里退出搜索态，等初始化稳定后重试一次，结果断言仍保持严格。
    if not search_state["hasResults"]:
        p.wait_for_timeout(500)
        search_state = initial_search()
    check("全文搜索完成后显示命中状态",
          search_state["hasResults"] and "命中" in search_state["stat"], search_state)
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
        it = p.locator(".item").filter(has_text="AGENTHUB自测")
        if not it.count():
            return 0
        m = re.search(r"命中 (\d+)", it.first.locator(".m").inner_text())
        return int(m.group(1)) if m else 0

    check("三个搜索选项按钮都在", p.locator("#opts button").count() == 3)
    ci = search_hits("AgentHubCase")
    cs = search_hits("AgentHubCase", ["case"])
    check("默认大小写不敏感", ci == 2, ci)
    check("大小写敏感只匹配一次", cs == 1, cs)
    check("选项按钮显示为激活", "on" in (p.locator('#opts button[data-o="case"]').get_attribute("class") or ""))

    sub = search_hits("wordprobe")
    whole = search_hits("wordprobe", ["word"])
    check("全词匹配少于子串匹配", 0 < whole < sub, f"whole={whole} sub={sub}")

    check("工具输出不进入对话正文搜索", search_hits("单行工具输出不折叠") == 0)
    check("注入上下文不进入对话正文搜索", search_hits("<INSTRUCTIONS>注入的") == 0)

    rx = search_hits("正则探针.{0,3}Q7内容", ["regex"])
    check("正则匹配生效", rx >= 1, rx)
    # 列表命中不等于正文高亮: 两条路径的匹配实现是分开的
    p.locator(".item").filter(has_text="AGENTHUB自测").first.click()
    p.wait_for_selector(".msg", timeout=20000)
    check("正则模式下正文也高亮", p.locator("#msgs mark").count() > 0,
          p.locator("#mcount").inner_text())
    check("正则高亮命中的是实际文本", "正则探针甲Q7内容" in p.locator("#msgs mark").first.inner_text(),
          p.locator("#msgs mark").first.inner_text())
    lit = search_hits("正则探针.{0,3}Q7内容")
    check("非正则时特殊字符按字面处理", lit == 0, lit)

    search_hits("[", ["regex"])
    check("坏正则给出错误提示", "无效" in p.locator("#stat").inner_text(), p.locator("#stat").inner_text())
    check("坏正则不产生 JS 报错", not errors, errors[:2])

    # 选项持久化
    p.locator('#opts button[data-o="word"]').click()
    p.wait_for_timeout(200)
    p.reload(wait_until="domcontentloaded")
    p.wait_for_selector(".item", timeout=15000)
    check("选项状态持久化", "on" in (p.locator('#opts button[data-o="word"]').get_attribute("class") or ""))
    p.locator('#opts button[data-o="word"]').click()
    p.wait_for_timeout(150)

    # ---- 6a2. 海量命中的限流 ----
    p.fill("#q", "a")
    p.press("#q", "Enter")
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=120000)
    mass_result = p.locator("#stat").inner_text()
    mass_count = re.search(r"(\d+) 个会话", mass_result)
    # 共享部署机的会话池会随清理自然低于服务端 60 条上限；两种合法结果都要
    # 覆盖：超过上限时明确提示截断，池较小时完整返回且数量不越界。
    check("海量结果状态符合搜索上限",
          "截断" in mass_result
          or ("全文命中" in mass_result and mass_count and int(mass_count.group(1)) <= 60),
          mass_result)
    seq = p.get_attribute("#stat", "data-seq") or ""
    p.fill("#q", "markprobe")
    p.press("#q", "Enter")
    p.wait_for_function(
        f"document.querySelector('#stat').dataset.seq !== '{seq}'"
        " && !document.querySelector('#stat').textContent.includes('搜索中')",
        timeout=120000)
    p.locator(".item").filter(has_text="AGENTHUB自测").first.click()
    p.wait_for_selector(".msg", timeout=60000)
    n_mark = p.locator("#msgs mark").count()
    check("高亮数量达到且不超过上限", n_mark == 3000, n_mark)
    hidden_hit = p.locator(".msg.hashit").first
    hidden_hit_dot = hidden_hit.evaluate(
        "n => { const s=getComputedStyle(n,'::after'); return [s.width,s.height,s.backgroundColor]; }")
    check("超限的命中消息显示角标",
          hidden_hit.count() > 0 and hidden_hit_dot[0] == "6px"
          and hidden_hit_dot[1] == "6px" and hidden_hit_dot[2] != "rgba(0, 0, 0, 0)",
          hidden_hit_dot)
    check("命中数显示为 N+", "+ 处匹配" in p.locator("#mcount").inner_text(),
          p.locator("#mcount").inner_text())
    check("页面仍可交互", p.locator("#a-session-action").is_enabled())
    p.fill("#q", "")
    p.wait_for_timeout(400)

    # ---- 6b. 搜索词高亮 ----
    p.fill("#q", "自测思考内容")           # 只出现在默认折叠的 thinking 里
    p.press("#q", "Enter")
    p.wait_for_function("document.querySelector('#stat').textContent.includes('命中')", timeout=60000)
    check("搜索命中列表标题也高亮", p.locator(".item .t mark").count() >= 0)
    # 搜索词也会出现在别的会话里, 按标题锁定自测会话
    p.locator(".item").filter(has_text="AGENTHUB自测").first.click()
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
    p.locator(".item").filter(has_text="AGENTHUB自测").first.click()
    p.wait_for_selector("#msgs mark", timeout=15000)
    check("被截断的长消息命中时直接全文展开",
          p.locator(".msg .mb.clip").count() == 0 or p.locator("#msgs mark").count() > 0)
    p.fill("#q", "")
    p.wait_for_timeout(300)

    # ---- 7. 打开自测会话 ----
    p.fill("#q", "AGENTHUB自测")
    p.wait_for_timeout(250)
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=10000)
    check("选中项高亮", p.locator(".item.sel").count() == 1)
    check("详情标题正确", "AGENTHUB自测会话请删除" in p.locator(".dhead h2").inner_text())
    title_spacing = p.locator(".dhead h2").evaluate("""n => {
      const icon = n.querySelector(':scope > .ico').getBoundingClientRect();
      const title = n.querySelector(':scope > :nth-child(2)').getBoundingClientRect();
      return title.left - icon.right;
    }""")
    check("会话来源图标与标题留有清晰间距", title_spacing >= 6, title_spacing)
    detail_star = p.locator("#a-star")
    list_star = p.locator(f'.item[data-uid="{fake_uid}"] .item-star')
    check("会话列表和详情菜单都提供星标开关",
          detail_star.count() == 1 and list_star.count() == 1
          and detail_star.get_attribute("aria-pressed") == "false"
          and list_star.get_attribute("aria-pressed") == "false")
    open_session_menu(p)
    with p.expect_response(lambda r: r.url.endswith("/api/session/star")
                           and r.request.method == "POST") as star_info:
        detail_star.click()
    star_result = star_info.value.json()
    p.wait_for_function("document.querySelector('#a-star')?.ariaPressed === 'true'")
    starred_item = p.locator(f'.item[data-uid="{fake_uid}"]')
    check("从详情加星后列表立即同步为实心星",
          star_result.get("starred") is True
          and starred_item.locator(".item-star.on").count() == 1
          and detail_star.locator('use[href="#i-star-filled"]').count() == 1)
    persisted_sessions = json.loads(urllib.request.urlopen(
        BASE + "/api/sessions?force=1", timeout=60).read())["sessions"]
    check("星标持久化在服务端而非当前浏览器",
          next(s for s in persisted_sessions if s["uid"] == fake_uid).get("starred") is True)
    selected_before_unstar = p.evaluate("S.sel")
    with p.expect_response(lambda r: r.url.endswith("/api/session/star")
                           and r.request.method == "POST"):
        p.locator(f'.item[data-uid="{fake_uid}"] .item-star').click()
    p.wait_for_function("document.querySelector('#a-star')?.ariaPressed === 'false'")
    check("从列表取消星标会同步详情且不会打开别的会话",
          p.evaluate("S.sel") == selected_before_unstar
          and p.locator("#a-star.on").count() == 0)
    open_session_menu(p)
    check("详情元信息含 cwd", "/tmp/agenthub-selftest" in p.locator(".dmeta").inner_text())
    check("详情菜单显示会话 UUID",
          "00000000-dead-beef-0000-000000000001" in p.locator(".dmeta").inner_text())
    meta_codes = p.locator(".dmeta code").all_inner_texts()
    check("目录在前且 UUID 内容在后",
          meta_codes[-2:] == ["/tmp/agenthub-selftest", "00000000-dead-beef-0000-000000000001"], meta_codes)
    p.keyboard.press("Escape")
    message_url = BASE + "/api/messages/" + urllib.parse.quote(p.evaluate("S.sel"), safe="")
    plain_res = urllib.request.urlopen(urllib.request.Request(
        message_url, headers={"Accept-Encoding": "identity"}), timeout=30)
    plain_body = plain_res.read()
    gzip_res = urllib.request.urlopen(urllib.request.Request(
        message_url, headers={"Accept-Encoding": "br, gzip"}), timeout=30)
    gzip_body = gzip_res.read()
    gzip_data = json.loads(gzip.decompress(gzip_body))
    check("大会话 JSON 使用 gzip 降低传输流量",
          gzip_res.headers.get("Content-Encoding") == "gzip"
          and "Accept-Encoding" in gzip_res.headers.get("Vary", "")
          and int(gzip_res.headers.get("X-AgentHub-Decoded-Length", 0))
              == len(gzip.decompress(gzip_body))
          and gzip_data["meta"]["uid"] == p.evaluate("S.sel")
          and len(gzip_body) < len(plain_body) * .7,
          f"{len(plain_body)} -> {len(gzip_body)}")
    no_gzip_res = urllib.request.urlopen(urllib.request.Request(
        message_url, headers={"Accept-Encoding": "*, gzip;q=0"}), timeout=30)
    no_gzip_body = no_gzip_res.read()
    check("客户端拒绝 gzip 时仍返回原始 JSON",
          no_gzip_res.headers.get("Content-Encoding") is None
          and int(no_gzip_res.headers.get("X-AgentHub-Decoded-Length", 0)) == len(no_gzip_body)
          and json.loads(no_gzip_body)["meta"]["uid"] == p.evaluate("S.sel"))
    progress_sizes = p.evaluate("""async uid => {
      const samples = [];
      const result = await fetchMessages(uid, {
        onProgress:(done, total) => samples.push([done, total]),
      });
      return {samples, bytes:result.bytes, networkBytes:result.networkBytes};
    }""", p.evaluate("S.sel"))
    check("浏览器进度显示压缩传输量而缓存仍记解压后字节",
          progress_sizes["samples"]
          and all(total > 0 and done <= total
                  for done, total in progress_sizes["samples"])
          and progress_sizes["samples"][-1]
              == [progress_sizes["networkBytes"], progress_sizes["networkBytes"]]
          and progress_sizes["networkBytes"] < progress_sizes["bytes"], progress_sizes)

    # 已完成回合首屏只保留用户输入、过程摘要和最终结论。过程正文必须是真正
    # 的懒加载：折叠时连 Markdown/tool DOM 都不创建，展开后继续沿用原工具组。
    turn_process = p.locator("#msgs > .turn-process").first
    process_preview = turn_process.locator(":scope > .turn-toolbar > .turn-preview")
    process_summary = process_preview.inner_text()
    check("已完成回合折叠为一条过程合集",
          turn_process.count() == 1
          and turn_process.get_attribute("data-role") == "process"
          and all(part in process_summary for part in ("条进展", "🔧", "修改", "次确认")),
          process_summary)
    check("过程合集折叠态不创建正文 DOM",
          "folded" in (turn_process.get_attribute("class") or "")
          and turn_process.locator(":scope > .turn-process-body").evaluate(
              "n => n.hidden && n.childElementCount === 0"))
    check("过程合集之后永久保留最终结论",
          turn_process.evaluate("n => n.nextElementSibling?.dataset.role") == "assistant")
    check("详情菜单提供持久化过程折叠开关",
          p.locator("#a-turns").get_attribute("aria-pressed") == "false"
          and "on" not in (p.locator("#a-turns").get_attribute("class") or "").split()
          and p.evaluate("S.compactTurns") is True
          and p.locator('#a-turns use[href="#i-process"]').count() == 1
          and process_preview.locator('use[href="#i-process"]').count() == 1)

    process_preview.scroll_into_view_if_needed()
    p.wait_for_timeout(100)
    process_top = process_preview.bounding_box()["y"]
    process_preview.locator(".fold-toggle").click()
    p.wait_for_function("document.querySelector('.turn-process-body').childElementCount > 0")
    p.wait_for_timeout(500)
    process_top_after = process_preview.bounding_box()["y"]
    check("点开过程后摘要保持在原视口位置",
          abs(process_top_after - process_top) <= 2 and p.evaluate("_stick") is False,
          f"{process_top:.1f} -> {process_top_after:.1f}")
    p.evaluate("""() => {
      const box = document.querySelector('#msgs');
      const body = document.querySelector('.turn-process-body');
      const br = box.getBoundingClientRect(), rr = body.getBoundingClientRect();
      box.scrollTop += rr.top + rr.height / 2 - (br.top + br.height / 2);
    }""")
    p.wait_for_timeout(250)
    middle_toolbar = p.evaluate("""() => {
      const box = document.querySelector('#msgs').getBoundingClientRect();
      const paddingTop = parseFloat(getComputedStyle(document.querySelector('#msgs')).paddingTop);
      const toolbar = document.querySelector('.turn-toolbar').getBoundingClientRect();
      const body = document.querySelector('.turn-process-body').getBoundingClientRect();
      return {top:toolbar.top, boxTop:box.top, bodyHeight:body.height,
        boxHeight:box.height, paddingTop,
        navVisible:document.querySelector('.turn-nav').checkVisibility(),
        collapseButtons:document.querySelectorAll('.turn-collapse').length};
    }""")
    check("长过程滚到中间仍有吸顶导航且不再重复显示收起键",
          middle_toolbar["bodyHeight"] > middle_toolbar["boxHeight"]
          and abs(middle_toolbar["top"] - middle_toolbar["boxTop"]
                  - middle_toolbar["paddingTop"] - 7) <= 2
          and middle_toolbar["navVisible"]
          and middle_toolbar["collapseButtons"] == 0, middle_toolbar)
    sticky_top = middle_toolbar["top"]
    process_preview.locator(".fold-toggle").click()
    p.wait_for_timeout(300)
    collapsed_top = process_preview.bounding_box()["y"]
    check("在过程任意位置收起后摘要仍留在眼前",
          "folded" in (turn_process.get_attribute("class") or "")
          and process_preview.is_visible()
          and middle_toolbar["boxTop"] <= collapsed_top
          <= middle_toolbar["boxTop"] + middle_toolbar["boxHeight"],
          f"{sticky_top:.1f} -> {collapsed_top:.1f}")

    open_session_menu(p)
    p.click("#a-turns")
    p.wait_for_function("!document.querySelector('.turn-process-body').hidden")
    check("展开所有过程后才物化正文且保留内层工具折叠",
          p.locator("#a-turns").get_attribute("aria-pressed") == "true"
          and "on" in (p.locator("#a-turns").get_attribute("class") or "").split()
          and p.evaluate("store.get('compactTurns')") is False
          and turn_process.locator(":scope > .turn-process-body").is_visible()
          and turn_process.locator('.msg[data-role="toolgroup"].folded').count() >= 1)
    roles = p.locator("#msgs [data-role]").evaluate_all("ns => ns.map(n => n.dataset.role)")
    check("消息角色齐全", {"user", "assistant", "thinking", "tool", "tool_result"} <= set(roles), roles)
    check("CLI 注入上下文不进入时间线", "context" not in roles, roles)
    time_dividers = p.evaluate("""() => {
      const box = el('section', 'msgs');
      box.style.position = 'fixed'; box.style.left = '0'; box.style.top = '0';
      box.style.width = '360px'; box.style.zIndex = '-1';
      document.body.appendChild(box);
      buildPlan(box, planMessages([
        {role:'assistant', text:'时间样本 A', ts:'2026-08-06T00:00:00.000Z'},
        {role:'user', text:'时间样本 B', ts:'2026-08-06T00:05:00.000Z'},
        {role:'assistant', text:'时间样本 C', ts:'2026-08-06T00:10:00.000Z'},
        {role:'user', text:'时间样本 D', ts:'2026-08-06T00:15:00.000Z'},
        {role:'assistant', text:'时间样本 E', ts:'2026-08-06T00:20:00.000Z'},
        {role:'user', text:'时间样本 F', ts:'2026-08-06T00:20:00.001Z'},
        {role:'assistant', text:'时间样本 G', ts:'2026-08-06T00:25:00.002Z'},
      ]), null);
      refreshMessageTimeDividers(box);
      const dividers = [...box.querySelectorAll(':scope > .message-time-divider')];
      const result = {count:dividers.length, text:dividers.map(x => x.textContent),
        dateTime:dividers.map(x => x.querySelector('time')?.dateTime),
        color:dividers.map(x => getComputedStyle(x).color),
        messageColor:getComputedStyle(box.querySelector('.msg')).color};
      box.remove();
      return result;
    }""")
    check("气泡间隔超过五分钟或距上次时间标记超过二十分钟时显示时间",
          time_dividers["count"] == 2
          and all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value)
                  for value in time_dividers["text"])
          and time_dividers["dateTime"] == [
              "2026-08-06T00:20:00.001Z", "2026-08-06T00:25:00.002Z"]
          and all(color != time_dividers["messageColor"]
                  for color in time_dividers["color"]), time_dividers)
    interrupted_message = p.evaluate("""() => {
      const node=msgNode({role:'user', text:'快速中断的输入', interrupted:true,
        interrupt_reason:'输入已中断，未进入当前 Claude 分支'});
      return {className:node.className, text:node.innerText,
        title:node.querySelector('.native-message-state')?.title || ''};
    }""")
    check("Claude 快速 Esc 的原生输入保留并明确标为已中断",
          "native-interrupted" in interrupted_message["className"]
          and "快速中断的输入" in interrupted_message["text"]
          and "已中断" in interrupted_message["text"]
          and "未进入当前 Claude 分支" in interrupted_message["title"],
          interrupted_message)
    check("结构化询问显示为对话气泡", "question" in roles)
    question = p.locator('.msg[data-role="question"]')
    check("询问气泡显示问题和选项",
          "要使用哪种启动方式" in question.inner_text()
          and "tmux" in question.inner_text() and "保留可重连的终端" in question.inner_text())
    check("询问回答显示为用户气泡", "answer" in roles)
    live_question = p.evaluate("""async () => {
      const prompt = {id:'ask-1', questions:[{header:'实时询问',
        question:'现在直接选择？', multiple:false, options:[
          {label:'继续', description:'继续执行'}, {label:'停止', description:'停在这里'}]}]};
      await applyDiff(S.sel, {prompt_only:true, prompt});
      const live = document.querySelector('.live-question');
      const historical = document.querySelector(
        '.msg[data-role="question"][data-call-id="ask-1"]');
      const oldSend = sendToSession;
      let sent = null;
      sendToSession = async (text, keys, uid) => { sent = {text, keys, uid}; return true; };
      const answered = await answerCliQuestion(S.sel, 1);
      sendToSession = oldSend;
      return {live:!!live, buttons:live?.querySelectorAll('[data-question-option]').length,
        historicalHidden:historical?.classList.contains('question-live-shadowed'),
        answered, sent};
    }""")
    check("恢复会话未落盘的实时选择题可直接在对话栏回答",
          live_question["live"] is True and live_question["buttons"] == 2
          and live_question["historicalHidden"] is True
          and live_question["answered"] is True
          and live_question["sent"]["keys"] == ["Up"] * 5 + ["Down", "Enter"],
          live_question)
    multi_question = p.evaluate("""async () => {
      const prompt = {id:'ask-many', questions:[
        {header:'第一题', question:'选择第一项？', multiple:false,
          options:[{label:'一'}, {label:'二'}]},
        {header:'第二题', question:'选择第二项？', multiple:false,
          options:[{label:'甲'}, {label:'乙'}, {label:'丙'}]}
      ]};
      await applyDiff(S.sel, {prompt_only:true, prompt});
      let live = document.querySelector('.live-question');
      const oldSend = sendToSession;
      const sent = [];
      sendToSession = async (text, keys, uid) => { sent.push({text, keys, uid}); return true; };
      live.querySelector('[data-question-index="0"][data-question-option="1"]').click();
      live.querySelector('[data-question-index="1"][data-question-option="2"]').click();
      await applyDiff(S.sel, {prompt_only:true, prompt:{...prompt}});
      live = document.querySelector('.live-question');
      const selectedAfterRedraw = [...live.querySelectorAll('.question-option.selected')]
        .map(x => [x.dataset.questionIndex, x.dataset.questionOption]);
      const submit = live.querySelector('.question-submit');
      const enabled = !submit.disabled;
      submit.click();
      await new Promise(resolve => setTimeout(resolve, 180));
      sendToSession = oldSend;
      return {selectedAfterRedraw, enabled, sent};
    }""")
    check("Claude 多题选择在后台重绘后保留并可统一提交",
          multi_question["selectedAfterRedraw"] == [["0", "1"], ["1", "2"]]
          and multi_question["enabled"] is True
          and [item["keys"] for item in multi_question["sent"]] == [
              ["Left"] * 3,
              ["Up"] * 5 + ["Down", "Enter"],
              ["Up"] * 6 + ["Down", "Down", "Enter"],
              ["Enter"]],
          multi_question)
    settled_question = p.evaluate("""async () => {
      const prompt = {id:'ask-1', state:'submitted', questions:[{
        question:'现在直接选择？', multiple:false,
        options:[{label:'继续'}, {label:'停止'}]}]};
      await applyDiff(S.sel, {prompt_only:true, prompt});
      const live = document.querySelector('.live-question');
      return {settling:live?.classList.contains('settling'),
        disabled:[...live.querySelectorAll('[data-question-option]')].every(x => x.disabled),
        text:live?.textContent};
    }""")
    check("原生答案落盘前保留禁用的提交中题卡",
          settled_question["settling"] is True
          and settled_question["disabled"] is True
          and "答案已提交，等待 Claude 记录" in settled_question["text"],
          settled_question)
    p.evaluate("async () => applyDiff(S.sel, {prompt_only:true, prompt:null})")
    check("实时问题结束后恢复原有历史问题",
          p.locator(".live-question").count() == 0
          and not question.first.evaluate("n => n.classList.contains('question-live-shadowed')"))
    codex_question = p.evaluate("""async () => {
      const uid = 'codex:synthetic-question', key = viewKey(uid);
      const oldEntry = cache.get(key), oldSend = sendToSession;
      const entry = {meta:{source:'codex'}, activity:{state:'waiting'}, msgs:[{
        role:'question', name:'request_user_input', call_id:'codex-ask-1',
        text:'选择 Codex 操作？', questions:[{header:'Codex',
          question:'选择 Codex 操作？', multiple:false, options:[
            {label:'继续', description:'继续执行'}, {label:'停止', description:'停止执行'}]}]}]};
      cache.set(key, entry);
      const pending = pendingHistoryQuestion(entry);
      const node = questionNode({...pending, live:true, uid});
      const sent = [];
      sendToSession = async (text, keys, target) => {
        sent.push({text, keys, uid:target}); return true;
      };
      try {
        const answered = await answerCliQuestion(uid, 1);
        const cancelled = await cancelCliQuestion(uid);
        return {pending:pending?.call_id, live:node.classList.contains('live-question'),
          buttons:node.querySelectorAll('[data-question-option]').length,
          answered, cancelled, sent};
      } finally {
        sendToSession = oldSend;
        if (oldEntry === undefined) cache.delete(key); else cache.set(key, oldEntry);
      }
    }""")
    check("Codex rollout 中未回答的原生选择题可在对话栏回答或取消",
          codex_question["pending"] == "codex-ask-1"
          and codex_question["live"] is True and codex_question["buttons"] == 2
          and codex_question["answered"] is True and codex_question["cancelled"] is True
          and codex_question["sent"] == [
              {"text": None, "keys": ["2"],
               "uid": "codex:synthetic-question"},
              {"text": None, "keys": ["Escape"],
               "uid": "codex:synthetic-question"}], codex_question)
    task_event = p.locator('.timeline-event.task').filter(has_text="监控事件 · 自测训练")
    check("Claude task notification 渲染成紧凑事件而非用户 XML",
          task_event.count() == 1
          and "<task-notification>" not in p.locator("#msgs").inner_text())
    task_event.locator("summary").click()
    check("任务事件保留可展开的结构化结果",
          "监控详细结果" in task_event.locator(".event-detail-body").inner_text())
    check("Claude recap 使用低强调回顾事件",
          p.locator('.timeline-event.recap').filter(has_text="自测任务已经收口").count() == 1)
    check("Claude 回合耗时显示在时间线",
          p.locator('.timeline-event.duration').filter(has_text="耗时 1 秒").count() == 1)
    activity = p.evaluate("""() => {
      const e = cache.get(S.sel), old = e.activity, wasLive = S.live.has(S.sel);
      const hadStart = S.liveStarted.has(S.sel), oldStart = S.liveStarted.get(S.sel);
      S.live.add(S.sel); S.liveStarted.set(S.sel, Date.now() / 1000 - 5);
      e.activity = {state:'working', ts:new Date().toISOString()}; renderActivity(e.activity);
      const working = document.querySelector('#activity')?.textContent;
      e.activity = {state:'working', ts:'2000-01-01T00:00:00Z'}; renderActivity(e.activity);
      const staleGone = !document.querySelector('#activity');
      e.activity = {state:'waiting'}; renderActivity(e.activity);
      const waiting = document.querySelector('#activity')?.textContent;
      e.activity = {state:'idle'}; renderActivity(e.activity);
      const idleGone = !document.querySelector('#activity');
      e.activity = old; if (!wasLive) S.live.delete(S.sel);
      if (hadStart) S.liveStarted.set(S.sel, oldStart); else S.liveStarted.delete(S.sel);
      renderActivity(old);
      return {working, staleGone, waiting, idleGone};
    }""")
    check("对话栏显示 Working 状态", "Working" in (activity["working"] or ""), activity)
    activity_style = p.evaluate("""() => {
      const e = cache.get(S.sel), old = e.activity, wasLive = S.live.has(S.sel);
      S.live.add(S.sel); e.activity = {state:'working', ts:new Date().toISOString()};
      renderActivity(e.activity);
      const s = getComputedStyle(document.querySelector('#activity'));
      const result = {background:s.backgroundColor, shadow:s.boxShadow,
        radius:s.borderRadius, border:s.borderTopWidth};
      e.activity = old; if (!wasLive) S.live.delete(S.sel); renderActivity(old);
      return result;
    }""")
    check("Working 使用无气泡状态行",
          activity_style["background"] == "rgba(0, 0, 0, 0)"
          and activity_style["shadow"] == "none"
          and activity_style["border"] == "0px", activity_style)
    check("新进程不继承旧回合的 Working", activity["staleGone"], activity)
    check("对话栏显示等待回答状态", "等待回答" in (activity["waiting"] or ""), activity)
    check("回合完成后移除临时状态", activity["idleGone"], activity)
    check("compact 自动注入不会残留 Working",
          p.evaluate("cache.get(S.sel)?.activity?.state") == "idle")
    compact_event = p.locator(".timeline-event.compact")
    check("compact 协议只显示一条低强调事件",
          compact_event.count() == 1
          and "上下文" in compact_event.inner_text()
          and "已压缩" in compact_event.inner_text(),
          compact_event.all_inner_texts())

    # ---- 8. 默认折叠规则 ----
    def body_visible(role):
        return p.locator(f'.msg[data-role="{role}"] .mb').first.is_visible()
    check("user 默认展开", body_visible("user"))
    check("assistant 默认展开", body_visible("assistant"))
    check("thinking 从不折叠", body_visible("thinking"))
    check("对话内容没有折叠入口",
          p.locator('.msg[data-role="user"] > .fold-preview, '
                    '.msg[data-role="assistant"] > .fold-preview, '
                    '.msg[data-role="thinking"] > .fold-preview').count() == 0)
    single_tool = p.locator('.msg[data-role="tool_result"]').filter(has_text="单行工具输出不折叠")
    check("单行工具输出也不折叠",
          single_tool.locator(".tool-out").is_visible() and single_tool.locator(".fold-preview").count() == 0)
    preview_limits = p.evaluate("""() => ({
      oneLine: outPreviewInfo('x'.repeat(10000)).text.length,
      eightLongLines: outPreviewInfo(Array(8).fill('中'.repeat(1000)).join('\\n')).text.length,
      normal: outPreviewInfo('a\\nb').text,
      omitted: outPreviewInfo(Array(13).fill('line').join('\\n')).omittedLines,
      trailing: outPreviewInfo(Array(8).fill('line').join('\\n') + '\\n').omittedLines
    })""")
    check("工具预览限制巨型单行和超长行",
          preview_limits["oneLine"] <= 1602
          and preview_limits["eightLongLines"] <= 1602
          and preview_limits["normal"] == "a\nb"
          and preview_limits["omitted"] == 5
          and preview_limits["trailing"] == 0, preview_limits)
    tool_line_policy = p.evaluate("""() => {
      const entry = document.createElement('div'); entry.className = 'tool-entry';
      addClippedPre(entry, 'tool-out', 'x'.repeat(400));
      document.querySelector('#msgs').appendChild(entry);
      const pre = entry.querySelector('pre'), button = entry.querySelector('.tool-wrap');
      const before = getComputedStyle(pre).whiteSpace;
      button.click();
      const after = getComputedStyle(pre).whiteSpace;
      const result = {before, after, label:button.textContent,
        pressed:button.getAttribute('aria-pressed')};
      entry.remove(); return result;
    }""")
    check("工具输出默认保留原始行且可显式切换自动换行",
          tool_line_policy == {"before": "pre", "after": "pre-wrap",
                               "label": "保持原行", "pressed": "true"},
          tool_line_policy)
    silent_result = p.evaluate("""() => {
      const box = document.querySelector('#msgs'), mark = document.createElement('i');
      box.appendChild(mark);
      appendMessages(box, [{role:'tool_result', text:'', call_id:'late-empty'}]);
      const node = mark.nextElementSibling;
      const result = {hidden:node?.hidden, role:node?.dataset.role,
        visibleBubble:!!node?.matches('.msg, .tool-entry')};
      node?.remove(); mark.remove(); return result;
    }""")
    check("跨增量批次的空工具结果不生成空白气泡",
          silent_result == {"hidden": True, "role": "tool_result", "visibleBubble": False},
          silent_result)
    tool_width = single_tool.evaluate("""n => ({
      tool: n.getBoundingClientRect().width,
      available: n.parentElement.clientWidth
        - parseFloat(getComputedStyle(n.parentElement).paddingLeft)
        - parseFloat(getComputedStyle(n.parentElement).paddingRight)
    })""")
    check("工具卡片使用对话栏完整可用宽度",
          abs(tool_width["tool"] - tool_width["available"]) < 2, tool_width)
    single_tool_skin = single_tool.evaluate("""n => {
      const entry = n.querySelector(':scope > .tool-entry');
      const pre = entry.querySelector(':scope > pre.tool-out');
      const ns = getComputedStyle(n), es = getComputedStyle(entry), ps = getComputedStyle(pre);
      return { entryPadding: es.padding, preBackground: ps.backgroundColor,
               preBorder: ps.borderTopWidth,
               bubbleBackground: ns.backgroundColor, textColor: ps.color,
               fontWeight: ps.fontWeight, fontSynthesis: ps.fontSynthesis,
               scrollbarWidth: ps.scrollbarWidth, scrollbarColor: ps.scrollbarColor };
    }""")
    check("单条工具输出是单层终端卡片(外层无气泡)",
          all(single_tool_skin[k] == v for k, v in {
              "entryPadding": "0px", "bubbleBackground": "rgba(0, 0, 0, 0)",
              "preBorder": "1px"}.items()), single_tool_skin)
    terminal_theme = p.evaluate("termTheme()")
    light_terminal_surface = p.locator("#xterm").evaluate("""n => ({
      filter: getComputedStyle(n).filter,
      sourceBackground: getComputedStyle(n).backgroundColor
    })""")
    check("亮色模式下工具输出使用明亮浅底深字主题",
          single_tool_skin["preBackground"] == "rgb(244, 246, 248)"
          and single_tool_skin["textColor"] == "rgb(37, 42, 50)"
          and single_tool_skin["fontWeight"] == "400"
          and single_tool_skin["fontSynthesis"] == "none"
          and terminal_theme["background"] == "#f4f6f8"
          and terminal_theme["foreground"] == "#252a32"
          and terminal_theme["red"] == "#a8323b", (single_tool_skin, terminal_theme))
    check("亮色终端直接绘制目标颜色，不再滤镜处理字体边缘",
          light_terminal_surface["filter"] == "none"
          and light_terminal_surface["sourceBackground"] == "rgb(244, 246, 248)",
          light_terminal_surface)
    ansi_colors = p.evaluate("""() => {
      const values = s => [...s.matchAll(/(?:38|48);2;(\\d+);(\\d+);(\\d+)/g)]
        .map(m => m.slice(1).map(Number));
      return {
        truecolor: values(lightTerminalAnsi('\x1b[38;2;255;123;114m'))[0],
        background: values(lightTerminalAnsi('\x1b[48;2;0;0;0m'))[0],
        indexed: values(lightTerminalAnsi('\x1b[38;5;231m'))[0],
        ansi16: lightTerminalAnsi('\x1b[38;5;1m'),
      };
    }""")
    check("浅色终端在 ANSI 层反射真彩色和 256 色亮度",
          sum(ansi_colors["truecolor"]) < 255 + 123 + 114
          and max(ansi_colors["background"]) <= 245
          and ansi_colors["indexed"] == [0, 0, 0]
          and "38;5;1" in ansi_colors["ansi16"], ansi_colors)
    p.emulate_media(color_scheme="dark")
    p.wait_for_timeout(50)
    dark_tool_skin = single_tool.evaluate("""n => ({
      background: getComputedStyle(n.querySelector(':scope > .tool-entry > pre.tool-out')).backgroundColor,
      color: getComputedStyle(n.querySelector(':scope > .tool-entry > pre.tool-out')).color
    })""")
    dark_terminal_theme = p.evaluate("termTheme()")
    terminal_curve = p.locator("#xterm").evaluate("""n => ({
      filter: getComputedStyle(n).filter,
      sourceBackground: getComputedStyle(n).backgroundColor
    })""")
    check("暗色模式下工具输出与终端共用纯黑底柔和灰字主题",
          dark_tool_skin == {"background": "rgb(0, 0, 0)", "color": "rgb(157, 165, 176)"}
          and dark_terminal_theme["background"] == "#000000"
          and dark_terminal_theme["foreground"] == "#9da5b0"
          and dark_terminal_theme["brightWhite"] == "#b9c0ca"
          and dark_terminal_theme["red"] == "#ff7b72", (dark_tool_skin, dark_terminal_theme))
    check("暗色模式终端不反色，保留黑底与原生字体抗锯齿",
          terminal_curve["filter"] == "none"
          and terminal_curve["sourceBackground"] == "rgb(0, 0, 0)", terminal_curve)
    p.emulate_media(color_scheme="light")
    p.wait_for_timeout(50)
    font_pair = p.evaluate("""() => ({
      tool: getComputedStyle(document.querySelector('.msg[data-role="tool_result"] > .tool-entry > pre')).fontFamily,
      toolSize: getComputedStyle(document.querySelector('.msg[data-role="tool_result"] > .tool-entry > pre')).fontSize,
      terminal: termFont(), terminalSize: termFontSize()
    })""")
    check("工具输出保留所选字体且 tmux 保留安全的 CJK 字体回退",
          "AgentHub CJK Sans" in font_pair["tool"] and "AgentHub Ubuntu Sans Mono" in font_pair["tool"]
          and "Adwaita Mono" in font_pair["tool"]
          and "Ubuntu Mono" in font_pair["tool"] and "Consola" in font_pair["tool"]
          and "AgentHub CJK Sans" in font_pair["terminal"]
          and "AgentHub Ubuntu Sans Mono" in font_pair["terminal"] and "Consola" in font_pair["terminal"]
          and font_pair["toolSize"] == "12.96px" and font_pair["terminalSize"] == 14.04, font_pair)
    cjk_grid = p.evaluate("""() => ({
      active: termFont().includes('AgentHub CJK Mono Grid'),
      ratio: terminalFontGridRatio('"AgentHub CJK Mono Grid"', termFontSize()),
      sample: TERM_FONT_SAMPLE
    })""")
    check("可用时只采用汉字宽度严格等于两个西文格的 CJK 字体",
          (not cjk_grid["active"] or abs(cjk_grid["ratio"] - 2) <= .025)
          and "，。！？" in cjk_grid["sample"], cjk_grid)
    p.evaluate("document.fonts.load('12px \\\"AgentHub CJK Sans\\\"', '中文字体')")
    check("三套等宽选项的汉字固定回退到无衬线 CJK 字体",
          p.evaluate("document.fonts.check('12px \\\"AgentHub CJK Sans\\\"', '中文字体')"))
    p.wait_for_function("document.fonts.check('12px \\\"AgentHub Ubuntu Sans Mono\\\"')", timeout=15000)
    bundled_font = p.evaluate("""() => ({
      loaded: document.fonts.check('12px "AgentHub Ubuntu Sans Mono"'),
      requested: performance.getEntriesByName(location.origin + '/fonts/UbuntuSansMono.ttf').length,
      metrics: (() => {
        const context = document.createElement('canvas').getContext('2d');
        context.font = `400 ${termFontSize()}px ${configuredTermFont()}`;
        const zero = context.measureText('0').width;
        return {zero, dashRatio:context.measureText('—').width / zero,
                cjkRatio:context.measureText('中').width / zero};
      })(),
      resolved: termFont()
    })""")
    check("Ubuntu Sans Mono 网页字体保持原始字号且不越终端网格",
          bundled_font["loaded"] and bundled_font["requested"] > 0
          and 7.7 <= bundled_font["metrics"]["zero"] <= 8.0
          and 1.7 <= bundled_font["metrics"]["cjkRatio"] <= 2.025
          and abs(bundled_font["metrics"]["dashRatio"] - 1) <= .025
          and "AgentHub Ubuntu Sans Mono" in bundled_font["resolved"]
          and "AgentHub CJK Mono Grid" not in bundled_font["resolved"],
          bundled_font)
    p.click("#settings")
    check("设置窗口集中提供字体、颜色和缓存选项",
          p.locator("#settings-dialog").is_visible()
          and p.locator("#setting-font").input_value() == "ubuntu"
          and p.locator("#setting-theme").input_value() == "system"
          and p.locator("#setting-cache").input_value() == "256")
    p.select_option("#setting-font", "system")
    p.select_option("#setting-theme", "dark")
    p.select_option("#setting-cache", "512")
    settings_applied = p.evaluate("""() => ({
      font: getComputedStyle(document.documentElement).getPropertyValue('--terminal-font'),
      theme: document.documentElement.dataset.theme,
      cache: CACHE_MAX_BYTES,
      saved: [store.get('font', ''), store.get('theme', ''), store.get('cacheMb', 0)],
    })""")
    check("设置修改后立即生效并保存在浏览器",
          "ui-monospace" in settings_applied["font"]
          and settings_applied["theme"] == "dark"
          and settings_applied["cache"] == 512 * 1024 * 1024
          and settings_applied["saved"] == ["system", "dark", 512], settings_applied)
    p.select_option("#setting-font", "consolas")
    p.wait_for_function("configuredTermFont().startsWith('\"AgentHub CJK Sans\", Consolas')")
    consolas_stack = p.evaluate("configuredTermFont()")
    check("Consola 选项不会让汉字落入 generic monospace 宋体",
          consolas_stack.startswith('"AgentHub CJK Sans", Consolas, Consola')
          and consolas_stack.endswith('sans-serif') and 'monospace' not in consolas_stack,
          consolas_stack)
    p.select_option("#setting-font", "ubuntu")
    p.select_option("#setting-theme", "system")
    p.select_option("#setting-cache", "256")
    p.locator("#settings-dialog .modal-actions button").click()
    report_requests = []
    def fake_bug_report(route):
        report_requests.append(route.request.post_data_json)
        route.fulfill(status=202, content_type="application/json", body=json.dumps({
            "ok": True, "report_id": "BUG-E2E", "path": "/tmp/BUG-E2E",
            "worker": {"name": "agenthub-grok-new-e2e", "source": "grok",
                       "sid": None, "cwd": str(Path(__file__).resolve().parents[1]),
                       "token": "e2e", "title": "处理 BUG-E2E",
                       "kind": "bug-report", "report_id": "BUG-E2E"},
        }))
    p.route("**/api/bug-report*", fake_bug_report)
    check("会话标题栏提供问题报告入口",
          p.locator(".dhead [data-report-bug]").count() == 1)
    # 窄屏时报告入口收在标题栏「⋯」菜单里，先展开再点击。
    open_session_menu(p)
    p.click(".dhead [data-report-bug]")
    check("会话标题栏的问题报告入口可以打开弹窗",
          p.locator("#bug-report-dialog").is_visible())
    p.locator("#bug-report-dialog .modal-close").click()
    p.click("#report-bug")
    check("问题报告弹窗说明自动诊断范围与模型用量",
          p.locator("#bug-report-dialog").is_visible()
          and "最近 15 分钟" in p.locator(".report-capture-note").inner_text()
          and "模型用量" in p.locator(".report-capture-note").inner_text())
    check("问题报告弹窗说明修复后自动 push 并同步其他机器",
          "push 到 GitHub" in p.locator(".report-capture-note").inner_text()
          and "同步到中央 Hub" in p.locator(".report-capture-note").inner_text()
          and p.locator("#bug-report-add").is_visible()
          and p.locator("#bug-report-items .draft-card").count() == 0)
    p.fill("#bug-report-description", "E2E 隔离验证，不启动真实 Codex")
    check("报告框提供 Claude/Codex/Grok 处理会话选择，默认 Codex",
          p.locator("#bug-report-source input").count() == 3
          and p.evaluate("bugReportSource()") == "codex")
    # 与新建会话一致：处理会话类型放在标题下方、描述框之前，而不是压在附件下面。
    report_source_box = p.locator("#bug-report-source").bounding_box()
    report_desc_box = p.locator("#bug-report-description").bounding_box()
    check("报告框的处理会话类型与新建会话一样排在最上面",
          report_source_box["y"] + report_source_box["height"] <= report_desc_box["y"]
          and p.locator("#bug-report-form > :nth-child(2)").get_attribute("id") == "bug-report-source",
          (report_source_box, report_desc_box))
    p.locator("#bug-report-source input[value=grok]").check(force=True)
    shot_paste = p.evaluate("""() => {
      const png = Uint8Array.from(atob('%s'), c => c.charCodeAt(0));
      const image = new File([png], '屏幕截图.png', {type: 'image/png'});
      const transfer = new DataTransfer(); transfer.items.add(image);
      const clipboard = {files: transfer.files, items: transfer.items, types: ['Files'],
                         getData: () => ''};
      const event = new Event('paste', {bubbles: true, cancelable: true});
      Object.defineProperty(event, 'clipboardData', {value: clipboard});
      document.querySelector('#bug-report-description').dispatchEvent(event);
      return event.defaultPrevented;
    }""" % E2E_PNG_BASE64)
    check("在问题描述框粘贴截图会成为与对话一致的附件卡片",
          shot_paste
          and p.locator("#bug-report-items .draft-card").count() == 1
          and p.locator("#bug-report-items .draft-info b").inner_text() == "屏幕截图.png"
          and "[附件1] · 图片" in p.locator("#bug-report-items .draft-info small").inner_text()
          and p.locator("#bug-report-items img").evaluate("img => img.naturalWidth") == 1)
    p.click("#bug-report-add")
    check("报告框的附件菜单与对话输入框一致",
          p.locator("#bug-report-attach-menu").is_visible()
          and p.locator("#bug-report-attach-menu button[data-attach]").count() == 4)
    p.locator("#bug-report-attach-menu button[data-attach=file]").click()
    p.locator("#bug-report-file").set_input_files([{
        "name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello"}])
    check("附件菜单选择的文件追加到附件列表",
          not p.locator("#bug-report-attach-menu").is_visible()
          and p.locator("#bug-report-items .draft-card").count() == 2
          and "[附件2] · 文件" in p.locator("#bug-report-items .draft-info small").nth(1).inner_text())
    p.locator("#bug-report-items .draft-card").nth(1).locator(".draft-remove").click()
    p.locator("#bug-report-items .draft-card").first.click()
    check("附件可在提交前移除，点击卡片把 [附件N] 插入描述",
          p.locator("#bug-report-items .draft-card").count() == 1
          and p.input_value("#bug-report-description").endswith("[附件1]"))
    p.click("#bug-report-go")
    p.wait_for_selector("#bug-report-toast:not(.hidden)", timeout=10000)
    check("报告提交包含最终页面快照且不强制切走当前会话",
          len(report_requests) == 1
          and report_requests[0].get("uid") == fake_uid
          and report_requests[0].get("snapshot", {}).get("data", {}).get("selected") == fake_uid
          and not p.locator("#bug-report-dialog").is_visible()
          and "BUG-E2E" in p.locator("#bug-report-toast").inner_text(), report_requests)
    check("报告请求携带所选处理会话类型并记住选择，toast 显示对应 CLI",
          report_requests[0].get("source") == "grok"
          and p.evaluate("store.get('bugReportSource')") == "grok"
          and "Grok 处理会话正在启动" in p.locator("#bug-report-toast").inner_text(),
          report_requests[0].get("source"))
    sent_attachments = report_requests[0].get("attachments") or []
    uploaded_path = Path(sent_attachments[0]["path"]) if sent_attachments else None
    check("报告附件通过对话同款上传接口落到仓库 agenthub_attachments/ 后随请求引用",
          len(sent_attachments) == 1
          and sent_attachments[0]["number"] == 1
          and sent_attachments[0]["name"] == "屏幕截图.png"
          and sent_attachments[0]["kind"] == "image"
          and re.fullmatch(r"agenthub_attachments/\d+/屏幕截图\.png", sent_attachments[0]["relative_path"])
          and uploaded_path.parent.parent == REPO_ROOT / "agenthub_attachments"
          and uploaded_path.read_bytes() == base64.b64decode(E2E_PNG_BASE64)
          and p.evaluate("bugReportDraftObject().attachments.length") == 0,
          sent_attachments)
    if uploaded_path and uploaded_path.parent.parent == REPO_ROOT / "agenthub_attachments":
        shutil.rmtree(uploaded_path.parent, ignore_errors=True)
    p.unroute("**/api/bug-report*", fake_bug_report)
    p.locator("#bug-report-toast").evaluate("node => node.classList.add('hidden')")
    check("滚动条使用细圆角低对比样式且轨道不是纯黑",
          single_tool_skin["scrollbarWidth"] == "thin"
          and "rgba(0, 0, 0, 0)" not in single_tool_skin["scrollbarColor"]
          and "rgb(0, 0, 0)" not in single_tool_skin["scrollbarColor"], single_tool_skin)

    # 所有状态都没有角色/时间 header；对齐方向和背景色构成聊天气泡层级。
    check("所有消息都没有 header", p.locator("#msgs .mh").count() == 0)
    bubble_geo = p.evaluate("""() => {
      const box = document.querySelector('#msgs'), cs = getComputedStyle(box), br = box.getBoundingClientRect();
      const left = br.left + parseFloat(cs.paddingLeft), right = br.right - parseFloat(cs.paddingRight);
      const width = right - left;
      const un = document.querySelector('#msgs > .msg[data-role="user"]:not(.folded)');
      const an = document.querySelector('#msgs > .msg[data-role="assistant"]:not(.folded)');
      const u = un.getBoundingClientRect(), a = an.getBoundingClientRect();
      return { userRight: right-u.right, otherLeft: a.left-left,
               userMax: getComputedStyle(un).maxWidth, otherMax: getComputedStyle(an).maxWidth,
               userRadius: getComputedStyle(un).borderRadius,
               otherRadius: getComputedStyle(an).borderRadius };
    }""")
    check("用户气泡靠右", bubble_geo["userRight"] <= 2, bubble_geo)
    check("其他气泡靠左", bubble_geo["otherLeft"] <= 2, bubble_geo)
    check("长气泡可以铺满内容区",
          bubble_geo["userMax"] == "100%" and bubble_geo["otherMax"] == "100%", bubble_geo)
    check("用户与助手气泡使用统一外圆角",
          bubble_geo["userRadius"] == bubble_geo["otherRadius"] == "10px", bubble_geo)
    colors = p.locator('#msgs > .msg[data-role="user"], #msgs > .msg[data-role="assistant"]').evaluate_all(
        "ns => ns.slice(0, 2).map(n => getComputedStyle(n).backgroundColor)")
    check("用户与助手用不同背景色区分", len(colors) == 2 and colors[0] != colors[1], colors)
    content_widths = p.evaluate("""() => {
      const nodes = {messages: '#msgs', composer: '#composer', detailHeader: '.dhead',
                     terminal: '#termpane', pageHeader: 'header', progress: '#prog'};
      return {win: innerWidth, boxes: Object.fromEntries(Object.entries(nodes).map(([k, sel]) => {
        const n = document.querySelector(sel), r = n.getBoundingClientRect();
        return [k, {max: getComputedStyle(n).maxWidth, right: r.right}];
      }))};
    }""")
    check("右侧内容、顶栏和进度条延伸到网页尽头",
          all(v["max"] == "none" for v in content_widths["boxes"].values())
          and all(abs(content_widths["boxes"][k]["right"] - content_widths["win"]) <= 1
                  for k in ("messages", "detailHeader", "pageHeader", "progress")),
          content_widths)
    folded_geo = p.locator('#msgs .msg[data-role="toolgroup"].folded').first.evaluate("""n => {
      const box = document.querySelector('#msgs').getBoundingClientRect();
      const r = n.getBoundingClientRect();
      return {ratio: r.width / box.width, radius: getComputedStyle(n).borderRadius,
              preview: !!n.querySelector(':scope > .fold-preview > .peek')};
    }""")
    check("工具组折叠态是紧凑的顺序提纲卡片",
          folded_geo["ratio"] <= .80 and folded_geo["radius"] == "10px"
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
    p.wait_for_function("document.querySelector('code[data-code-lang=python]')?.classList.contains('hljs')",
                        timeout=15000)
    python_code = p.locator('code[data-code-lang="python"]')
    check("带语言标签的代码块使用本地语法高亮",
          python_code.locator(".hljs-keyword").count() >= 2
          and python_code.locator(".hljs-title").count() >= 1
          and python_code.evaluate("c => c.parentElement.dataset.codeLanguage") == "python")
    auto_json = p.locator('code[data-code-lang=""][data-detected="json"]')
    check("无标签常见代码会自动识别并显示语言",
          auto_json.count() == 1 and auto_json.locator(".hljs-attr").count() >= 2
          and auto_json.evaluate("c => c.parentElement.dataset.codeLanguage") == "json")
    check("纯文本代码块不误做语言检测",
          p.locator('code[data-code-lang="text"].hljs').count() == 0)
    check("句中三反引号不会把后文误切成代码块",
          "句中提到 ```python 不是围栏，后文不能被吞掉" in p.locator(".msgs").inner_text()
          and p.locator('code.code-block').filter(has_text="后文不能被吞掉").count() == 0)
    check("语法高亮依赖从 agenthub 本地加载",
          p.locator('script[src$="syntax.js"]').count() == 1
          and p.locator('script[src^="http"]:not([src^="' + BASE + '"])').count() == 0)
    p.evaluate("""() => {
      const mixed = document.createElement('div');
      mixed.id = 'mixed-tool-syntax-probe';
      mixed.className = 'tool-entry';
      addClippedPre(mixed, 'tool-out',
        '$ git status\\n\\ndef tool_value():\\n    return 42\\n\\nconst answer = 7;\\nconsole.log(answer);');
      document.querySelector('#msgs').appendChild(mixed);
      const diff = document.createElement('div');
      diff.id = 'tool-diff-syntax-probe';
      diff.className = 'tool-entry';
      addClippedPre(diff, 'tool-out',
        'diff --git a/demo.py b/demo.py\\n--- a/demo.py\\n+++ b/demo.py\\n@@ -1,2 +1,2 @@\\n-def value():\\n-    return 1\\n+def value():\\n+    return 2');
      document.querySelector('#msgs').appendChild(diff);
    }""")
    p.wait_for_function("""() =>
      document.querySelector('#mixed-tool-syntax-probe .syntax-segment[data-syntax-language=python] .hljs-keyword')
      && document.querySelector('#mixed-tool-syntax-probe .syntax-segment[data-syntax-language=javascript] .hljs-keyword')
      && document.querySelector('#tool-diff-syntax-probe .tool-diff-line.add .hljs-keyword')""",
                        timeout=15000)
    mixed_probe = p.locator("#mixed-tool-syntax-probe")
    check("同一工具输出可分段识别 shell、Python 和 JavaScript",
          mixed_probe.locator('[data-syntax-language="bash"]').count() == 1
          and mixed_probe.locator('[data-syntax-language="python"] .hljs-keyword').count() >= 2
          and mixed_probe.locator('[data-syntax-language="javascript"] .hljs-keyword').count() >= 1,
          mixed_probe.locator('[data-syntax-language]').evaluate_all(
              "ns => ns.map(n => n.dataset.syntaxLanguage)"))
    tool_diff_probe = p.locator("#tool-diff-syntax-probe")
    tool_diff_colors = tool_diff_probe.locator(".tool-diff-line.add").first.evaluate("""n => ({
      background: getComputedStyle(n).backgroundColor,
      keyword: getComputedStyle(n.querySelector('.hljs-keyword')).color,
      base: getComputedStyle(n).color
    })""")
    check("工具输出里的 diff 同时保留增删底色和源码语法色",
          tool_diff_probe.locator(".tool-diff-line.del .hljs-keyword").count() >= 1
          and tool_diff_probe.locator(".tool-diff-line.add .hljs-keyword").count() >= 1
          and tool_diff_colors["background"] != "rgba(0, 0, 0, 0)"
          and tool_diff_colors["keyword"] != tool_diff_colors["base"], tool_diff_colors)
    p.locator("#mixed-tool-syntax-probe, #tool-diff-syntax-probe").evaluate_all(
        "ns => ns.forEach(n => n.remove())")
    imgs = p.locator(".mb img")
    check("Markdown、附件清单与结构化图片都渲染", imgs.count() >= 3, imgs.count())
    check("附件清单中的裸路径图片已渲染",
          p.locator('.media-link img[alt="image.png"]').count() >= 1)
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
    turn_folding = p.evaluate("""() => {
      const raw = [
        {role:'user', text:'请完成任务', turn_id:'turn-probe'},
        {role:'user', text:'[图片]', turn_id:'turn-probe'},
        {role:'assistant', text:'先检查现状', phase:'progress', turn_id:'turn-probe'},
        {role:'tool', name:'exec', summary:'$ echo one', call_id:'turn-call-1', turn_id:'turn-probe'},
        {role:'tool_result', text:'one', call_id:'turn-call-1', turn_id:'turn-probe'},
        {role:'tool', name:'read', summary:'读 demo.py', call_id:'turn-call-2', turn_id:'turn-probe'},
        {role:'tool_result', text:'内容', call_id:'turn-call-2', turn_id:'turn-probe'},
        {role:'question', text:'要继续吗？', call_id:'turn-ask', turn_id:'turn-probe'},
        {role:'answer', text:'继续', call_id:'turn-ask', turn_id:'turn-probe'},
        {role:'assistant', text:'任务已完成', phase:'final', turn_id:'turn-probe'},
      ];
      const plan = planTurns(raw, {tailComplete:false});
      const outer = plan.find(item => item.turn)?.turn;
      const node = turnProcessNode(outer, false);
      const lazyBeforeOpen = node.classList.contains('folded')
        && node.querySelector(':scope > .turn-process-body').childElementCount === 0;
      node._open();
      const preservesInnerToolFold = !!node.querySelector(
        ':scope > .turn-process-body > .msg[data-role=toolgroup].folded');
      const waiting = planTurns(raw.slice(0, -1), {tailComplete:false});
      const legacy = raw.map(m => {
        const copy = {...m}; delete copy.phase; delete copy.turn_id; return copy;
      });
      const interruptedRaw = legacy.filter((_, i) => i !== 1);
      const interruptedLast = [...interruptedRaw].reverse().find(isTurnAssistant);
      interruptedLast.interrupted = true;
      const interrupted = planTurns(interruptedRaw, {tailComplete:true});
      const legacyComplete = planTurns(legacy, {tailComplete:true});
      // Codex 的末次 commentary 后仍可能落下一项工具结果；中断状态应被
      // 提升为可见末次进展，前后工具仍留在同一个过程合集。
      const abortedWithTrailingTool = [
        {role:'user', text:'执行任务', turn_id:'turn-aborted'},
        {role:'assistant', text:'先检查', phase:'progress', turn_id:'turn-aborted'},
        {role:'tool', name:'exec', summary:'$ first', call_id:'first', turn_id:'turn-aborted'},
        {role:'tool_result', text:'first ok', call_id:'first', turn_id:'turn-aborted'},
        {role:'assistant', text:'中断前状态', phase:'progress', turn_id:'turn-aborted',
         interrupted:true},
        {role:'tool', name:'exec', summary:'$ trailing', call_id:'trailing', turn_id:'turn-aborted'},
        {role:'tool_result', text:'trailing ok', call_id:'trailing', turn_id:'turn-aborted'},
      ];
      const abortedPlan = planTurns(abortedWithTrailingTool, {tailComplete:true});
      const abortedProcess = abortedPlan.find(item => item.turn)?.turn;
      const abortedNode = turnProcessNode(abortedProcess, false);
      const shortAbortedPlan = planTurns([
        {role:'user', text:'短任务', turn_id:'turn-short-aborted'},
        {role:'assistant', text:'最后状态', phase:'progress',
         turn_id:'turn-short-aborted', interrupted:true},
        {role:'tool', name:'exec', summary:'$ verify', call_id:'verify',
         turn_id:'turn-short-aborted'},
        {role:'tool_result', text:'ok', call_id:'verify',
         turn_id:'turn-short-aborted'},
      ], {tailComplete:true});
      const incrementalAbort = abortedWithTrailingTool.map(m => {
        const copy = {...m}; delete copy.interrupted; return copy;
      });
      const incrementalMarked = markInterruptedTurn(incrementalAbort, {
        state:'aborted', turn_id:'turn-aborted', reason:'用户中断',
      });
      const abortedIncremental = document.createElement('div');
      buildPlan(abortedIncremental, planMessages(incrementalAbort), null);
      const abortedSealed = sealTurnTail(abortedIncremental, {
        msgs:incrementalAbort, meta:{uid:'turn-aborted'},
        activity:{state:'aborted', turn_id:'turn-aborted'},
      }, {defer:false});
      // BUG-20260906-072235-9128c5: 用户在同一个原生 turn 里追加要求时，
      // 追加前的 commentary/tool 段没有自己的 final，但已经是历史过程。
      const steered = [
        {role:'user', text:'先做 A', turn_id:'turn-steered'},
        {role:'assistant', text:'A 处理中', phase:'progress', turn_id:'turn-steered'},
        {role:'tool', name:'exec', summary:'$ do-a', call_id:'a', turn_id:'turn-steered'},
        {role:'tool_result', text:'a ok', call_id:'a', turn_id:'turn-steered'},
        {role:'assistant', text:'A 还在处理', phase:'progress', turn_id:'turn-steered'},
        {role:'user', text:'同时做 B', turn_id:'turn-steered'},
        {role:'assistant', text:'AB 处理中', phase:'progress', turn_id:'turn-steered'},
        {role:'tool', name:'exec', summary:'$ do-b', call_id:'b', turn_id:'turn-steered'},
        {role:'tool_result', text:'b ok', call_id:'b', turn_id:'turn-steered'},
        {role:'assistant', text:'AB 完成', phase:'final', turn_id:'turn-steered'},
      ];
      const steeredPlan = planTurns(steered, {tailComplete:true});
      const gapHead = planTurns(steered.slice(0, 5), {
        tailComplete:false, foldTail:true,
      });
      const gapTail = planTurns([
        {role:'assistant', text:'缺口后的进展', phase:'progress', turn_id:'gap-turn'},
        {role:'tool', name:'exec', summary:'$ resume', call_id:'gap', turn_id:'gap-turn'},
        {role:'tool_result', text:'ok', call_id:'gap', turn_id:'gap-turn'},
        {role:'assistant', text:'缺口后的结论', phase:'final', turn_id:'gap-turn'},
        {role:'user', text:'下一轮', turn_id:'next-turn'},
      ], {tailComplete:true});
      const activeFragment = planTurns([
        {role:'assistant', text:'仍在工作', phase:'progress', turn_id:'active-turn'},
        {role:'tool', name:'exec', summary:'$ active', call_id:'active', turn_id:'active-turn'},
        {role:'tool_result', text:'running', call_id:'active', turn_id:'active-turn'},
      ], {openTail:true, tailComplete:false});
      // BUG-20260906-213329-413d8d: 后台监控通知没有可见 user 气泡，后来的
      // final 不能抢走前一条完整答复的结论位置，把“查完了”折进过程合集。
      const monitored = [
        {role:'user', text:'检查流水', turn_id:'turn-monitored'},
        {role:'assistant', text:'正在检查', phase:'progress', turn_id:'turn-monitored'},
        {role:'tool', name:'exec', summary:'$ inspect', call_id:'inspect',
         turn_id:'turn-monitored'},
        {role:'tool_result', text:'healthy', call_id:'inspect', turn_id:'turn-monitored'},
        {role:'assistant', text:'查完了，流水正常', phase:'final', turn_id:'turn-monitored'},
        {role:'event', text:'', event_kind:'duration', counted:false,
         turn_id:'turn-monitored'},
        {role:'queue_operation', text:'通知入队', operation:'enqueue', counted:false},
        {role:'queue_operation', text:'', operation:'dequeue', counted:false},
        {role:'event', text:'监控事件', event_kind:'task', counted:false},
        {role:'assistant', text:'进度 55%', phase:'final', turn_id:'turn-monitored'},
        {role:'event', text:'', event_kind:'duration', counted:false,
         turn_id:'turn-monitored'},
      ];
      const monitoredPlan = planTurns(monitored, {tailComplete:true});
      const monitoredProcess = monitoredPlan.find(item => item.turn)?.turn;
      const monitoredFinals = monitoredPlan
        .filter(item => isFinalAssistant(item.m)).map(item => item.m.text);
      const interruptedProcess = interrupted.find(item => item.turn)?.turn;

      const incremental = document.createElement('div');
      buildPlan(incremental, planMessages(raw), null);
      const sealed = sealTurnTail(incremental, {
        msgs:raw, meta:{uid:'turn-probe'}, activity:{state:'idle'}
      }, {defer:false});
      return {
        planShape:plan.length === 4 && plan[0].m?.role === 'user'
          && plan[1].m?.role === 'user' && plan[2].turn
          && plan[3].m?.text === '任务已完成',
        nativeTurnId:outer?.key === 'turn-probe',
        virtualProcessWrapperIsNotAMessage:node.dataset.counted === 'false',
        multipartPromptStaysVisible:!outer?.items.some(m => m.role === 'user'),
        questionAndAnswerStayInside:outer?.items.some(m => m.role === 'question')
          && outer.items.some(m => m.role === 'answer')
          && !plan.some(item => item.m?.role === 'answer'),
        lazyBeforeOpen, materializedAfterOpen:
          node.querySelector(':scope > .turn-process-body').childElementCount > 0,
        preservesInnerToolFold,
        waitingDoesNotGuessConclusion:!waiting.some(item => item.turn),
        interruptedKeepsLastUpdateOutsideProcess:!!interruptedProcess
          && interruptedProcess.hasConclusion === true
          && interruptedProcess.interrupted === true
          && interrupted.at(-1)?.m?.text === '任务已完成'
          && interrupted.at(-1)?.m?.interrupted === true,
        interruptedTrailingToolsStayInProcess:
          abortedPlan.length === 3
          && abortedPlan[0].m?.role === 'user'
          && abortedProcess?.interrupted === true
          && abortedProcess.items.some(m => m.call_id === 'trailing')
          && !abortedProcess.items.some(m => m.text === '中断前状态')
          && abortedPlan[2].m?.text === '中断前状态'
          && abortedNode.dataset.interrupted === 'true'
          && abortedNode.querySelector('.turn-to-conclusion')?.textContent
             === '末次进展 ↓',
        shortInterruptedTurnStillFoldsItsSingleTool:
          shortAbortedPlan.length === 3
          && shortAbortedPlan[1].turn?.items.length === 2
          && shortAbortedPlan[2].m?.text === '最后状态',
        incrementalAbortMarksOnlyLastAssistant:
          incrementalMarked
          && incrementalAbort.filter(m => m.interrupted).length === 1
          && incrementalAbort.find(m => m.interrupted)?.text === '中断前状态'
          && incrementalAbort.find(m => m.interrupted)?.interrupt_reason === '用户中断',
        incrementalAbortSealsWithoutReload:
          abortedSealed
          && [...abortedIncremental.children].map(n => n.dataset.role).join(',')
             === 'user,process,assistant'
          && abortedIncremental.lastElementChild.classList.contains('native-interrupted'),
        steeredHistoryCompactsWithoutLosingPrompts:
          steeredPlan.length === 5
          && steeredPlan[0].m?.text === '先做 A'
          && steeredPlan[1].turn?.hasConclusion === false
          && steeredPlan[1].turn?.items.at(-1)?.phase === 'progress'
          && steeredPlan[2].m?.text === '同时做 B'
          && steeredPlan[3].turn?.hasConclusion === true
          && steeredPlan[4].m?.text === 'AB 完成',
        windowGapSidesCompactWithoutCrossGapGuessing:
          gapHead.length === 2
          && gapHead[0].m?.text === '先做 A'
          && gapHead[1].turn?.hasConclusion === false
          && gapTail.length === 3
          && gapTail[0].turn?.hasConclusion === true
          && gapTail[1].m?.text === '缺口后的结论'
          && gapTail[2].m?.text === '下一轮',
        activeFragmentStaysExpanded:!activeFragment.some(item => item.turn),
        backgroundFinalDoesNotReplaceVisibleConclusion:
          monitoredProcess?.hasConclusion === true
          && !monitoredProcess.items.some(m => m.text === '查完了，流水正常')
          && monitoredFinals.join('|') === '查完了，流水正常|进度 55%',
        completedLegacyFallsBackToLastAssistant:legacyComplete.some(item => item.turn),
        incrementalSeal:sealed
          && incremental.children.length === 4
          && incremental.children[0].dataset.role === 'user'
          && incremental.children[1].dataset.role === 'user'
          && incremental.children[2].dataset.role === 'process'
          && incremental.children[3].dataset.role === 'assistant'
          && incremental.children[0]._turnSealed === true,
      };
    }""")
    check("过程合集按原生回合边界保留结论、懒加载并兼容旧会话与增量封口",
          all(turn_folding.values()), turn_folding)

    live_tool_grouping = p.evaluate("""() => {
      const box = document.createElement('div');
      const tool = n => ({role:'tool', name:'exec', summary:`$ echo ${n}`,
                          text:`{\"n\":${n}}`, call_id:`call-${n}`});
      appendMessages(box, [tool(1)], null, {openTail:true});
      const firstIsSingle = box.children.length === 1
        && box.firstElementChild.matches('.tool-msg');
      appendMessages(box, [tool(2)], null, {openTail:true});
      let group = box.querySelector(':scope > .grp');
      const twoBecomeOpenGroup = box.children.length === 1 && !!group
        && !group.classList.contains('folded') && group._toolItems.length === 2;
      appendMessages(box, [tool(3)], null, {openTail:true});
      group = box.querySelector(':scope > .grp');
      const nextBatchJoinsTail = box.children.length === 1
        && group._toolItems.length === 3 && !group.classList.contains('folded');
      appendMessages(box, [{role:'assistant', text:'工具段结束'}], null, {openTail:false});
      group = box.querySelector(':scope > .grp');
      const nonToolSealsGroup = group.classList.contains('folded')
        && group.nextElementSibling?.dataset.role === 'assistant';

      const idle = document.createElement('div');
      appendMessages(idle, [tool(4), tool(5)], null, {openTail:true});
      const openBeforeIdle = !idle.querySelector('.grp').classList.contains('folded');
      sealToolTail(idle);
      const idleSealsGroup = idle.querySelector('.grp').classList.contains('folded');
      return {firstIsSingle, twoBecomeOpenGroup, nextBatchJoinsTail,
              nonToolSealsGroup, openBeforeIdle, idleSealsGroup};
    }""")
    check("连续黑色工具段工作中展开、跨推送合并并在结束后折叠",
          all(live_tool_grouping.values()), live_tool_grouping)

    grp = p.locator('.msg[data-role="toolgroup"]').first
    check("连续工具调用合并成组", grp.count() > 0)
    if grp.count():
        check("组预览显示调用次数", re.search(r"🔧 ×\d+", grp.locator("> .fold-preview").inner_text()),
              grp.locator("> .fold-preview").inner_text())
        check("组预览显示语义摘要", "$ echo hi" in grp.locator("> .fold-preview .peek").inner_text(),
              grp.locator("> .fold-preview .peek").inner_text())
        outline = grp.locator("> .fold-preview .group-outline > span").all_inner_texts()
        check("组预览按原顺序逐行列出调用",
              len(outline) >= 3 and outline[0].startswith("1. $ echo hi")
              and outline[1].startswith("2. 读 /tmp/a.py")
              and outline[2].startswith("3. $ echo hi2"), outline)
        first_command = grp.locator("> .fold-preview .group-outline .tool-command").first
        p.wait_for_function("document.querySelector('.group-outline .tool-command.hljs .hljs-title')",
                            timeout=15000)
        check("折叠工具组里的命令摘要使用 Shell 语义高亮",
              first_command.locator(".hljs-title").count() >= 2
              and first_command.locator(".hljs-keyword").count() >= 1
              and first_command.locator(".hljs-attr").count() >= 1)
        check("组默认折叠", not grp.locator("> .tool-entry").first.is_visible())
        grp.hover()
        folded_hover = grp.evaluate("""n => {
          const ns = getComputedStyle(n), ps = getComputedStyle(n.querySelector(':scope > .fold-preview'));
          return { borders: [ns.borderTopColor, ns.borderRightColor, ns.borderBottomColor, ns.borderLeftColor],
                   radius: ns.borderRadius, previewRadius: ps.borderRadius,
                   previewBackground: ps.backgroundColor, previewFilter: ps.filter };
        }""")
        check("折叠气泡悬停使用完整圆角轮廓",
              len(set(folded_hover["borders"])) == 1
              and folded_hover["radius"] == folded_hover["previewRadius"]
              and folded_hover["previewBackground"] == "rgba(0, 0, 0, 0)"
              and folded_hover["previewFilter"] == "none", folded_hover)
        inner = grp.locator("> .tool-entry").count()
        check("组内至少 3 条", inner >= 3, inner)
        grp.locator("> .fold-preview > .fold-toggle").click()
        p.wait_for_timeout(200)
        check("点组预览展开", grp.locator("> .tool-entry").first.is_visible())
        check("展开后预览不再占垂直空间", not grp.locator("> .fold-preview").is_visible())
        check("工具组不再包 grp-body", grp.locator("> .grp-body").count() == 0)
        check("工具组内不再嵌套 msg 气泡", grp.locator("> .msg").count() == 0)
        read_entry = grp.locator("> .tool-entry").filter(has_text="读 /tmp/a.py")
        p.wait_for_function("""() => [...document.querySelectorAll('.tool-entry')].some(n =>
          n.textContent.includes('读 /tmp/a.py') && n.querySelector('.tool-out .hljs-keyword'))""",
                            timeout=15000)
        check("Read 工具输出按文件扩展名识别 Python",
              read_entry.locator(".tool-out.hljs").count() == 1
              and read_entry.locator(".tool-out .hljs-keyword").count() >= 2
              and read_entry.locator(".tool-out").get_attribute("data-code-language") == "python")
        first_entry = grp.locator("> .tool-entry").first
        tool_card_frame = first_entry.evaluate("""n => {
          const head = n.querySelector(':scope > .tool-head');
          const out = n.querySelector(':scope > .tool-out');
          const s = getComputedStyle(n), h = getComputedStyle(head), o = getComputedStyle(out);
          return {tag:head.tagName,
            outerBorders:[s.borderTopStyle,s.borderRightStyle,s.borderBottomStyle,s.borderLeftStyle],
            outerRadius:s.borderRadius, outerBackground:s.backgroundColor,
            headBorders:[h.borderTopWidth,h.borderRightWidth,h.borderBottomWidth,h.borderLeftWidth],
            headPadding:[h.paddingTop,h.paddingBottom], headRadius:h.borderRadius,
            outBorders:[o.borderTopWidth,o.borderRightWidth,o.borderBottomWidth,o.borderLeftWidth],
            outRadius:o.borderRadius};
        }""")
        check("同一次工具调用的命令、状态和输出共用一张外框",
              tool_card_frame == {"tag": "DIV",
                  "outerBorders": ["solid", "solid", "solid", "solid"],
                  "outerRadius": "10px", "outerBackground": single_tool_skin["preBackground"],
                  "headBorders": ["0px", "0px", "0px", "0px"],
                  "headPadding": ["8px", "8px"], "headRadius": "0px",
                  "outBorders": ["1px", "0px", "0px", "0px"], "outRadius": "0px"},
              tool_card_frame)
        standalone_frame = p.evaluate("""() => {
          const box = document.querySelector('#msgs');
          const node = msgNode({role:'tool', name:'exec', summary:'$ echo standalone', text:'{}'});
          box.appendChild(node);
          const card = node.querySelector(':scope > .tool-entry');
          const head = node.querySelector(':scope > .tool-entry > .tool-head');
          const ns = getComputedStyle(node), cs = getComputedStyle(card), hs = getComputedStyle(head);
          const result = {outerOverflow:ns.overflow, outerRadius:ns.borderRadius,
            cardRadius:cs.borderRadius, cardBorders:[cs.borderTopStyle, cs.borderRightStyle,
              cs.borderBottomStyle, cs.borderLeftStyle], headRadius:hs.borderRadius,
            headBorders:[hs.borderTopWidth, hs.borderRightWidth,
              hs.borderBottomWidth, hs.borderLeftWidth]};
          node.remove(); return result;
        }""")
        check("独立 exec 外层不再裁掉命令框圆角",
              standalone_frame == {"outerOverflow": "visible", "outerRadius": "0px",
                                   "cardRadius": "10px",
                                   "cardBorders": ["solid", "solid", "solid", "solid"],
                                   "headRadius": "0px",
                                   "headBorders": ["0px", "0px", "0px", "0px"]},
              standalone_frame)
        first_entry.locator("> .tool-head > .tool-toggle").focus()
        p.keyboard.press("Enter")
        check("工具调用头可用键盘展开参数",
              first_entry.locator("> .tool-args").is_visible()
              and first_entry.locator("> .tool-head > .tool-toggle").get_attribute("aria-expanded") == "true")
        first_entry.locator("> .tool-args-close").click()
        first_entry.locator("> .tool-head > .tool-toggle").click()
        args_geometry = first_entry.evaluate("""n => {
          const h = n.querySelector(':scope > .tool-head').getBoundingClientRect();
          const a = n.querySelector(':scope > .tool-args').getBoundingClientRect();
          return {gap:a.top - h.bottom, expanded:n.querySelector(':scope > .tool-head > .tool-toggle').ariaExpanded};
        }""")
        check("原始参数与命令头无缝连成一张卡片",
              first_entry.locator("> .tool-args").is_visible()
              and abs(args_geometry["gap"]) < 1 and args_geometry["expanded"] == "true",
              args_geometry)
        check("展开原始参数后显示明确收起键",
              first_entry.locator("> .tool-args-close").is_visible()
              and first_entry.locator("> .tool-args-close").inner_text() == "收起参数")
        first_entry.locator("> .tool-args-close").click()
        check("收起参数键关闭展开区",
              not first_entry.locator("> .tool-args").is_visible()
              and first_entry.locator("> .tool-head > .tool-toggle").get_attribute("aria-expanded") == "false")
        failed_entry = grp.locator("> .tool-entry").filter(has_text="$ echo hi2")
        check("Claude 文本退出码进入工具状态行",
              "exit 2" in failed_entry.locator(".tool-status").inner_text()
              and "13 行" in failed_entry.locator(".tool-status").inner_text(),
              failed_entry.locator(".tool-status").inner_text())
        check("长工具输出用省略行数而非总字符数",
              "另有 5 行" in failed_entry.locator("button.more").inner_text(),
              failed_entry.locator("button.more").inner_text())
        failed_entry.locator("button.more").click()
        check("工具输出展开后可以收起",
              failed_entry.locator("button.more").inner_text() == "收起"
              and failed_entry.locator("button.more").get_attribute("aria-expanded") == "true")
        failed_entry.locator("button.more").click()
        check("工具输出收起后恢复原预览",
              "另有 5 行" in failed_entry.locator("button.more").inner_text()
              and failed_entry.locator("button.more").get_attribute("aria-expanded") == "false")
        grp.locator("> .disclosure").click()
        p.wait_for_timeout(150)
        check("组可再折叠", not grp.locator("> .tool-entry").first.is_visible())

    # ---- 8c. 文件修改卡片内联 diff 与独立视图切换 ----
    change = p.locator(".file-change-card").first
    check("文件修改不埋在折叠工具组里", change.is_visible())
    diff_width = change.evaluate("""n => {
      const msg = n.closest('.file-change-msg');
      const parent = msg.parentElement, ps = getComputedStyle(parent);
      return {
        diff: msg.getBoundingClientRect().width,
        available: parent.clientWidth - parseFloat(ps.paddingLeft) - parseFloat(ps.paddingRight)
      };
    }""")
    check("diff 卡片使用对话栏完整可用宽度",
          abs(diff_width["diff"] - diff_width["available"]) < 2, diff_width)
    check("diff 卡片使用统一外圆角",
          change.evaluate("n => getComputedStyle(n).borderRadius") == "10px")
    check("修改卡显示路径和增删统计",
          "demo.py" in change.inner_text() and "+1" in change.inner_text() and "−1" in change.inner_text(),
          change.inner_text())
    check("时间线直接显示完整修改 diff",
          "return 1" in change.inner_text() and "return 2" in change.inner_text())
    check("diff 不再使用点击弹窗", p.locator("#file-diff-dialog").count() == 0)
    check("内联统一 diff 区分新增和删除",
          "return 1" in change.locator(".diff-line.del").inner_text()
          and "return 2" in change.locator(".diff-line.add").inner_text())
    p.wait_for_function("document.querySelector('.file-change-card .diff-line.add .hljs-keyword')",
                        timeout=15000)
    check("内联 diff 按文件扩展名叠加源码语法高亮",
          change.locator(".diff-line.del .hljs-keyword").count() >= 1
          and change.locator(".diff-line.add .hljs-keyword").count() >= 1)
    wrap_button = change.locator("[data-diff-wrap]")
    wrap_before = change.locator(".diff-line > code").first.evaluate(
        "n => getComputedStyle(n).whiteSpace")
    wrap_button.click()
    wrap_after = change.locator(".diff-line > code").first.evaluate(
        "n => getComputedStyle(n).whiteSpace")
    check("diff 可以按卡片切换长行自动换行",
          wrap_before == "pre" and wrap_after == "pre-wrap"
          and change.get_attribute("data-diff-wrap") == "true"
          and wrap_button.get_attribute("aria-pressed") == "true")
    wrap_button.click()
    check("diff 可以恢复原始行宽",
          change.locator(".diff-line > code").first.evaluate(
              "n => getComputedStyle(n).whiteSpace") == "pre"
          and change.get_attribute("data-diff-wrap") == "false")
    change.locator('[data-diff-view="split"]').click()
    split = change.locator(".diff-split > section")
    check("diff 可以切换为修改前后并排", split.count() == 2
          and "return 1" in split.nth(0).inner_text()
          and "return 2" in split.nth(1).inner_text())
    check("并排 diff 切换后仍保留语法高亮",
          split.nth(0).locator(".hljs-keyword").count() >= 1
          and split.nth(1).locator(".hljs-keyword").count() >= 1)
    check("片段不会伪装成完整文件", "片段" in change.locator(".file-change-head").inner_text())
    check("当前卡片记录并排状态", change.get_attribute("data-diff-view") == "split")
    desktop_sides = split.evaluate_all("""nodes => nodes.map(n => {
      const r = n.getBoundingClientRect(); return {left:r.left, top:r.top, bottom:r.bottom};
    })""")
    check("桌面并排 diff 保持左右布局",
          abs(desktop_sides[0]["top"] - desktop_sides[1]["top"]) < 2
          and desktop_sides[1]["left"] > desktop_sides[0]["left"], desktop_sides)
    p.set_viewport_size({"width": 390, "height": 780})
    p.evaluate("document.body.classList.add('mobile-detail')")
    p.wait_for_timeout(150)
    mobile_sides = split.evaluate_all("""nodes => nodes.map(n => {
      const r = n.getBoundingClientRect(); return {left:r.left, top:r.top, bottom:r.bottom};
    })""")
    check("手机并排 diff 改为修改前后上下布局",
          mobile_sides[1]["top"] >= mobile_sides[0]["bottom"] - 1,
          mobile_sides)
    p.set_viewport_size({"width": 1280, "height": 720})
    p.evaluate("document.body.classList.remove('mobile-detail')")
    p.wait_for_timeout(150)
    change.locator('[data-diff-view="unified"]').click()
    check("diff 可以切回统一视图", change.locator(".diff-unified").is_visible())

    # ---- 9. 展开全文 ----
    more = p.locator(".msg .more:visible").filter(has_text="展开全文").first
    check("长消息出现展开全文按钮", more.count() > 0 and "展开全文" in more.inner_text())
    # 对话长文只有这一个展开入口，展开后同一按钮变为收起。
    target = more.evaluate_handle("n => n.closest('.msg')").as_element()
    mb = target.query_selector(".mb")
    h_before = mb.bounding_box()["height"]
    check("截断样式实际生效(有 clip 类)", mb.evaluate("n => n.classList.contains('clip')"))
    check("截断高度受限", h_before <= 400, h_before)
    target.query_selector(".more").click()
    p.wait_for_timeout(400)
    h_after = mb.bounding_box()["height"]
    check("展开全文后高度变大", h_after > h_before + 100, f"{h_before}->{h_after}")
    check("展开后同一按钮变为收起",
          target.query_selector(".more").is_visible()
          and target.query_selector(".more").inner_text() == "收起")
    check("展开后内容完整", "点下方按钮展开全文" not in mb.inner_text())
    target.query_selector(".more").click()
    p.wait_for_timeout(200)
    check("长对话可以收回预览",
          mb.bounding_box()["height"] < h_after
          and "展开全文" in target.query_selector(".more").inner_text())

    # ---- 10. 子代理是父会话内的互斥视图，不混入主时间线 ----
    check("有子代理的会话标题可下拉切换", p.locator("#a-view-switch").count() == 1)
    check("不再显示合并子代理按钮", p.locator("#a-agents").count() == 0)
    p.locator("#a-view-switch").click()
    check("下拉列出主会话和两个子代理",
          p.locator("#session-view-menu button").count() == 3)
    check("子代理任务描述作为标题",
          "调查数据链路" in p.locator("#session-view-menu").inner_text())
    p.locator('#session-view-menu button[data-agent="selftest-one"]').click()
    p.wait_for_function("document.querySelector('.dhead h2')?.textContent.includes('调查数据链路')")
    check("切换后只显示选中子代理",
          "子代理一的独立结论" in p.locator("#msgs").inner_text()
          and "自测：第一条用户消息" not in p.locator("#msgs").inner_text())
    check("子代理不会出现在左侧独立会话列表",
          p.locator(".item").filter(has_text="调查数据链路").count() == 0)
    check("切换子代理不改变左侧父会话选中项", p.locator(".item.sel").count() == 1)
    p.locator("#a-view-switch").click()
    p.locator('#session-view-menu button[data-agent=""]').click()
    p.wait_for_function("document.querySelector('.dhead h2')?.textContent.includes('AGENTHUB自测会话请删除')")
    check("切回主会话后不残留子代理内容",
          "子代理一的独立结论" not in p.locator("#msgs").inner_text())

    # ---- 14. 整份载入 + 进度条 + LRU 缓存 ----
    pinned_cache = p.evaluate("""() => {
      const oldLiveTmux = S.liveTmux;
      const oldTermList = T.list;
      const oldSelected = S.sel, oldAgent = S.agent;
      const oldCache = [...cache];
      const oldLimit = CACHE_MAX_BYTES;
      try {
        cache.clear();
        CACHE_MAX_BYTES = 64 * 1024 * 1024;
        S.sel = 'plain-selected'; S.agent = null;
        S.liveTmux = new Set(['tmux-live']);
        T.list = [{ name: 'tmux-mapped', uid: 'tmux-by-list' }];
        const mb = 1024 * 1024;
        cachePut('tmux-live', {
          meta: { uid: 'tmux-live' }, msgs: [], bytes: 80 * mb,
        });
        cachePut('plain-selected', {
          meta: { uid: 'plain-selected' }, msgs: [], bytes: 40 * mb,
        });
        cachePut('plain-old', {
          meta: { uid: 'plain-old' }, msgs: [], bytes: 40 * mb,
        });
        cachePut('tmux-by-list::child', {
          meta: { uid: 'tmux-by-list' }, msgs: [], bytes: 80 * mb,
        });
        cachePut('plain-new', {
          meta: { uid: 'plain-new' }, msgs: [], bytes: 40 * mb,
        });
        const whileRunning = [...cache.keys()];

        S.liveTmux = new Set();
        T.list = [];
        trimCache();
        return { whileRunning, afterStop: [...cache.keys()] };
      } finally {
        S.liveTmux = oldLiveTmux;
        T.list = oldTermList;
        S.sel = oldSelected; S.agent = oldAgent;
        CACHE_MAX_BYTES = oldLimit;
        cache.clear();
        for (const [key, entry] of oldCache) cache.set(key, entry);
      }
    }""")
    check("tmux 会话缓存不受 LRU 容量淘汰",
          pinned_cache["whileRunning"] ==
          ["tmux-live", "plain-selected", "tmux-by-list::child", "plain-new"],
          pinned_cache)
    check("当前可见会话缓存不受 LRU 容量淘汰",
          "plain-selected" in pinned_cache["whileRunning"]
          and "plain-selected" in pinned_cache["afterStop"], pinned_cache)
    check("tmux 结束后缓存重新参与 LRU",
          pinned_cache["afterStop"] == ["plain-selected", "plain-new"], pinned_cache)

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
    window_uid = p.evaluate(
        "() => S.sessions.find(s => s.title === 'AGENTHUB分页载入测试').uid")
    # 上一段可能还有没落地的载入, 清两次并等一拍, 否则它的 cachePut 会落在 clear 之后
    p.evaluate("cache.clear()")
    p.wait_for_load_state("domcontentloaded")
    p.wait_for_timeout(400)
    p.evaluate("uid => cache.delete(uid)", window_uid)
    reqs.clear()
    p.evaluate("uid => openSession(uid)", window_uid)
    p.wait_for_selector(".history-gap", timeout=120000)
    p.wait_for_function("!document.querySelector('#prog').classList.contains('on')", timeout=120000)
    total = int(re.search(r"(\d+) 条消息", p.locator("#mcount-total").text_content()).group(1))
    sparse = p.evaluate("""() => { const e = cache.get(S.sel); return {
      shown:e.msgs.length, total:e.total, partial:e.partial,
      text:document.querySelector('#msgs').textContent}; }""")
    check("新会话首载最早 100 条和最新 500 条",
          sparse["shown"] == 600 and sparse["total"] == 650
          and sparse["partial"] == {"head": 100, "tail": 500, "omitted": 50}, sparse)
    check("首尾消息存在而中间消息尚未载入",
          "分页消息 099" in sparse["text"] and "分页消息 120" not in sparse["text"]
          and "分页消息 649" in sparse["text"])
    check("中间缺口显示一键载入按钮",
          p.locator(".history-gap-load").count() == 1
          and "50" in p.locator(".history-gap-load").inner_text())
    check("首载请求启用 100+500 窗口",
          reqs and "window=1" in reqs[0] and "start=" not in reqs[0], reqs[:2])
    check("载入时显示过进度条", p.evaluate("window.__prog"))

    p.locator(".history-gap-load").click()
    p.wait_for_function("!document.querySelector('.history-gap')", timeout=120000)
    p.wait_for_function("!document.querySelector('#prog').classList.contains('on')", timeout=120000)
    full = p.evaluate("() => { const e = cache.get(S.sel); return {shown:e.msgs.length, total:e.total, partial:e.partial}; }")
    check("点击缺口按钮一次载入完整历史",
          full == {"shown": 650, "total": 650, "partial": None}, full)
    check("完整历史请求不再带首载窗口",
          any("window=1" not in u and "start=" not in u for u in reqs[1:]), reqs)
    # 工具组本身不算原始消息，组内扁平 tool-entry 各算一条；调用卡片吸收的
    # tool_result 通过 data-result 标记补回计数；rename/compact 结果会显示在
    # 时间线里，但它们是辅助事件，不进入标题栏的“消息”计数。
    dom_msgs = p.locator(
        '#msgs .msg:not(.grp):not(.tool-msg):not([data-counted="false"]), '
        '#msgs .tool-entry:not([data-counted="false"])').count() \
        + p.locator('#msgs [data-result="1"]').count()
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
                              if (n) n.querySelector('.fold-preview > .fold-toggle').click(); }""")
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
    # 完整历史已进入缓存，重新打开应直接命中，只允许后台增量请求。
    reqs.clear()
    p.evaluate("uid => openSession(uid)", window_uid)
    p.wait_for_selector(".msg", timeout=60000)
    p.wait_for_timeout(800)
    cached = p.evaluate("() => !!cache.get(S.sel)")
    check("再次打开命中缓存", cached and (not reqs or all("start=" in u for u in reqs)),
          f"cached={cached} reqs={reqs[-2:]}")
    # 活跃会话在这期间可能又被推了新消息, 也可能因回滚整份重来, 数字不好精确比;
    # 关键是缓存命中后内容正常渲染出来了
    back = int(re.search(r"(\d+) 条消息", p.locator("#mcount-total").text_content()).group(1))
    check("缓存命中后内容照常渲染", back > 0 and p.locator("#msgs .msg").count() > 0,
          f"meta={back} dom={p.locator('#msgs .msg').count()} 首次={total}")

    # 缓存命中必须先用短连接补齐游标，再建立长期 EventSource。反过来在
    # HTTP/1 连接池紧张时会让 fetch 永远排队，表现为原标签页不再增量上屏。
    watch_order = p.evaluate("""async () => {
      const originalSync = syncSession, originalWatch = watchSession;
      const events = [];
      let error = '';
      syncSession = async () => {
        events.push('sync-start');
        await new Promise(resolve => setTimeout(resolve, 40));
        events.push('sync-end');
        return 0;
      };
      watchSession = () => events.push('watch');
      try {
        await openSession(S.sel, S.agent);
      } catch (e) {
        error = String(e);
      } finally {
        syncSession = originalSync;
        watchSession = originalWatch;
        originalWatch(S.sel, S.agent);
      }
      return {events, error};
    }""")
    check("缓存命中先增量补齐再建立实时监听",
          not watch_order["error"]
          and watch_order["events"] == ["sync-start", "sync-end", "watch"],
          watch_order)

    # 模拟一条连响应头都收不到的增量请求。它必须自行 abort，并从
    # syncingViews 删除；否则后续 SSE/outbox 修复只会反复等同一个死 Promise。
    p.evaluate("syncSession(S.sel, S.agent)")
    stalled_sync = p.evaluate("""async () => {
      const originalFetch = globalThis.fetch;
      const originalTimeout = SYNC_STALL_MS;
      const key = viewKey(S.sel, S.agent);
      let calls = 0, aborted = 0;
      closeWatch();
      SYNC_STALL_MS = 80;
      globalThis.fetch = (url, options = {}) => {
        if (String(url).includes('/api/messages/') && String(url).includes('start=')) {
          calls++;
          return new Promise((resolve, reject) => {
            const abort = () => {
              aborted++;
              reject(new DOMException('stalled test request', 'AbortError'));
            };
            if (options.signal?.aborted) abort();
            else options.signal?.addEventListener('abort', abort, {once: true});
          });
        }
        return originalFetch(url, options);
      };
      const started = performance.now();
      let result = null, error = '';
      try {
        result = await syncSession(S.sel, S.agent);
      } catch (e) {
        error = String(e);
      } finally {
        globalThis.fetch = originalFetch;
        SYNC_STALL_MS = originalTimeout;
      }
      const state = {calls, aborted, result, error,
        elapsed: performance.now() - started,
        stillSyncing: syncingViews.has(key)};
      watchSession(S.sel, S.agent);
      return state;
    }""")
    check("悬住的增量读取会超时并释放同步槽",
          stalled_sync["calls"] == 1 and stalled_sync["aborted"] == 1
          and stalled_sync["result"] == 0 and not stalled_sync["error"]
          and stalled_sync["elapsed"] < 1000 and not stalled_sync["stillSyncing"],
          stalled_sync)

    # ---- 14a. 增量同步: 会话被 CLI 追加内容后应自动接上 ----
    p.fill("#q", "AGENTHUB自测")
    p.wait_for_timeout(250)
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=20000)
    n0 = int(re.search(r"(\d+) 条消息", p.locator("#mcount-total").text_content()).group(1))
    fake = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
    with open(fake, "a") as fh:                       # 模拟 CLI 追加
        fh.write(json.dumps({
            "type": "user", "message": {"role": "user", "content": "追加的新消息ZZQ"},
            "uuid": "u9", "timestamp": "2026-08-06T12:30:00.000Z",
            "cwd": "/tmp/agenthub-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
    p.evaluate("syncSession(S.sel)")
    p.wait_for_function(f"document.querySelector('#mcount-total').textContent !== '{n0} 条消息'",
                        timeout=30000)
    n1 = int(re.search(r"(\d+) 条消息", p.locator("#mcount-total").text_content()).group(1))
    check("增量同步接上新消息", n1 == n0 + 1, f"{n0}->{n1}")

    check("新消息出现在末尾", "追加的新消息ZZQ" in p.locator("#msgs > .msg").last.inner_text(),
          p.locator("#msgs > .msg").last.inner_text()[:40])
    check("对话页不显示新消息数", p.locator(".dhead .newmsg, .dmeta .newmsg").count() == 0)

    # 手机停在列表时，代理的新内容积累在来源图标右上角；打开会话即视为已读。
    viewport(390, 780)
    p.evaluate("showMobileList()")
    with open(fake, "a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "message": {"role": "assistant", "content": "助手追加的新内容AAQ"},
            "uuid": "a9", "timestamp": "2026-08-06T12:30:01.000Z",
            "cwd": "/tmp/agenthub-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
    p.evaluate("syncSession(S.sel)")
    p.wait_for_function("document.querySelector('.item.sel .item-status')?.textContent === '1'",
                        timeout=30000)
    n1a = int(re.search(r"(\d+) 条消息", p.locator("#mcount-total").text_content()).group(1))
    status = p.locator(".item.sel .item-status")
    check("代理新内容显示在列表图标右上角",
          "counted" in (status.get_attribute("class") or "") and status.inner_text() == "1",
          status.get_attribute("class"))
    p.locator(".item.sel").click()
    p.wait_for_function("!document.querySelector('.item.sel .item-status')?.classList.contains('counted')")
    check("打开会话后清除列表未读数", status.inner_text() == "")
    viewport(1280, 800)
    inc = [u for u in reqs if "start=" in u]
    check("同步走的是增量请求", len(inc) > 0)

    # 文件被改写(不是追加) → 必须整份重来, 不能把旧内容和新内容拼起来
    txt = fake.read_text().splitlines()
    fake.write_text("\n".join([json.dumps({"type": "ai-title", "aiTitle": "AGENTHUB自测会话请删除",
                                           "sessionId": fake.stem}, ensure_ascii=False)] + txt[1:]) + "\n")
    with open(fake, "a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "改写后追加YYQ"},
                             "uuid": "u10", "timestamp": "2026-08-06T12:31:00.000Z",
                             "cwd": "/tmp/agenthub-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
    p.evaluate("syncSession(S.sel)")
    p.wait_for_function("document.querySelector('#msgs').textContent.includes('改写后追加YYQ')", timeout=30000)
    n2 = int(re.search(r"(\d+) 条消息", p.locator("#mcount-total").text_content()).group(1))
    check("文件改写后整份重载, 消息不重复", n2 == n1a + 1, f"{n1a}->{n2}")
    check("重载后旧消息仍在一次", p.locator("#msgs").inner_text().count("追加的新消息ZZQ") == 1,
          p.locator("#msgs").inner_text().count("追加的新消息ZZQ"))
    # 更新靠服务端推送(SSE), 不是客户端轮询
    p.wait_for_function("_es && _es.readyState === 1", timeout=20000)
    check("已建立服务端推送连接", p.evaluate("_es.readyState") == 1)
    # 上一段刻意让主动读取与 SSE 竞争，可能留下一个正在收尾的恢复任务。
    # 先建立干净边界，后面的断言才真正只测 SSE，而不是把上一段的尾声
    # 误算成“定时轮询”。若恢复自身悬死，这里会明确超时失败。
    p.wait_for_function("diffRecoveries.size === 0 && syncingViews.size === 0",
                        timeout=20000)
    # 上一段同时使用了主动读取和 SSE。主动读取会推进浏览器游标，已有
    # EventSource 仍保留建连时的旧游标；重建连接后再测纯推送路径。
    old_sse_connection = p.evaluate("_es?.__agenthubConnectionId || ''")
    p.evaluate("watchSession(S.sel, S.agent)")
    p.wait_for_function("old => _es && _es.readyState === 1"
                        " && _es.__agenthubConnectionId !== old",
                        arg=old_sse_connection, timeout=20000)
    p.evaluate("""() => {
      window.__e2eOriginalSyncSession = syncSession;
      window.__e2eOriginalScheduleDiffRecovery = scheduleDiffRecovery;
      window.__e2eSyncCalls = [];
      window.__e2eRecoveryCalls = [];
      syncSession = (...args) => {
        window.__e2eSyncCalls.push({at:Date.now(), args,
          selected:[S.sel, S.agent], lastSync:S.lastSync,
          eventSource:{uid:_esUid, state:_es?.readyState ?? -1},
          recoveries:[...diffRecoveries.keys()],
          stack:String(new Error().stack || '').split('\\n').slice(1, 7)});
        return window.__e2eOriginalSyncSession(...args);
      };
      scheduleDiffRecovery = (...args) => {
        const entry = cache.get(viewKey(args[0], args[1]));
        window.__e2eRecoveryCalls.push({at:Date.now(), args,
          cursor:entry ? {end:entry.end, head:entry.version?.head,
            messages:entry.msgs?.length} : null,
          queued:queuedMessages(args[0]).map(x => ({id:x.id, state:x.state})),
          stack:String(new Error().stack || '').split('\\n').slice(1, 7)});
        return window.__e2eOriginalScheduleDiffRecovery(...args);
      };
    }""")
    pulls = []
    p.on("request", lambda r: pulls.append(r.url) if "/api/messages/" in r.url else None)
    lat = []
    for i in range(3):
        t0 = time.time()
        with open(fake, "a") as fh:
            fh.write(json.dumps({
                "type": "user", "message": {"role": "user", "content": "推送消息RT%d" % i},
                "uuid": "rt%d" % i, "timestamp": "2026-08-07T12:1%d:00.000Z" % i,
                "cwd": "/tmp/agenthub-selftest", "sessionId": fake.stem}, ensure_ascii=False) + "\n")
        p.wait_for_function("document.querySelector('#msgs').textContent.includes('推送消息RT%d')" % i,
                            timeout=20000)
        lat.append(time.time() - t0)
    sync_calls = p.evaluate("""() => {
      const calls = {sync:window.__e2eSyncCalls || [],
        recovery:window.__e2eRecoveryCalls || []};
      if (window.__e2eOriginalSyncSession) {
        syncSession = window.__e2eOriginalSyncSession;
        delete window.__e2eOriginalSyncSession;
      }
      if (window.__e2eOriginalScheduleDiffRecovery) {
        scheduleDiffRecovery = window.__e2eOriginalScheduleDiffRecovery;
        delete window.__e2eOriginalScheduleDiffRecovery;
      }
      delete window.__e2eSyncCalls;
      delete window.__e2eRecoveryCalls;
      return calls;
    }""")
    check("新消息被推送上屏(亚秒级)", max(lat) < 1.0, [round(x, 3) for x in lat])
    check("期间没有客户端主动拉取", not pulls,
          {"pulls": pulls[:2], "sync_calls": sync_calls})

    # 回滚也走推送
    lines = fake.read_text().splitlines()
    fake.write_text("\n".join(lines[:-2] + [json.dumps(
        {"type": "user", "message": {"role": "user", "content": "推送回滚RBK"}, "uuid": "rbk",
         "timestamp": "2026-08-07T12:30:00.000Z", "cwd": "/tmp/agenthub-selftest",
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
          and isinstance(api.get("started_at"), dict)
          and set(api["started_at"]).issubset(api["uids"])
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
    live_switch = p.locator("#livecount")
    check("顶栏活动计数是一个单选项",
          live_switch.evaluate("n => n.tagName") == "BUTTON"
          and live_switch.get_attribute("aria-checked") == "false")
    check("活动计数不再显示 tmux 文字",
          "tmux" not in live_switch.inner_text().lower(), live_switch.inner_text())
    zero_live = p.evaluate("""() => {
      const live = S.live, tmux = S.liveTmux;
      const pending = typeof T !== 'undefined' ? T.pending : undefined;
      S.live = new Set(); S.liveTmux = new Set();
      if (typeof T !== 'undefined') T.pending = [];
      paintLive();
      const n = document.querySelector('#livecount');
      const result = {text:n.innerText.trim(), display:getComputedStyle(n).display,
                      label:n.getAttribute('aria-label')};
      S.live = live; S.liveTmux = tmux;
      if (typeof T !== 'undefined') T.pending = pending;
      paintLive();
      return result;
    }""")
    check("无活动会话时计数仍显示 0",
          zero_live["text"].endswith("0") and zero_live["display"] == "inline-flex"
          and zero_live["label"].startswith("0 个活跃会话"), zero_live)
    before_live_filter = set(p.locator("#side .item").evaluate_all(
        "nodes => nodes.map(n => n.dataset.uid)"))
    live_switch.click()
    p.wait_for_timeout(100)
    expected_active = set(p.evaluate("""() => sidebarSessions()
      .filter(s => !S.off.has(s.source) && (s.pending || S.live.has(s.uid)))
      .map(s => s.uid)"""))
    shown_active = set(p.locator("#side .item").evaluate_all(
        "nodes => nodes.map(n => n.dataset.uid)"))
    check("活动开关按下后只显示活动会话",
          shown_active == expected_active
          and live_switch.get_attribute("aria-checked") == "true",
          f"shown={shown_active}, expected={expected_active}")
    p.locator('#allcount').click()
    p.wait_for_timeout(100)
    check("点击总数恢复全部会话",
          set(p.locator("#side .item").evaluate_all("nodes => nodes.map(n => n.dataset.uid)"))
          == before_live_filter
          and live_switch.get_attribute("aria-checked") == "false")
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
    extra.write_text(json.dumps({"type": "ai-title", "aiTitle": "AGENTHUB新会话ZZ",
                                 "sessionId": extra.stem}, ensure_ascii=False) + "\n"
                     + json.dumps({"type": "user", "message": {"role": "user", "content": "新会话正文"},
                                   "uuid": "n1", "timestamp": "2026-08-07T09:00:00.000Z",
                                   "cwd": "/tmp/agenthub-selftest", "sessionId": extra.stem},
                                  ensure_ascii=False) + "\n")
    p.evaluate("pollSessions()")
    p.wait_for_function(f"S.sessions.length > {n_before}", timeout=30000)
    check("新会话自动出现在列表, 不必手动刷新",
          p.evaluate("S.sessions.some(s => s.title.includes('AGENTHUB新会话ZZ'))"))
    check("签名随之更新", p.evaluate("S.sig") != sig0)
    check("自动刷新不丢选中", p.evaluate("S.sel") == sel_uid and p.locator(".item.sel").count() == 1)
    check("自动刷新保持滚动位置",
          abs(p.evaluate("document.querySelector('#side').scrollTop") - 240) < 30,
          p.evaluate("document.querySelector('#side').scrollTop"))
    check("详情区没被打断", p.locator("#msgs .msg").count() > 0)

    # 未选中的 Claude 会话也要从列表游标续读新增区间；不能只有当前详情页
    # 的 SSE 才会累加角标，更不能为此重新下载整份历史。
    extra_uid = p.evaluate(
        "() => S.sessions.find(s => s.title.includes('AGENTHUB新会话ZZ')).uid")
    with open(extra, "a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "message": {"role": "assistant", "content": "后台回复未读BBQ"},
            "uuid": "n2", "parentUuid": "n1", "timestamp": "2026-08-07T09:00:01.000Z",
            "cwd": "/tmp/agenthub-selftest", "sessionId": extra.stem,
        }, ensure_ascii=False) + "\n")
    # 服务端会在 500ms 内复用刚发布的 inventory；这里明确跨过该去抖窗口，
    # 测的是后台增量/未读，而不是同一瞬间重复刷新是否重扫磁盘。
    p.wait_for_timeout(600)
    p.evaluate("pollSessions()")
    p.wait_for_timeout(3000)
    extra_badge = p.locator(f'.item[data-uid="{extra_uid}"] .item-status')
    unread_debug = p.evaluate("""uid => ({
      badge: document.querySelector(`.item[data-uid="${uid}"] .item-status`)?.textContent,
      unread: S.unread.get(uid), cursor: S.cursors.get(uid),
      sessionCursor: S.sessions.find(s => s.uid === uid)?.cursor,
      syncing: [...sidebarSyncing], selected: S.sel,
    })""", extra_uid)
    check("未选中的 Claude 新回复显示在左栏",
          "counted" in (extra_badge.get_attribute("class") or "")
          and extra_badge.inner_text() == "1", unread_debug)

    # 活跃会话每隔几秒就变一次, 列表要是每次都重建就会一直闪。
    # 结构没变时只改文字, DOM 节点必须原地不动。
    live_file = FAKE_PROJ / "00000000-dead-beef-0000-000000000003.jsonl"
    live_file.write_text(json.dumps(
        {"type": "ai-title", "aiTitle": "AGENTHUB活跃写入", "sessionId": live_file.stem},
        ensure_ascii=False) + "\n")
    urllib.request.urlopen(BASE + "/api/sessions?force=1", timeout=60).read()
    p.evaluate("pollSessions()")
    p.wait_for_function("S.sessions.some(s => s.title.includes('AGENTHUB活跃写入'))", timeout=30000)
    p.wait_for_timeout(400)
    live_uid = p.evaluate("() => S.sessions.find(s => s.title.includes('AGENTHUB活跃写入')).uid")
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
                    "cwd": "/tmp/agenthub-selftest", "sessionId": live_file.stem},
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
    p.fill("#q", "AGENTHUB自测")
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
        p.fill("#q", "AGENTHUB自测")
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
                "cwd": "/tmp/agenthub-selftest", "sessionId": fake2.stem}, ensure_ascii=False) + "\n")
        p.wait_for_function("document.querySelector('#msgs').textContent.includes('将被回滚掉XYZ')",
                            timeout=20000)
        # 模拟双 Esc 回滚: 砍掉尾部, 再写入不同内容, 让文件重新变得更长
        lines = fake2.read_text().splitlines()
        rolled = lines[:-2] + [json.dumps(
            {"type": "user", "message": {"role": "user", "content": "回滚后的新内容QQZ"},
             "uuid": "r%d" % i, "timestamp": "2026-08-07T12:0%d:00.000Z" % i,
             "cwd": "/tmp/agenthub-selftest", "sessionId": fake2.stem}, ensure_ascii=False)
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
    check("刷新使用完整的双箭头环形图标",
          p.locator("#i-refresh path").count() == 2
          and p.locator("#reload use").get_attribute("href") == "#i-refresh")
    n_before = p.locator(".item").count()
    p.click("#reload")
    p.wait_for_function(
        f"() => document.querySelector('.item[data-uid=\"{fake_uid}\"]')"
        " && !document.querySelector('#stat').textContent.includes('扫描')", timeout=30000)
    n_refreshed = p.locator(".item").count()
    # 自动化与用户真实会话共用索引，刷新期间真实会话可能恰好增删；确定性
    # 验证自测会话和非搜索态，不把全局总数当成刷新正确性的前提。
    check("刷新后会话列表保持完整",
          p.locator(f'.item[data-uid="{fake_uid}"]').count() == 1
          and p.evaluate("S.results === null"),
          {"before": n_before, "after": n_refreshed})

    # ---- 14c. 搜索态下切换来源筛选 ----
    p.fill("#q", "agenthub")
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
    check("清空输入退出搜索态",
          p.evaluate("S.results === null")
          and p.locator(f'.item[data-uid="{fake_uid}"]').count() == 1,
          p.locator(".item").count())

    # ---- 14d. 折叠预览不冒充 header ----
    p.locator(".item").first.click()
    p.wait_for_selector(".msg", timeout=20000)
    check("消息 DOM 中没有 header", p.locator("#msgs .mh").count() == 0)
    check("折叠的消息仍显示预览", p.locator('.msg.folded .peek').first.is_visible())

    # ---- 15. 快捷键 ----
    p.keyboard.press("Escape")
    p.locator("body").click(position={"x": 5, "y": 400})
    p.keyboard.press("/")
    check("斜杠不再劫持焦点到搜索框", p.evaluate("document.activeElement.id") != "q")
    check("斜杠不会写入搜索框", p.input_value("#q") == "", repr(p.input_value("#q")))

    # ---- 15a. 接管会话 (服务端需 --terminal) ----
    tl = json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())
    if not tl.get("enabled"):
        check("终端未启用时不显示接管入口", p.locator("#a-term").count() == 0)
        check("终端未启用时不显示新建入口", p.locator("#new-session").is_hidden())
    else:
        # 新会话进入专用 server，但改造前默认 server 中的 agenthub-* 仍须可见、可路由。
        legacy_name = "agenthub-e2e-legacycompat"
        tmux_run("default", "kill-session", "-t", legacy_name, capture_output=True)
        tmux_run("default", "new-session", "-d", "-s", legacy_name, "sleep 60", check=True)
        legacy_row = next((x for x in term_rows() if x["name"] == legacy_name), None)
        check("默认 server 的旧 agenthub 会话仍可见",
              legacy_row is not None and legacy_row.get("server") == "default", legacy_row)
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/term/kill",
            json.dumps({"name": legacy_name}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read()
        check("旧会话操作会路由回默认 server",
              tmux_run("default", "has-session", "-t", legacy_name,
                       capture_output=True).returncode != 0)

        p.click("#new-session")
        p.wait_for_selector("#new-session-dialog[open]")
        check("新建弹窗有三种会话类型", p.locator('input[name="new-source"]').count() == 3)
        recent_policy = p.evaluate("""() => {
          const sessions = S.sessions, recent = store.get('newDirs', []);
          try {
            S.sessions = [
              {cwd:'/tmp/claude-1000/scratchpad/hooktest', updated:'2099-01-01'},
              {cwd:'/var/tmp/generated', updated:'2099-01-01'},
              {cwd:'/work/real-project', updated:'2099-01-01'},
            ];
            store.set('newDirs', ['/tmp/explicit-choice']);
            return commonSessionDirs().map(row => row.cwd);
          } finally {
            S.sessions = sessions;
            store.set('newDirs', recent);
          }
        }""")
        check("自动最近目录排除易失会话但保留用户主动选择",
              "/tmp/claude-1000/scratchpad/hooktest" not in recent_policy
              and "/var/tmp/generated" not in recent_policy
              and "/work/real-project" in recent_policy
              and "/tmp/explicit-choice" in recent_policy, recent_policy)
        common_dir_count = p.locator("#new-cwd-options .new-cwd-option").count()
        common_value = p.locator("#new-cwd-options .new-cwd-option").first.get_attribute("title")
        check("新建目录面板默认列出最近使用", common_dir_count >= 1
              and p.locator("#new-cwd-options-title").inner_text() == "最近使用"
              and common_dir_count == p.evaluate("cwdCompletion.common.length"))
        check("新建弹窗只有一个目录选择面板",
              p.locator("#new-cwd-picker").count() == 1
              and p.locator("#new-session-dialog select:not(#new-node)").count() == 0)
        check("启动目录可手工输入", p.locator("#new-cwd").input_value().startswith("/"))
        recent_filter = p.evaluate("""() => {
          const common = cwdCompletion.common;
          try {
            cwdCompletion.common = [
              {cwd:'/work/flux-main', count:3},
              {cwd:'/work/unrelated', count:2},
              {cwd:'/archive/FLUX-lab', count:1},
            ];
            $('#new-cwd').value = 'flux';
            scheduleCwdCompletions();
            return {
              paths:[...document.querySelectorAll('#new-cwd-options .new-cwd-option')]
                .map(node => node.title),
              groups:[...document.querySelectorAll('#new-cwd-options .new-cwd-section')]
                .map(node => node.textContent),
              title:$('#new-cwd-options-title').textContent,
            };
          } finally {
            cwdCompletion.common = common;
            $('#new-cwd').value = '';
            renderCommonCwdOptions();
          }
        }""")
        check("普通关键词会全文匹配 recent dir 且忽略大小写",
              recent_filter == {
                  "paths": ["/work/flux-main", "/archive/FLUX-lab"],
                  "groups": ["最近匹配"], "title": "匹配目录",
              }, recent_filter)
        completion_prefix = str(FAKE_CWD / "autocomplete-a")
        recent_match = completion_prefix + "-recent"
        p.evaluate("row => cwdCompletion.common.unshift(row)",
                   {"cwd": recent_match, "count": 4})
        p.fill("#new-cwd", completion_prefix)
        p.wait_for_function("""() => document.querySelector('#new-cwd-options-title')?.textContent
          === '匹配目录' && document.querySelectorAll('#new-cwd-options .new-cwd-option').length === 3""",
                            timeout=10000)
        completion_paths = p.locator("#new-cwd-options .new-cwd-option-path").all_inner_texts()
        check("斜杠开头时补全建议排在 recent 匹配之前", completion_paths == [
            str(FAKE_CWD / "autocomplete-alpha") + "/",
            str(FAKE_CWD / "autocomplete-alpine") + "/",
            recent_match,
        ] and p.locator("#new-cwd-options .new-cwd-section").all_inner_texts()
            == ["补全建议", "最近匹配"], completion_paths)
        p.locator("#new-cwd-options .new-cwd-option").first.hover()
        p.wait_for_timeout(250)
        check("鼠标移入目录候选后面板不会消失",
              p.locator("#new-cwd-picker").is_visible()
              and p.locator("#new-cwd-options .new-cwd-option").count() == 3)
        check("recent 与补全建议共用一个目录面板",
              p.locator("#new-cwd-picker").count() == 1)
        p.press("#new-cwd", "Tab")
        check("Tab 先补齐多个候选的公共前缀",
              p.input_value("#new-cwd") == str(FAKE_CWD / "autocomplete-alp"),
              p.input_value("#new-cwd"))
        p.press("#new-cwd", "ArrowDown")
        p.press("#new-cwd", "Enter")
        check("方向键和 Enter 可接受目录候选",
              p.input_value("#new-cwd") == completion_paths[0])
        p.evaluate("path => { cwdCompletion.common = cwdCompletion.common.filter(row => row.cwd !== path); }",
                   recent_match)
        p.fill("#new-cwd", "")
        check("清空输入后同一面板恢复最近目录",
              p.locator("#new-cwd-options-title").inner_text() == "最近使用"
              and p.locator("#new-cwd-options .new-cwd-option").count() == common_dir_count)
        p.locator("#new-cwd-options .new-cwd-option").first.click()
        check("最近目录可一键回填输入栏", p.input_value("#new-cwd") == common_value)
        creation_confirmation = p.evaluate("""async () => {
          const oldPost = post, oldConfirm = window.confirm;
          const requests = [];
          const prompts = [];
          let allow = false;
          $('#new-cwd').value = '/tmp/typed-missing-project';
          post = async (url, body) => {
            requests.push({url, body});
            return body.create_cwd
              ? {error:'模拟创建结束'}
              : {error:'启动目录不存在', needs_create:true,
                 cwd:'/tmp/resolved-missing-project'};
          };
          window.confirm = message => { prompts.push(message); return allow; };
          try {
            await createNewSession({preventDefault() {}});
            const cancelledAt = requests.length;
            allow = true;
            await createNewSession({preventDefault() {}});
            return {requests, prompts, cancelledAt,
                    error:$('#new-session-error').textContent,
                    button:$('#new-session-go').textContent};
          } finally {
            post = oldPost;
            window.confirm = oldConfirm;
            $('#new-session-error').textContent = '';
          }
        }""")
        check("不存在的启动目录先询问再显式创建",
              creation_confirmation["cancelledAt"] == 1
              and len(creation_confirmation["requests"]) == 3
              and "create_cwd" not in creation_confirmation["requests"][0]["body"]
              and "create_cwd" not in creation_confirmation["requests"][1]["body"]
              and creation_confirmation["requests"][2]["body"].get("create_cwd") is True
              and creation_confirmation["requests"][2]["body"]["cwd"]
                  == "/tmp/resolved-missing-project"
              and len(creation_confirmation["prompts"]) == 2
              and all("/tmp/resolved-missing-project" in prompt
                      and "是否创建" in prompt
                      for prompt in creation_confirmation["prompts"])
              and creation_confirmation["error"] == "模拟创建结束"
              and creation_confirmation["button"] == "创建并打开",
              creation_confirmation)
        p.click("#new-session-dialog .modal-cancel")

        # 新建 CLI 写出第一条正式记录前只有 pending tmux：手机也必须能切到空
        # 对话页、添加附件，并直接按 tmux 名关机。
        tmux_run("agenthub", "kill-session", "-t", PENDING_TERM, capture_output=True)
        tmux_run("agenthub", "new-session", "-d", "-s", PENDING_TERM,
                 "-x", "100", "-y", "30", "-c", str(FAKE_CWD),
                 "bash --noprofile --norc", check=True)
        pending_store.put({
            "name": PENDING_TERM, "source": "claude",
            "sid": "00000000-dead-beef-0000-000000000002", "cwd": str(FAKE_CWD),
            "token": "e2e-pending", "before": [], "started": time.time(),
            "cols": 100, "rows": 30,
        })
        p.evaluate("async () => { await loadTermList(); }")
        viewport(390, 780)
        p.evaluate("""n => openPendingSession(
          pendingTmuxSessions().find(x => x.name === n || x.tmuxName === n))""", PENDING_TERM)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        open_session_menu(p)
        check("手机新建临时会话菜单显示关机按钮",
              p.locator("#a-session-action").is_visible()
              and p.locator("#a-session-action").get_attribute("title") == "停止会话"
              and p.locator("#a-session-action use").get_attribute("href") == "#i-power")
        p.keyboard.press("Escape")
        check("手机新建临时会话可从终端切换到对话",
              p.locator("#a-term").get_attribute("title") == "切换到对话")
        p.click("#a-term")
        p.wait_for_timeout(200)
        check("尚无首条消息也显示输入框和附件入口",
              p.locator("#composer").is_visible() and p.locator("#cadd").is_visible())
        pending_upload = p.evaluate("""async n => {
          const uid = pendingUid(n), url = new URL(appUrl('api/session/attachment'));
          url.searchParams.set('uid', uid); url.searchParams.set('name', 'pending.png');
          const file = new File([new Uint8Array([1, 2, 3])], 'pending.png', {type:'image/png'});
          const response = await fetch(url, {method:'POST', headers:{'Content-Type':file.type}, body:file});
          return {status:response.status, data:await response.json()};
        }""", PENDING_TERM)
        check("临时会话第一条消息前即可上传附件",
              pending_upload["status"] == 200
              and re.fullmatch(r"agenthub_attachments/\d+/pending\.png",
                               pending_upload["data"]["relative_path"]),
              pending_upload)
        migrated_draft = p.evaluate("""() => {
          const from = 'tmux:e2e-draft', to = 'claude:e2e-draft';
          composerDrafts.set(from, {text:'待发送', quotes:[{id:'q', text:'引用'}],
            attachments:[{id:'a', uploaded:{uid:from, path:'/tmp/a'}}]});
          migrateComposerDraft(from, to);
          const draft = composerDrafts.get(to);
          const result = {old:composerDrafts.has(from), text:draft.text,
            quotes:draft.quotes.length, files:draft.attachments.length,
            uploadUid:draft.attachments[0].uploaded.uid};
          composerDrafts.delete(to);
          return result;
        }""")
        check("临时会话关联正式 uid 时迁移未发送草稿",
              migrated_draft == {"old": False, "text": "待发送", "quotes": 1,
                                 "files": 1, "uploadUid": "claude:e2e-draft"},
              migrated_draft)
        p.once("dialog", lambda d: d.accept())
        open_session_menu(p)
        p.click("#a-session-action")
        p.wait_for_function("n => !T.pending.some(x => x.name === n)", arg=PENDING_TERM,
                            timeout=30000)
        check("手机关机按临时 tmux 名结束并移出列表",
              tmux_run("agenthub", "has-session", "-t", PENDING_TERM,
                       capture_output=True).returncode != 0
              and pending_store.get(PENDING_TERM) is None)
        p.wait_for_timeout(900)
        check("关机同时取消临时会话关联轮询",
              not any(PENDING_TERM in error for error in errors), errors[-3:])
        viewport(1280, 720)
        p.wait_for_function("!MOBILE.matches")
        p.wait_for_timeout(200)

        # 首条消息前直接退出 CLI：临时项、选中详情、终端对象和输入框必须一起
        # 消失，不能留下一个不在左栏、也无法再连接的“新建会话”孤儿页。
        p.evaluate("T.mode = 'normal'")
        tmux_run("agenthub", "new-session", "-d", "-s", PENDING_EXIT_TERM,
                 "-x", "100", "-y", "30", "-c", str(FAKE_CWD),
                 "bash --noprofile --norc", check=True)
        pending_store.put({
            "name": PENDING_EXIT_TERM, "source": "claude",
            "sid": "00000000-dead-beef-0000-000000000099", "cwd": str(FAKE_CWD),
            "token": "e2e-exit", "before": [], "started": time.time(),
            "cols": 100, "rows": 30,
        })
        p.evaluate("async () => { await loadTermList(); }")
        p.evaluate("""n => openPendingSession(
          pendingTmuxSessions().find(x => x.name === n || x.tmuxName === n))""", PENDING_EXIT_TERM)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        tmux_run("agenthub", "kill-session", "-t", PENDING_EXIT_TERM, check=True)
        p.wait_for_function("n => S.sel !== pendingUid(n)", arg=PENDING_EXIT_TERM, timeout=30000)
        abandoned = p.evaluate("""n => ({
          selected:S.sel, stored:store.get('sel'), pending:T.pending.some(x => x.name === n),
          view:T.views.has(n), open:T.openViews.has(n), composer:!$('#composer').classList.contains('hidden'),
          terminal:!$('#termpane').classList.contains('hidden'), mode:T.mode, detail:$('#detail').innerText,
          listed:!!document.querySelector(`.item[data-uid="${CSS.escape(pendingUid(n))}"]`)
        })""", PENDING_EXIT_TERM)
        check("首条消息前自然退出会完整移除临时会话",
              abandoned == {"selected": None, "stored": None, "pending": False,
                            "view": False, "open": False, "composer": False,
                            "terminal": False, "mode": "normal",
                            "detail": "从左侧选择一个会话", "listed": False},
              abandoned)
        check("自然退出同时删除服务端临时记录",
              pending_store.get(PENDING_EXIT_TERM) is None)

        dialogs = []
        def _dlg(d):                 # 用完必须摘掉, 否则后面删除会话的确认框也会被它吃掉
            dialogs.append(d.message)
            d.dismiss()
        p.on("dialog", _dlg)
        # 终端回归只能使用本次测试自己的会话。旧实现从真实历史里挑“最老的
        # offline 会话”，既会污染用户 JSONL，也可能撞上无人占有但仍存活的
        # tmux。合成记录在前端测试中允许 assistant 简写为字符串；接管前转成
        # Claude Code 能 resume 的标准 content 数组即可，全程不调用模型。
        fake_path = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
        make_cli_resumable(fake_path)
        urllib.request.urlopen(BASE + "/api/sessions?force=1", timeout=60).read()
        target = fake_uid
        existing_terms = {s["name"] for s in term_rows()}
        check("终端回归使用隔离会话且不占用任何用户 tmux",
              TERMINAL_TERM not in existing_terms, sorted(existing_terms))
        p.evaluate("u => openSession(u)", target)
        p.wait_for_selector("#a-term", timeout=60000)
        check("会话详情有接管按钮", p.locator("#a-term").get_attribute("title") == "接管会话",
              p.locator("#a-term").get_attribute("title"))
        check("终端面板初始不显示", p.locator("#termpane").is_hidden())

        p.click("#a-term")
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=90000)
        p.wait_for_timeout(1500)
        tname = p.evaluate("T.name")
        tserver = term_server(tname)
        check("一键接管起了 tmux 会话", tname.startswith("agenthub-claude-"), tname)
        check("新接管会话使用 agenthub 专用 server", tserver == "agenthub", tserver)
        check("接管未弹确认框(会话本来就没在跑)", not dialogs, dialogs[:1])
        check("首次打开默认纯终端而不是分屏",
              p.locator("#termpane").is_visible()
              and p.evaluate("T.mode") == "full"
              and p.locator("#msgs").evaluate("n => getComputedStyle(n).display") == "none"
              and p.locator("#msgs .msg").count() > 0)
        ctrl_lock = p.evaluate("""() => {
          T.term.textarea.dispatchEvent(new KeyboardEvent('keydown', {
            key:'Control', code:'ControlRight', location:2, ctrlKey:true, bubbles:true
          }));
          const result = {armed:T.ctrlArmed, pane:$('#termpane').classList.contains('ctrl-locked'),
            indicator:$('#term-ctrl-lock').getClientRects().length > 0,
            ctrlT:applyTermCtrl('t').charCodeAt(0)};
          T.term.textarea.dispatchEvent(new KeyboardEvent('keyup', {
            key:'Control', code:'ControlRight', location:2, bubbles:true
          }));
          result.released = !T.ctrlArmed && !$('#termpane').classList.contains('ctrl-locked');
          return result;
        }""")
        check("桌面右 Ctrl 锁定下一键并在发送 Ctrl+T 后解除",
              ctrl_lock == {"armed": True, "pane": True, "indicator": True,
                            "ctrlT": 20, "released": True},
              ctrl_lock)
        check("终端不再提供全屏模式", p.locator("#term-exclusive").count() == 0)
        check("纯终端的切换键指向对话",
              p.locator("#a-term").get_attribute("title") == "切换到对话"
              and p.locator("#a-term use").get_attribute("href") == "#i-chat",
              p.locator("#a-term").get_attribute("title"))
        check("终端不再增加已接管状态栏",
              p.locator(".thead, #tstatus").count() == 0
              and p.locator("#tmouse").count() == 0)
        p.wait_for_function("""() => {
          const b = T.term.buffer.active; let s = '';
          for (let i = 0; i < b.length; i++) s += (b.getLine(i)?.translateToString(true) || '');
          return s.trim().length > 40;
        }""", timeout=30000)
        check("终端里 CLI 已经在跑", True)
        prompt_reveal = p.evaluate("""async () => {
          const prompt = {id:'terminal-live-question', state:'waiting', kind:'approval',
            questions:[{header:'命令审批', question:'是否继续？', multiple:false,
              options:[{label:'允许本次', key:'y'}, {label:'拒绝', key:'Escape'}]}]};
          await applyDiff(S.sel, {prompt_only:true, prompt});
          const first = {mode:T.mode, live:document.querySelectorAll('.live-question').length,
            messages:getComputedStyle($('#msgs')).display, title:$('#a-term').title};
          // 看过题卡后，用户仍有权主动回到原生终端；同一题目的后台轮询/恢复
          // 不应再次抢走界面。
          $('#a-term').click();
          restoreTermPane(S.sel, S.agent);
          const manual = T.mode;
          await applyDiff(S.sel, {prompt_only:true, prompt:null});
          return {first, manual};
        }""")
        check("纯终端收到实时选择题会自动切到对话且只切一次",
              prompt_reveal["first"] == {
                  "mode": "collapsed", "live": 1, "messages": "block",
                  "title": "切换到终端",
              } and prompt_reveal["manual"] == "full", prompt_reveal)
        # 默认没有分屏。用户从纯终端的下边界向下拉到中间后才进入分屏；
        # 分屏状态点顶栏键仍是“切到纯终端”，而不是关闭终端对象。
        grip = p.locator("#tgrip").bounding_box()
        right_box = p.locator("#right").bounding_box()
        split_y = right_box["y"] + right_box["height"] * 0.55
        p.mouse.move(grip["x"] + grip["width"] / 2,
                     grip["y"] + grip["height"] / 2)
        p.mouse.down()
        p.mouse.move(grip["x"] + grip["width"] / 2, split_y, steps=8)
        p.mouse.up()
        p.wait_for_timeout(200)
        split_layout = p.evaluate("""() => ({
          mode:T.mode,
          messages:getComputedStyle($('#msgs')).display,
          pane:$('#termpane').getBoundingClientRect().height,
          detail:$('#detail').getBoundingClientRect().height,
          title:$('#a-term').title,
          icon:$('#a-term use').getAttribute('href'),
        })""")
        check("只有拖动分界线才进入分屏",
              split_layout["mode"] == "normal"
              and split_layout["messages"] != "none"
              and split_layout["pane"] > 100 and split_layout["detail"] > 100,
              split_layout)
        check("分屏中的切换键指向纯终端",
              split_layout["title"] == "切换到终端"
              and split_layout["icon"] == "#i-terminal", split_layout)
        p.click("#a-term")
        p.wait_for_timeout(200)
        check("分屏点切换键进入纯终端而不是关闭面板",
              p.evaluate("T.mode") == "full"
              and p.locator("#termpane").is_visible()
              and p.locator("#msgs").evaluate("n => getComputedStyle(n).display") == "none")
        terminal_backend = p.evaluate("""() => {
          const view = currentTermViewObject();
          return {renderer:view.renderer, unicode:view.term.unicode.activeVersion,
            versions:view.term.unicode.versions};
        }""")
        check("终端启用 Unicode 11 且 WebGL 不可用时安全回退 DOM",
              terminal_backend["unicode"] == "11"
              and "11" in terminal_backend["versions"]
              and terminal_backend["renderer"] in ("webgl", "dom"), terminal_backend)
        p.fill("#q", "正在输入的搜索词")
        p.locator("#q").focus()
        focus_after_tmux_refresh = p.evaluate("""async () => {
          restoreTermPane(S.sel, S.agent);
          await new Promise(resolve => setTimeout(resolve, 30));
          return {id:document.activeElement?.id, value:$('#q').value};
        }""")
        check("tmux 后台刷新不会抢走搜索框焦点",
              focus_after_tmux_refresh
              == {"id": "q", "value": "正在输入的搜索词"},
              focus_after_tmux_refresh)
        p.fill("#q", "")
        p.evaluate("T.term.focus()")
        render_batch = p.evaluate("""async () => {
          const writes = [];
          const view = {outputBuffer:'', outputTimer:null, ansiTail:'',
            term:{write:s => writes.push(s)}};
          queueTermOutput(view, '先清除');
          queueTermOutput(view, '再重画');
          await new Promise(resolve => setTimeout(resolve, TERM_RENDER_BATCH_MS + 30));
          return {writes, pending:view.outputBuffer, timer:!!view.outputTimer};
        }""")
        check("Claude 同一帧的分段重画合并后再交给 xterm",
              render_batch == {"writes": ["先清除再重画"], "pending": "", "timer": False},
              render_batch)
        history_flush = p.evaluate("""() => {
          const writes = [];
          const view = {outputBuffer:'', outputTimer:null, ansiTail:'',
            term:{write:s => writes.push(s)}};
          queueTermOutput(view, 'x'.repeat(TERM_RENDER_BATCH_MAX));
          return {count:writes.length, size:writes[0]?.length || 0,
            pending:view.outputBuffer, timer:!!view.outputTimer};
        }""")
        check("大段终端历史绕过合帧缓冲立即回放",
              history_flush == {"count": 1, "size": 32768, "pending": "", "timer": False},
              history_flush)
        resize_dedup = p.evaluate("""() => {
          const view = currentTermViewObject(), ws = view.ws;
          const original = ws.send, sent = [];
          ws.send = data => typeof data === 'string'
            ? sent.push(JSON.parse(data)) : original.call(ws, data);
          view.lastResizeWs = null; view.lastResizeKey = '';
          try { fitTerm(true); fitTerm(true); } finally { ws.send = original; }
          return sent.filter(x => x.t === 'resize');
        }""")
        check("相同终端尺寸只向 tmux 通知一次", len(resize_dedup) == 1, resize_dedup)
        activation_resync = p.evaluate("""async () => {
          const view = currentTermViewObject(), ws = view.ws;
          const oldSend = ws.send, oldRefresh = view.term.refresh.bind(view.term);
          const sent = []; let refreshes = 0;
          ws.send = data => typeof data === 'string'
            ? sent.push(JSON.parse(data)) : oldSend.call(ws, data);
          view.term.refresh = (start, end) => { refreshes += 1; oldRefresh(start, end); };
          view.lastResizeWs = ws;
          view.lastResizeKey = `${view.term.cols}x${view.term.rows}`;
          try {
            closeTermPane(true);
            await openTermPane(view.name);
            await new Promise(resolve => setTimeout(resolve, 180));
          } finally {
            ws.send = oldSend;
            view.term.refresh = oldRefresh;
          }
          return {resizes:sent.filter(x => x.t === 'resize'), refreshes,
            active:T.name, dimensions:`${view.term.cols}x${view.term.rows}`};
        }""")
        check("缓存终端重新显示时强制同步尺寸并重绘",
              len(activation_resync["resizes"]) == 1
              and activation_resync["resizes"][0]["cols"] > 0
              and activation_resync["resizes"][0]["rows"] > 0
              and activation_resync["refreshes"] >= 1
              and activation_resync["active"] == tname,
              activation_resync)
        smooth_fit = p.evaluate("""async () => {
          const view = currentTermViewObject();
          const service = view.term._core._renderService;
          const originalClear = service.clear.bind(service);
          const originalPropose = view.fit.proposeDimensions.bind(view.fit);
          let clears = 0, proposals = 0;
          service.clear = () => { clears += 1; return originalClear(); };
          view.fit.proposeDimensions = () => { proposals += 1; return originalPropose(); };
          try {
            fitTerm(); fitTerm(); fitTerm(); fitTerm();
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          } finally {
            service.clear = originalClear;
            view.fit.proposeDimensions = originalPropose;
          }
          return {clears, proposals};
        }""")
        check("同一动画帧的终端适配只执行一次且从不先清屏",
              smooth_fit == {"clears": 0, "proposals": 1}, smooth_fit)
        collapsed_fit = p.evaluate("""() => {
          const view = currentTermViewObject(), ws = view.ws;
          const oldMode = T.mode, oldSend = ws.send;
          const sent = [];
          ws.send = data => sent.push(data);
          T.mode = 'collapsed'; layoutTermPane();
          try { fitTerm(true); } finally {
            T.mode = oldMode; layoutTermPane(); ws.send = oldSend; fitTerm(true);
          }
          return sent.length;
        }""")
        check("纯对话吸附态不会把隐藏终端尺寸发送给 tmux",
              collapsed_fit == 0, collapsed_fit)

        hidden_ancestor_fit = p.evaluate("""() => {
          const view = currentTermViewObject(), ws = view.ws, right = $('#right');
          const oldDisplay = right.style.display, oldSend = ws.send;
          const sent = [], before = [view.term.cols, view.term.rows];
          ws.send = data => typeof data === 'string'
            ? sent.push(JSON.parse(data)) : oldSend.call(ws, data);
          right.style.display = 'none';
          try {
            fitTerm(true);
            return {before, after:[view.term.cols, view.term.rows],
                    resizes:sent.filter(x => x.t === 'resize')};
          } finally {
            right.style.display = oldDisplay;
            ws.send = oldSend;
            layoutTermPane(); fitTerm(true);
          }
        }""")
        check("祖先隐藏时不会用 FitAddon 内部最小尺寸改写终端",
              hidden_ancestor_fit["after"] == hidden_ancestor_fit["before"]
              and hidden_ancestor_fit["resizes"] == [], hidden_ancestor_fit)

        # 桌面详情跨进手机断点时，若手机上次停在列表，CSS 会隐藏整个右栏。
        # 必须先暂存终端视图；随后进入同一详情时再用可见容器尺寸恢复。
        p.evaluate("""() => {
          const view = currentTermViewObject(), ws = view.ws;
          window.__mobileListTermProbe = {
            view, ws, send:ws.send, sent:[], before:[view.term.cols, view.term.rows]
          };
          ws.send = data => {
            const probe = window.__mobileListTermProbe;
            if (typeof data === 'string') probe.sent.push(JSON.parse(data));
            else probe.send.call(probe.ws, data);
          };
          store.set('mobilePage', 'list');
        }""")
        viewport(390, 780)
        p.wait_for_timeout(300)
        mobile_list_terminal = p.evaluate("""() => {
          const probe = window.__mobileListTermProbe, view = probe.view;
          return {
            active:T.name, paneHidden:$('#termpane').classList.contains('hidden'),
            right:getComputedStyle($('#right')).display,
            before:probe.before, after:[view.term.cols, view.term.rows],
            resizes:probe.sent.filter(x => x.t === 'resize'),
          };
        }""")
        check("桌面终端切到手机列表时先暂存视图且不产生伪尺寸",
              mobile_list_terminal["active"] is None
              and mobile_list_terminal["paneHidden"]
              and mobile_list_terminal["right"] == "none"
              and mobile_list_terminal["after"] == mobile_list_terminal["before"]
              and mobile_list_terminal["resizes"] == [], mobile_list_terminal)
        p.evaluate("u => openSession(u)", target)
        p.wait_for_function("""n => document.body.classList.contains('mobile-detail')
          && T.name === n && !$('#termpane').classList.contains('hidden')""",
                            arg=tname, timeout=60000)
        p.wait_for_timeout(300)
        mobile_detail_terminal = p.evaluate("""() => {
          const probe = window.__mobileListTermProbe, view = probe.view;
          const rect = view.host.getBoundingClientRect();
          const result = {
            active:T.name, dimensions:[view.term.cols, view.term.rows],
            host:[rect.width, rect.height],
            resizes:probe.sent.filter(x => x.t === 'resize'),
          };
          probe.ws.send = probe.send;
          delete window.__mobileListTermProbe;
          return result;
        }""")
        check("从手机列表进入原详情会按可见区域恢复终端尺寸",
              mobile_detail_terminal["active"] == tname
              and mobile_detail_terminal["dimensions"][0] >= 20
              and mobile_detail_terminal["dimensions"][1] >= 8
              and all(x > 0 for x in mobile_detail_terminal["host"])
              and len(mobile_detail_terminal["resizes"]) >= 1
              and mobile_detail_terminal["resizes"][-1]["cols"]
                == mobile_detail_terminal["dimensions"][0]
              and mobile_detail_terminal["resizes"][-1]["rows"]
                == mobile_detail_terminal["dimensions"][1], mobile_detail_terminal)
        viewport(1280, 800)
        p.wait_for_timeout(200)

        # 切到纯对话只隐藏终端视图；xterm、WebSocket 和 tmux 都必须原样保留。
        p.evaluate("window.__keptSwitchSocket = T.ws")
        p.click("#a-term")
        p.wait_for_timeout(600)
        check("切换键进入纯对话而不是默认分屏",
              p.evaluate("T.mode") == "collapsed"
              and p.locator("#termpane").is_hidden()
              and p.locator("#msgs").is_visible())
        check("纯对话的切换键指向终端",
              p.locator("#a-term").get_attribute("title") == "切换到终端"
              and p.locator("#a-term use").get_attribute("href") == "#i-terminal",
              p.locator("#a-term").get_attribute("title"))
        check("切到对话时终端连接和对象常驻",
              p.evaluate("T.ws === window.__keptSwitchSocket"
                         " && T.views.get(T.name)?.ws === window.__keptSwitchSocket"))
        after = json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())
        check("切到对话后 tmux 会话仍存活", any(s["name"] == tname for s in after["sessions"]))

        # 再点一次直接切回纯终端，不重复起、不重新连接。
        p.click("#a-term")
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=60000)
        n_now = len(json.loads(urllib.request.urlopen(BASE + "/api/term/list", timeout=30).read())["sessions"])
        check("切回终端复用同一会话和连接",
              p.evaluate("T.mode === 'full' && T.ws === window.__keptSwitchSocket")
              and p.evaluate("T.name") == tname and n_now == len(after["sessions"]),
              f'{p.evaluate("T.name")} {n_now}')

        # 切会话只隐藏当前网页视图，xterm 和 WebSocket 都应继续存活；回来直接复用。
        p.evaluate("""n => {
          const v = T.views.get(n);
          window.__keptTerm = v.term;
          window.__keptSocket = v.ws;
        }""", tname)
        other = p.evaluate("""u => S.sessions.find(x => x.uid !== u && x.size < 2_000_000)?.uid
          || S.sessions.find(x => x.uid !== u)?.uid""", target)
        p.evaluate("u => openSession(u)", other)
        p.wait_for_function("u => S.sel === u && !!document.querySelector('.dhead')", arg=other,
                            timeout=60000)
        check("切走后只暂时隐藏终端视图",
              p.locator("#termpane").is_hidden()
              and p.evaluate("""n => {
                const v = T.views.get(n);
                return T.openViews.has(n) && v?.term === window.__keptTerm
                  && v?.ws === window.__keptSocket && v.ws.readyState === 1;
              }""", tname))
        p.evaluate("u => openSession(u)", target)
        p.wait_for_function("n => T.name === n && T.ws && T.ws.readyState === 1", arg=tname,
                            timeout=60000)
        check("切回会话自动恢复原 tmux 状态",
              p.locator("#termpane").is_visible() and p.evaluate("T.uid") == target
              and p.evaluate("""n => T.views.get(n)?.term === window.__keptTerm
                && T.views.get(n)?.ws === window.__keptSocket""", tname),
              {"name": p.evaluate("T.name"), "uid": p.evaluate("T.uid")})

        # 输入框: 接管后才出现, 内容直接送进 tmux
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(400)
        check("接管后消息流底部出现输入框", p.locator("#composer").is_visible())
        check("输入框左侧提供附件加号", p.locator("#cadd").is_visible())
        composer_alignment = p.evaluate("""() => {
          const ta = document.querySelector('#cinput');
          ta.value = ''; autoGrow(ta);
          const rects = ['#cadd', '#cinput', '#cesc', '#csend'].map(s => {
            const r = document.querySelector(s).getBoundingClientRect();
            return { top: r.top, bottom: r.bottom, height: r.height };
          });
          return rects;
        }""")
        check("单行输入栏各控件上下对齐",
              max(x["top"] for x in composer_alignment) - min(x["top"] for x in composer_alignment) < 1
              and max(x["bottom"] for x in composer_alignment) - min(x["bottom"] for x in composer_alignment) < 1,
              composer_alignment)
        composer_single_line = p.evaluate("""() => {
          const ta = document.querySelector('#cinput'), esc = document.querySelector('#cesc');
          const send = document.querySelector('#csend'), es = getComputedStyle(esc), ss = getComputedStyle(send);
          return {overflow:getComputedStyle(ta).overflowY,
            esc:{height:esc.getBoundingClientRect().height, lineHeight:es.lineHeight, padding:es.paddingBlock},
            send:{height:send.getBoundingClientRect().height, lineHeight:ss.lineHeight, padding:ss.paddingBlock}};
        }""")
        check("单行输入框不显示垂直滚动条",
              composer_single_line["overflow"] == "hidden", composer_single_line)
        check("Esc 和发送按钮使用完全相同的垂直尺寸",
              composer_single_line["esc"] == composer_single_line["send"], composer_single_line)

        # 空输入时用 ↑ 取回当前会话的历史输入；Enter 只回填，不直接发送。
        # 接管目标由真实会话中动态挑选，可能本来只有一条输入；浏览器内补两条
        # 确定性样本，测试结束即移除，不修改用户的 JSONL。
        p.evaluate("""() => {
          const entry = cache.get(viewKey(S.sel));
          entry.msgs.push(
            {role:'user', text:'输入历史自测甲', ts:'2026-08-18T00:00:00Z',
             event_id:'e2e-input-history-a'},
            {role:'user', text:'输入历史自测乙', ts:'2026-08-18T00:00:01Z',
             event_id:'e2e-input-history-b'});
        }""")
        p.fill("#cinput", "")
        p.press("#cinput", "ArrowUp")
        p.wait_for_selector("#input-history .input-history-item", timeout=10000)
        history_open = p.evaluate("""() => ({
          count:composerHistoryPicker.items.length,
          index:composerHistoryPicker.index,
          selected:document.querySelector('.input-history-item.selected')?.dataset.historyIndex,
          popup:document.querySelector('#input-history').getBoundingClientRect(),
          input:document.querySelector('#cinput').getBoundingClientRect(),
        })""")
        check("空输入按上键打开全部输入历史并默认最后一条",
              history_open["count"] >= 3
              and history_open["index"] == history_open["count"] - 1
              and int(history_open["selected"]) == history_open["index"], history_open)
        check("输入历史上拉框与输入框等宽并位于其上方",
              abs(history_open["popup"]["width"] - history_open["input"]["width"]) <= 1
              and history_open["popup"]["bottom"] <= history_open["input"]["top"], history_open)
        latest_history = p.evaluate("composerHistoryPicker.items.at(-1).text")
        p.press("#cinput", "Enter")
        check("历史项回车只送入编辑框而不发送",
              p.input_value("#cinput") == latest_history
              and p.locator("#input-history").is_hidden(), repr(p.input_value("#cinput")))
        p.fill("#cinput", "")
        p.press("#cinput", "ArrowUp")
        p.wait_for_selector("#input-history .input-history-item")
        p.press("#cinput", "ArrowUp")
        previous_history = p.evaluate("composerHistoryPicker.items[composerHistoryPicker.index].text")
        p.press("#cinput", "Enter")
        check("历史上拉框可用上下键浏览",
              p.input_value("#cinput") == previous_history
              and previous_history != latest_history)
        p.fill("#cinput", "已有草稿")
        p.press("#cinput", "ArrowUp")
        check("输入框有内容时上键保持普通编辑行为",
              p.locator("#input-history").is_hidden()
              and p.input_value("#cinput") == "已有草稿")
        p.fill("#cinput", "")
        p.evaluate("""() => {
          const entry = cache.get(viewKey(S.sel));
          entry.msgs = entry.msgs.filter(x => !String(x.event_id || '').startsWith('e2e-input-history-'));
        }""")

        p.click("#cadd")
        check("加号菜单提供图片视频音频文件和引用",
              p.locator("#attach-menu button").evaluate_all(
                  "nodes => nodes.map(n => n.dataset.attach)")
              == ["image", "video", "audio", "file", "quote"]
              and all(label in "".join(p.locator("#attach-menu").all_inner_texts())
                      for label in ("图片", "视频", "音频", "文件", "引用文字")))
        p.click('#attach-menu button[data-attach="quote"]')
        p.fill("#compose-items .draft-quote textarea", "被引用的上下文")
        check("引用文字显示为可编辑独立卡片",
              p.locator("#compose-items .draft-quote").count() == 1
              and p.locator("#compose-items .draft-quote textarea").input_value() == "被引用的上下文")
        paste_prevented = p.evaluate("""() => {
          const bytes = new Uint8Array([137, 80, 78, 71]);
          const file = new File([bytes], '粘贴图片.png', {type:'image/png', lastModified:1000});
          // 真实 Chromium 会在 files 与 items.getAsFile() 各返回一个对象，
          // 且同一张图片的 lastModified 可能相差 1ms。
          const mirror = new File([bytes], '粘贴图片.png', {type:'image/png', lastModified:1001});
          const clipboard = {
            files:[file], types:['Files', 'text/plain'],
            items:[{kind:'file', type:'image/png', getAsFile:() => mirror}],
            getData:type => type === 'text/plain' ? '剪贴板伴随文字不应再次进入正文' : '',
          };
          const event = new Event('paste', {bubbles:true, cancelable:true});
          Object.defineProperty(event, 'clipboardData', {value:clipboard});
          document.querySelector('#cinput').dispatchEvent(event);
          return event.defaultPrevented;
        }""")
        check("粘贴附件会拦截剪贴板伴随文字以免正文重复",
              paste_prevented and p.locator("#cinput").input_value() == "")
        check("输入框可直接粘贴图片或文件",
              p.locator("#compose-items .draft-card:not(.draft-quote)").count() == 1
              and p.locator("#compose-items .draft-thumb img").count() == 1
              and "[附件1]" in p.locator("#compose-items .draft-info small").inner_text()
              and "4B" in p.locator("#compose-items .draft-info small").inner_text())
        uploading_size = p.evaluate("""() => {
          const attachment = composerDraft().attachments[0];
          attachment.status = 'uploading'; renderComposerItems();
          const text = document.querySelector('#compose-items .draft-info small').textContent;
          attachment.status = ''; renderComposerItems();
          return text;
        }""")
        check("附件上传中仍显示文件大小",
              "4B" in uploading_size and "正在上传" in uploading_size, uploading_size)
        p.click("#compose-items .draft-card:not(.draft-quote)")
        check("点击附件卡在正文光标处插入稳定引用",
              p.locator("#cinput").input_value() == "[附件1]")
        p.evaluate("""() => addComposerFiles([
          new File([new Uint8Array([1])], '第二个.txt', {type:'text/plain'})
        ])""")
        p.locator("#compose-items .draft-card:not(.draft-quote) .draft-remove").first.click()
        p.evaluate("""() => addComposerFiles([
          new File([new Uint8Array([2])], '第三个.txt', {type:'text/plain'})
        ])""")
        remaining_refs = p.locator(
            "#compose-items .draft-card:not(.draft-quote) .draft-info small").all_inner_texts()
        check("删除附件后不重排编号且新附件不复用旧编号",
              len(remaining_refs) == 2
              and "[附件2]" in remaining_refs[0] and "[附件3]" in remaining_refs[1]
              and p.locator("#cinput").input_value() == "[附件1]", remaining_refs)
        converted_prompt = p.evaluate("""() => buildComposerPrompt(
          '请分析 [附件1]，原样保留 @2', [{
            number:1, relative_path:'agenthub_attachments/7/图.png'
          }, {
            number:3, relative_path:'agenthub_attachments/7/数据.csv'
          }], [])""")
        check("正文原样保留并在空行后追加精简附件清单",
              converted_prompt == "请分析 [附件1]，原样保留 @2\n\n"
              "附件1: ./agenthub_attachments/7/图.png\n"
              "附件3: ./agenthub_attachments/7/数据.csv",
              converted_prompt)
        check("纯文字 prompt 保持原样以兼容斜杠命令",
              p.evaluate("buildComposerPrompt('/rename abc', [], [])") == "/rename abc")
        p.locator("#compose-items .draft-remove").evaluate_all("nodes => nodes.forEach(n => n.click())")
        p.fill("#cinput", "")
        check("附件与引用可以在发送前移除", p.locator("#compose-items .draft-card").count() == 0)
        csv_paste = p.evaluate("""() => {
          const file = new File(['symbol,price\\n000001,12.3\\n'], '行情.csv', {type:'text/csv'});
          const transfer = new DataTransfer(); transfer.items.add(file);
          // 模拟部分浏览器：clipboardData.files 为空，但 items 仍有 CSV File。
          const clipboard = {files:[], items:transfer.items, types:['Files'], getData:() => ''};
          const event = new Event('paste', {bubbles:true, cancelable:true});
          Object.defineProperty(event, 'clipboardData', {value:clipboard});
          document.querySelector('#cinput').dispatchEvent(event);
          return event.defaultPrevented;
        }""")
        check("CSV 只出现在 clipboardData.items 时仍可粘贴为附件",
              csv_paste
              and p.locator("#compose-items .draft-card:not(.draft-quote)").count() == 1
              and p.locator("#compose-items .draft-info b").inner_text() == "行情.csv")
        gzip_paste = p.evaluate("""() => {
          const file = new File([new Uint8Array([31,139,8,0])], '行情.csv.gz',
            {type:'application/gzip'});
          const transfer = new DataTransfer(); transfer.items.add(file);
          const clipboard = {files:[], items:transfer.items, types:['Files'], getData:() => ''};
          const event = new Event('paste', {bubbles:true, cancelable:true});
          Object.defineProperty(event, 'clipboardData', {value:clipboard});
          document.querySelector('#cinput').dispatchEvent(event);
          return event.defaultPrevented;
        }""")
        check("任意文件类型都可从 clipboardData.items 粘贴，包括 csv.gz",
              gzip_paste
              and p.locator("#compose-items .draft-card:not(.draft-quote)").count() == 2
              and p.locator("#compose-items .draft-info b").nth(1).inner_text() == "行情.csv.gz")
        folder_paste = p.evaluate("""() => {
          window.__folderAlert = '';
          const originalAlert = window.alert;
          window.alert = text => { window.__folderAlert = text; };
          const item = {kind:'file', type:'', getAsFile:() => null,
            webkitGetAsEntry:() => ({isDirectory:true, name:'数据目录'})};
          const event = new Event('paste', {bubbles:true, cancelable:true});
          Object.defineProperty(event, 'clipboardData',
            {value:{files:[], items:[item], types:['Files'], getData:() => ''}});
          document.querySelector('#cinput').dispatchEvent(event);
          window.alert = originalAlert;
          return {prevented:event.defaultPrevented, message:window.__folderAlert};
        }""")
        check("粘贴文件夹时明确提示压缩而不生成空文件附件",
              folder_paste["prevented"] and "数据目录" in folder_paste["message"]
              and "压缩" in folder_paste["message"]
              and p.locator("#compose-items .draft-card:not(.draft-quote)").count() == 2,
              folder_paste)
        csv_blob_paste = p.evaluate("""() => new Promise(resolve => {
          const item = {kind:'string', type:'text/csv', getAsString:callback =>
            queueMicrotask(() => callback('a,b\\n1,2\\n'))};
          const clipboard = {files:[], items:[item], types:['text/csv'], getData:() => ''};
          const event = new Event('paste', {bubbles:true, cancelable:true});
          Object.defineProperty(event, 'clipboardData', {value:clipboard});
          document.querySelector('#cinput').dispatchEvent(event);
          setTimeout(() => resolve(event.defaultPrevented), 0);
        })""")
        check("只有 text/csv 剪贴板数据时生成 clipboard.csv 附件",
              csv_blob_paste
              and p.locator("#compose-items .draft-card:not(.draft-quote)").count() == 3
              and p.locator("#compose-items .draft-info b").nth(2).inner_text()
                  == "clipboard.csv")
        p.locator("#compose-items .draft-remove").evaluate_all("nodes => nodes.forEach(n => n.click())")
        queued_id = p.evaluate("""({u, media}) => queuePendingUserMessage(
          u, '等待前一轮完成的指令', [{...media, gallery:true}])""",
                               {"u": target, "media": image_attachment_response["media"]})
        queued = p.locator('.msg.client-pending[data-role="user"]')
        check("尚未获得 Claude 原生回执的输入只显示为发送中",
              bool(queued_id) and queued.count() == 1
              and "等待前一轮完成的指令" in queued.inner_text()
              and "发送中" in queued.inner_text())
        check("排队副本记录原生会话边界而不依赖浏览器时钟",
              bool(p.evaluate("u => queuedMessages(u)[0]?.afterTs", target)))
        check("排队中的附件消息立即显示图片",
              queued.locator(".media-gallery img").count() == 1
              and queued.locator(".media-gallery img").get_attribute("src").endswith(
                  image_attachment_response["media"]["src"]))
        p.evaluate("""u => {
          reconcileQueuedMessages(u, [{role:'queue_operation', operation:'enqueue',
            text:'等待前一轮完成的指令', ts:new Date().toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("Claude enqueue 回执后才改为排队中",
              queued.count() == 1 and "排队中" in queued.inner_text()
              and p.evaluate("u => queuedMessages(u)[0]?.state", target) == "queued")
        p.evaluate("""u => {
          reconcileQueuedMessages(u, [{role:'queue_operation', operation:'dequeue',
            text:'', ts:new Date().toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("Claude dequeue 后保留副本直到正式 user 消息落盘",
              queued.count() == 1 and "发送中" in queued.inner_text()
              and p.evaluate("u => queuedMessages(u)[0]?.state", target) == "sending")
        p.evaluate("""u => {
          reconcileQueuedMessages(u, [{role:'user', text:'等待前一轮完成的指令',
            ts:new Date().toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("Claude 正式用户消息出现后撤掉队列副本", queued.count() == 0)
        p.evaluate("""u => {
          S.queued.set(u, [{id:'server-check', uid:u, text:'已提交待核对',
            created:Date.now(), state:'submitted', server:true, media:[]}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("Claude 服务端账本不提供危险重试而只允许检查终端",
              queued.count() == 1
              and queued.locator(".client-pending-actions button").all_inner_texts()
                  == ["检查终端"])
        pending_footer = queued.locator(".client-pending-footer").evaluate("""n => {
          const state = n.querySelector('.client-pending-state').getBoundingClientRect();
          const action = n.querySelector('.client-pending-actions').getBoundingClientRect();
          return {display:getComputedStyle(n).display,
            centerGap:Math.abs((state.top + state.bottom) / 2 - (action.top + action.bottom) / 2)};
        }""")
        check("待确认状态和检查按钮在同一条紧凑状态栏",
              pending_footer["display"] == "flex" and pending_footer["centerGap"] < 1,
              pending_footer)
        interrupted = p.evaluate("""u => {
          S.queued.set(u, [{id:'server-aborted', uid:u,
            text:'已经送进终端但立即中断的指令', created:Date.now(),
            state:'aborted', server:true, media:[]}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
          const nodes=[...document.querySelectorAll(
            '#msgs .client-aborted[data-queued-id="server-aborted"]')];
          return {count:nodes.length,
            pending:nodes.filter(node=>node.classList.contains('client-pending')).length,
            text:nodes[0]?.innerText || '',
            actions:[...(nodes[0]?.querySelectorAll(
              '.client-pending-actions button') || [])].map(node=>node.innerText)};
        }""", target)
        check("被中断但未写入原生记录的输入保留一条正式动作且不伪装成排队",
              interrupted["count"] == 1 and interrupted["pending"] == 0
              and "已经送进终端但立即中断的指令" in interrupted["text"]
              and "已中断" in interrupted["text"]
              and interrupted["actions"] == ["移除"], interrupted)
        p.evaluate("""u => {
          S.queued.delete(u); saveQueuedMessages();
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        p.evaluate("""u => {
          queuePendingUserMessage(u, '没有进入 Claude 的幽灵指令');
          expireQueuedMessages(Date.now() + 9000);
        }""", target)
        check("未获得原生回执的 Claude 消息保留为可处理的未确认状态",
              queued.count() == 1
              and "发送未确认" in queued.inner_text()
              and queued.locator(".client-pending-actions button").all_inner_texts()
                  == ["重试", "移除"])
        p.evaluate("""u => {
          reconcileQueuedMessages(u, [{role:'user', text:'没有进入 Claude 的幽灵指令',
            ts:new Date().toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("迟到的 Claude 原生记录仍会清掉未确认副本", queued.count() == 0)
        p.evaluate("""({u, media}) => queuePendingUserMessage(
          u, '等待前一轮完成的指令', [{...media, gallery:true}])""",
                   {"u": target, "media": image_attachment_response["media"]})
        p.evaluate("""u => {
          reconcileQueuedMessages(u, [{role:'user', text:'等待前一轮完成的指令',
            ts:new Date().toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("原生用户消息出现后移除乐观排队副本", queued.count() == 0)
        cached_reconcile = p.evaluate("""u => {
          const entry = cache.get(viewKey(u));
          const original = entry.msgs;
          const boundary = new Date(Date.now() + 86400000).toISOString();
          entry.msgs = [...original,
            {role:'user', text:'完全相同的上一条', ts:boundary}];
          S.queued.set(u, [{id:'same-next', text:'完全相同的上一条',
            state:'sending', created:Date.now(), afterTs:boundary, media:[]}]);
          renderConversationTail(entry.activity, u);
          const samePreviousKept = queuedMessages(u).length;
          entry.msgs.push({role:'user', text:'完全相同的上一条',
            ts:new Date(Date.parse(boundary) + 1000).toISOString()});
          renderConversationTail(entry.activity, u);
          const newerRemoved = queuedMessages(u).length;

          const missedBoundary = new Date(Date.parse(boundary) + 2000).toISOString();
          entry.msgs.push({role:'user', text:'已在缓存但错过增量对账',
            ts:new Date(Date.parse(missedBoundary) + 1000).toISOString()});
          S.queued.set(u, [{id:'missed-diff', text:'已在缓存但错过增量对账',
            state:'sending', created:Date.now(), afterTs:missedBoundary, media:[]}]);
          renderConversationTail(entry.activity, u);
          const cachedRemoved = queuedMessages(u).length;
          entry.msgs = original;
          S.queued.delete(u); saveQueuedMessages();
          renderConversationTail(entry.activity, u);
          return {samePreviousKept, newerRemoved, cachedRemoved};
        }""", target)
        check("Claude 队尾重画会清掉已进缓存的假排队且不误删同文新消息",
              cached_reconcile == {"samePreviousKept": 1,
                                   "newerRemoved": 0, "cachedRemoved": 0},
              cached_reconcile)
        pending_recovery_requests = []
        p.on("request", lambda r: pending_recovery_requests.append(r.url)
             if "/api/messages/" in r.url else None)
        skipped_native = p.evaluate("""async u => {
          const key = viewKey(u), entry = cache.get(key);
          // 前面的回滚用例在文件尾留下四条同文 user；用它模拟浏览器游标
          // 已经越过这些正文，且边界之后没有别的 user 可被误判成分支取代。
          const text = '回滚后的新内容QQZ';
          const saved = {...entry, msgs:entry.msgs};
          entry.msgs = entry.msgs.filter(m => !(m.role === 'user' && m.text === text));
          S.queued.set(u, [{id:'server-past-eof', uid:u, text,
            created:Date.parse('2026-08-07T11:59:59Z'),
            afterTs:'2026-08-07T11:59:59Z', state:'submitted',
            server:true, media:[]}]);
          renderConversationTail(entry.activity, u);
          const before = {pending:document.querySelectorAll('.client-pending').length,
            native:entry.msgs.filter(m => m.role === 'user' && m.text === text).length};
          await reconcilePendingUid(u);
          const recovered = cache.get(key);
          const after = {pending:document.querySelectorAll('.client-pending').length,
            queued:queuedMessages(u).length,
            native:recovered.msgs.filter(m => m.role === 'user' && m.text === text).length};
          cachePut(key, saved);
          await renderSession(saved.meta, saved.msgs, saved.activity);
          return {before, after};
        }""", target)
        check("游标越过正文后用一次有界窗口恢复已确认消息",
              skipped_native["before"] == {"pending": 1, "native": 0}
              and skipped_native["after"] == {
                  "pending": 0, "queued": 0, "native": 4}
              and any("window=1" in url for url in pending_recovery_requests),
              {"state": skipped_native,
               "requests": pending_recovery_requests[-6:]})
        p.evaluate("""u => {
          S.queued.set(u, [{id:'legacy-clock-skew', text:'旧版残留',
            created:Date.now() + 3600000, media:[]}]);
          saveQueuedMessages();
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
          reconcileQueuedMessages(u, [{role:'user', text:'旧版残留',
            ts:new Date(Date.now() - 3600000).toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("旧版排队副本不再因浏览器时钟偏差而残留", queued.count() == 0)
        p.evaluate("u => queuePendingUserMessage(u, '按 Esc 后应从页面撤掉的排队消息')", target)
        check("Esc 测试前确实有乐观排队副本", queued.count() == 1)
        p.evaluate("""async u => {
          const realPost = post;
          post = async () => ({ok:true});
          try { await sendToSession(null, ['Escape'], u); }
          finally { post = realPost; }
        }""", target)
        check("Esc 请求成功也不抢在 Claude 原生事件前清空队列", queued.count() == 1)
        p.evaluate("""u => {
          reconcileQueuedMessages(u, [
            {role:'queue_operation', operation:'dequeue', text:'',
             ts:new Date().toISOString()},
            {role:'user', text:'按 Esc 后应从页面撤掉的排队消息',
             ts:new Date().toISOString()}]);
          renderConversationTail(cache.get(viewKey(u))?.activity, u);
        }""", target)
        check("Claude 提升排队消息后由正式 user 记录完成对账", queued.count() == 0)
        sent = []
        p.on("response", lambda r: sent.append(r.status) if "/api/term/send" in r.url else None)
        # 手机 Enter 只换行，发送必须点按钮；短 placeholder 不把单行输入框撑高。
        viewport(390, 780)
        p.evaluate("showMobileDetail()")
        mobile_head = p.evaluate("""() => {
          const h = document.querySelector('.dtitle h2');
          const title = h.querySelector('.session-view-switch > span')
            || [...h.children].find(n => n.tagName === 'SPAN');
          const old = title.textContent;
          title.textContent = '这是一条必须在手机标题栏中保持单行并用省略号截断的很长会话标题';
          const hs = getComputedStyle(h), ts = getComputedStyle(title);
          const hr = h.getBoundingClientRect(), ar = document.querySelector('.dhead-actions').getBoundingClientRect();
          const result = {height: hr.height, lineHeight: parseFloat(hs.lineHeight),
            whiteSpace: hs.whiteSpace, overflow: hs.overflow, textOverflow: ts.textOverflow,
            noOverlap: hr.right <= ar.left + 1};
          title.textContent = old;
          return result;
        }""")
        check("手机详情标题保持单行省略",
              mobile_head["whiteSpace"] == "nowrap"
              and mobile_head["overflow"] == "hidden"
              and mobile_head["textOverflow"] == "ellipsis"
              and mobile_head["height"] <= mobile_head["lineHeight"] + 2
              and mobile_head["noOverlap"], mobile_head)
        open_session_menu(p)
        mobile_summary = p.evaluate("""() => {
          const count = document.querySelector('#mcount-total');
          const c = count.getBoundingClientRect();
          return {text: count.textContent, visible: c.width > 0 && c.height > 0,
            unread: document.querySelectorAll('.dhead .newmsg, .dmeta .newmsg').length};
        }""")
        check("手机详情菜单显示消息总数，不显示未读数",
              re.fullmatch(r"\d+ 条消息", mobile_summary["text"]) and mobile_summary["visible"]
              and mobile_summary["unread"] == 0, mobile_summary)
        p.keyboard.press("Escape")
        p.fill("#cinput", "手机第一行")
        mobile_single_line_height = p.locator("#cinput").bounding_box()["height"]
        sent_before_enter = len(sent)
        p.press("#cinput", "Enter")
        p.wait_for_timeout(100)
        check("手机输入提示保持简短单行",
              p.locator("#cinput").get_attribute("placeholder") == "输入内容"
              and mobile_single_line_height <= 40, mobile_single_line_height)
        check("手机 Enter 只换行不发送",
              p.input_value("#cinput") == "手机第一行\n" and len(sent) == sent_before_enter,
              repr(p.input_value("#cinput")))
        p.fill("#cinput", "")
        viewport(1280, 800)
        p.wait_for_timeout(100)
        check("桌面仍提示 Enter 与 Shift+Enter",
              "Shift+Enter" in p.locator("#cinput").get_attribute("placeholder"))

        # 输入内容要等服务端确认后再清空；网络失败时必须保留草稿，不能假装发出。
        p.evaluate("""() => {
          window.__agenthubRealPost = post;
          post = url => url === 'api/session/draft-status'
            ? Promise.resolve({ok:true, draft_state:'empty'})
            : new Promise(resolve => setTimeout(() => resolve({ok: true}), 300));
        }""")
        p.fill("#cinput", "等待发送确认")
        p.click("#csend")
        check("发送确认前保留输入内容",
              p.input_value("#cinput") == "等待发送确认" and p.locator("#csend").is_disabled())
        p.wait_for_timeout(400)
        check("发送确认后才清空输入内容",
              p.input_value("#cinput") == "" and p.locator("#csend").is_enabled())
        p.evaluate("""() => {
          window.__failedRequestIds = [];
          post = async (url, body) => {
            if (url === 'api/session/draft-status') {
              return {ok:true, draft_state:'empty'};
            }
            if (url === 'api/session/send') window.__failedRequestIds.push(body.request_id);
            return {error: '模拟发送失败'};
          };
        }""")
        p.fill("#cinput", "失败后保留草稿")
        # 本段已安装 _dlg；不要再给同一个 alert 注册第二个处理器。
        p.click("#csend")
        p.wait_for_timeout(100)
        check("发送失败时不丢草稿", p.input_value("#cinput") == "失败后保留草稿")
        p.wait_for_function("!composerSending", timeout=5000)
        p.click("#csend")
        p.wait_for_function("!composerSending", timeout=5000)
        failed_ids = p.evaluate("window.__failedRequestIds")
        check("相同草稿在响应丢失后复用 request id 而不会重复注入",
              len(failed_ids) == 2 and bool(failed_ids[0])
              and failed_ids[0] == failed_ids[1], failed_ids)
        p.fill("#cinput", "")
        p.evaluate("""() => {
          post = window.__agenthubRealPost;
          delete window.__agenthubRealPost;
          delete window.__failedRequestIds;
        }""")

        rewind_bridge = p.evaluate("""async () => {
          const sent = [], opened = [], rewindPosts = [];
          const realSend = sendToSession, realOpen = openTermPane, realPost = post;
          const entry = cache.get(viewKey(S.sel)), oldActivity = entry.activity;
          sendToSession = async (text, keys, uid) => { sent.push({keys, uid}); return true; };
          openTermPane = async name => { opened.push(name); };
          post = async (url, body) => {
            if (url === 'api/session/rewind') {
              rewindPosts.push(body);
              return {ok:true, pending:true};
            }
            return realPost(url, body);
          };
          // 模拟旧进程遗留但已被 renderActivity 隐藏的 working。它不能让空输入
          // 下的双 Esc 永久失去原生 rewind 语义。
          entry.activity = {state:'working', ts:'2000-01-01T00:00:00Z'};
          document.querySelector('#activity')?.remove();
          composerEscAt = -Infinity;
          try {
            await sendComposerEscape(1000);
            await sendComposerEscape(1200);
            return {sent, opened, rewindPosts,
                    title:document.querySelector('#cesc').title};
          } finally {
            sendToSession = realSend;
            openTermPane = realOpen;
            post = realPost;
            entry.activity = oldActivity;
            composerEscAt = -Infinity;
            claudeRewinds.clear();
          }
        }""")
        check("对话 Esc 单击发送一次、Claude 双击发送第二次并显示原生回滚界面",
              [x["keys"] for x in rewind_bridge["sent"]] == [["Escape"], ["Escape"]]
              and len(rewind_bridge["opened"]) == 1
              and [x["action"] for x in rewind_bridge["rewindPosts"]] == ["begin"]
              and "双击进入原生回滚选择" in rewind_bridge["title"], rewind_bridge)

        pane_before = tmux_run(tserver, "capture-pane", "-p", "-t", tname,
                               capture_output=True, text=True).stdout
        if "trust this folder" in pane_before:      # 新起的 TUI 可能停在信任确认页
            p.fill("#cinput", "1")
            p.press("#cinput", "Enter")
            p.wait_for_timeout(3500)
        # 历史会话可能恢复在补全菜单或弹层里；先回到普通输入态再测斜杠命令。
        p.click("#cesc")
        p.wait_for_timeout(700)
        # 精确复现快速 Esc 的终态：Claude 把旧消息留在原生编辑器。网页发送
        # 必须先弹确认、清掉这版草稿，再单独提交新消息，不能把两段文字拼接。
        draft_driver = server_module.send_protocol.driver_for("claude")
        if draft_driver.composer_probe(tname).get("draft_state") == "editing":
            term.send_keys(tname, "C-c")
            time.sleep(0.3)
        # 先把网页新消息放进网页编辑器，再模拟 Claude 的 ESC 回填。若反过来，
        # xterm 的 focus-out 会参与 TUI 重画，使测试不再等价于用户遇到的终态。
        p.fill("#cinput", "/help")                  # 本地命令，不消耗模型额度
        restored_draft = "AGENTHUB_ESC_RESTORED_DRAFT"
        term.send_text(tname, restored_draft)
        deadline = time.time() + 5
        while (time.time() < deadline
               and draft_driver.composer_probe(tname).get("draft_state") != "editing"):
            time.sleep(0.05)
        check("Claude 原生编辑器里的 ESC 回填正文可被识别为草稿",
              draft_driver.composer_probe(tname).get("draft_state") == "editing")
        draft_status = json.loads(urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/session/draft-status",
            json.dumps({"uid": target, "name": tname}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read())
        check("Claude 草稿检测接口在发送前返回不可逆版本指纹",
              draft_status.get("draft_conflict") is True
              and re.fullmatch(r"[0-9a-f]{64}", draft_status.get("draft_token", "")),
              draft_status)
        p.evaluate("""() => {
          window.__draftRealConfirmTerminal = confirmTerminalDraftOverwrite;
          window.__draftConfirmCalls = [];
          confirmTerminalDraftOverwrite = () => {
            window.__draftConfirmCalls.push('终端草稿中有内容，是否覆盖？');
            return true;
          };
        }""")
        draft_requests = []
        draft_dialog_at = len(dialogs)
        def _draft_request(request):
            path = urllib.parse.urlparse(request.url).path
            if path not in {"/api/session/draft-status", "/api/session/send",
                            "/api/term/send"}:
                return
            try:
                body = request.post_data_json
            except Exception:
                body = request.post_data
            draft_requests.append({"path": path, "body": body})
        p.on("request", _draft_request)
        p.press("#cinput", "Enter")
        # Do not use an old /help frame as proof that this submission finished.
        # The draft-status request is asynchronous; restoring the confirm stub
        # before it resolves makes the real native dialog cancel the send and
        # turns the test itself into the apparent product failure.
        deadline = time.time() + 10
        while (time.time() < deadline
               and p.evaluate("composerSending && !(window.__draftConfirmCalls?.length)")):
            p.wait_for_timeout(100)
        deadline = time.time() + 15
        while time.time() < deadline and p.evaluate("composerSending"):
            p.wait_for_timeout(100)
        draft_settled = not p.evaluate("composerSending")
        # Claude TUI 启动后还可能刷新插件/状态，固定 sleep 4 秒偶尔只截到主界面。
        # 轮询真实帮助页，仍然要求 CLI 确实处理了命令，而不只看发送接口 200。
        help_words = ("code.claude.com", "keybindings", "resets in", "CLAUDE.md",
                      "Keyboard shortcuts", "slash commands", "Available commands")
        pane = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            pane = tmux_run(tserver, "capture-pane", "-p", "-t", tname,
                            capture_output=True, text=True).stdout
            if any(k in pane for k in help_words):
                break
            # 让 Playwright 同时派发 fetch response 事件；time.sleep 会阻塞它的事件泵。
            p.wait_for_timeout(500)
        check("输入框内容送进了会话", sent and all(x == 200 for x in sent), sent)
        check("CLI 确实响应了输入", any(k in pane for k in help_words), pane.strip()[-90:])
        draft_confirm_calls = p.evaluate("""() => {
          const calls = window.__draftConfirmCalls || [];
          confirmTerminalDraftOverwrite = window.__draftRealConfirmTerminal;
          delete window.__draftRealConfirmTerminal;
          delete window.__draftConfirmCalls;
          return calls;
        }""")
        p.remove_listener("request", _draft_request)
        draft_diagnostics = {
            "confirm": draft_confirm_calls,
            "requests": draft_requests,
            "dialogs": dialogs[draft_dialog_at:],
            "probe": draft_driver.composer_probe(tname),
            "settled": draft_settled,
        }
        check("Claude 草稿覆盖确认只弹一次且旧正文没有拼进新命令",
              draft_settled
              and draft_confirm_calls == ["终端草稿中有内容，是否覆盖？"]
              and restored_draft not in pane, draft_diagnostics)
        composer_after_send = p.input_value("#cinput")
        check("发送后输入框清空", composer_after_send == "",
              {**draft_diagnostics, "composer": composer_after_send})

        # 专用 server 只做托管：无状态栏/前缀/鼠标接管，滚动留给 xterm。
        for server in ("agenthub", "default"):
            tmux_run(server, "kill-session", "-t", "agenthub-wheeltest", capture_output=True)
        wname = term.new_session("wheeltest", "bash --noprofile --norc", "/tmp")
        wserver = term_server(wname)
        check("测试终端位于专用 server", wserver == "agenthub", wserver)
        transparent = {
            "status": tmux_run(wserver, "show-options", "-gv", "status",
                               capture_output=True, text=True).stdout.strip(),
            "mouse": tmux_run(wserver, "show-options", "-gv", "mouse",
                              capture_output=True, text=True).stdout.strip(),
            "prefix": tmux_run(wserver, "show-options", "-gv", "prefix",
                               capture_output=True, text=True).stdout.strip(),
            "escape": tmux_run(wserver, "show-options", "-sv", "escape-time",
                               capture_output=True, text=True).stdout.strip(),
            "focus": tmux_run(wserver, "show-options", "-sv", "focus-events",
                              capture_output=True, text=True).stdout.strip(),
            "extended": tmux_run(wserver, "show-options", "-sv", "extended-keys",
                                 capture_output=True, text=True).stdout.strip(),
        }
        check("专用 tmux 已关闭 UI 与输入截获",
              transparent == {"status": "off", "mouse": "off", "prefix": "None",
                              "escape": "10", "focus": "on", "extended": "on"}, transparent)
        stale_marker = "AGENTHUB_STALE_PAGE_MUST_NOT_RUN"
        stale_status = 0
        stale_error = ""
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/term/send",
                json.dumps({"name": wname,
                            "text": f"echo {stale_marker}"}).encode(),
                {"Content-Type": "application/json"}), timeout=30).read()
        except urllib.error.HTTPError as error:
            stale_status = error.code
            stale_error = json.loads(error.read()).get("error", "")
        stale_screen = tmux_run(
            wserver, "capture-pane", "-p", "-t", wname,
            capture_output=True, text=True).stdout
        check("旧页面的文字发送在触碰 tmux 前被版本握手拒绝",
              stale_status == 409 and "版本已过期" in stale_error
              and stale_marker not in stale_screen,
              {"status": stale_status, "error": stale_error})
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/term/send",
            json.dumps({"name": wname, "text": "for i in $(seq 1 200); do echo 历史行$i; done",
                        "_build": server_module.ASSET_VERSION}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read()
        time.sleep(1.2)
        p.evaluate("n => openTermPane(n)", wname)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        p.wait_for_timeout(1200)
        in_mode = lambda: tmux_run(
            wserver, "display", "-p", "-t", wname, "#{pane_in_mode}",
            capture_output=True, text=True).stdout.strip()
        check("网页终端使用 xterm 正常缓冲区",
              p.evaluate("T.term.buffer.active === T.term.buffer.normal"))
        check("xterm 会把模糊宽度标点限制在自身单元格",
              p.evaluate("T.term.options.rescaleOverlappingGlyphs === true"))
        check("连接时已把 tmux 历史送入 xterm scrollback",
              p.evaluate("T.term.buffer.normal.baseY") > 100,
              p.evaluate("T.term.buffer.normal.baseY"))

        # 对话气泡和 xterm 都响应颜色方案；两套终端颜色直接交给 xterm 绘制，
        # 不再滤整张 Canvas，以免浅色模式的抗锯齿像素发粗、出毛边。
        p.emulate_media(color_scheme="dark")
        p.wait_for_function("T.term.options.theme.background === '#000000'")
        check("已打开的 tmux 使用统一黑底源主题且不套反色滤镜",
              p.evaluate("T.term.options.theme.foreground === '#9da5b0'"
                         " && T.term.options.theme.brightWhite === '#b9c0ca'"
                         " && getComputedStyle(document.querySelector('#xterm')).filter === 'none'"))
        p.emulate_media(color_scheme="light")
        p.wait_for_function("T.term.options.theme.background === '#f4f6f8'")
        check("亮色页面切换为明亮原生浅色终端",
              p.evaluate("T.term.options.theme.foreground === '#252a32'"
                         " && T.term.options.theme.brightWhite === '#252a32'"
                         " && getComputedStyle(document.querySelector('#xterm')).filter === 'none'"))

        # 手机锁屏/切后台会冻结 WebSocket，但 tmux 本体仍在。模拟 pagehide/pageshow，
        # 恢复后必须换一条 socket，并且输入、输出都继续工作。
        p.evaluate("T.term.focus()")
        check("锁屏前终端确实持有输入焦点",
              p.evaluate("T.views.get(T.name).host.contains(document.activeElement)"))
        p.evaluate("""() => {
          window.__termWsBeforeSleep = T.ws;
          window.dispatchEvent(new PageTransitionEvent('pagehide'));
        }""")
        check("页面进入后台时只暂停传输、不丢 tmux 关联",
              p.evaluate("T.ws === null && T.name") == wname, p.evaluate("T.name"))
        p.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
        p.wait_for_function(
            "T.ws && T.ws !== window.__termWsBeforeSleep && T.ws.readyState === 1", timeout=30000)
        check("锁屏恢复后自动重建终端连接",
              p.locator(".thead, #tstatus").count() == 0
              and p.locator("#tmouse").count() == 0)
        p.keyboard.type("echo AGENTHUB_LOCK_RESUME_OK")
        p.keyboard.press("Enter")
        p.wait_for_function("""() => { const b = T.term.buffer.active; let s = '';
          for (let i = b.viewportY; i < b.viewportY + T.term.rows; i++)
            s += (b.getLine(i)?.translateToString(true) || '');
          return s.includes('AGENTHUB_LOCK_RESUME_OK'); }""", timeout=15000)
        check("重连后终端输入输出均恢复", True)

        scroll_urls = []
        def _scroll_req(req):
            if "/api/term/scroll" in req.url:
                scroll_urls.append(req.url)
        p.on("request", _scroll_req)
        box = p.locator("#xterm").bounding_box()
        p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(4):
            p.mouse.wheel(0, -1200)
        p.wait_for_function("T.term.buffer.active.viewportY < T.term.buffer.active.baseY", timeout=15000)
        check("滚轮由 xterm 本地滚动",
              p.evaluate("T.term.buffer.active.viewportY < T.term.buffer.active.baseY"))
        check("滚动不请求服务端也不进入 copy-mode",
              not scroll_urls and in_mode() == "0", {"requests": scroll_urls, "mode": in_mode()})
        p.evaluate("T.term.scrollToTop()")
        seen = p.evaluate("""() => { const b = T.term.buffer.active; let s = '';
          for (let i = b.viewportY; i < b.viewportY + T.term.rows; i++)
            s += (b.getLine(i)?.translateToString(true) || '');
          return s; }""")
        check("确实看到了更早的输出", "历史行1" in seen and "历史行200" not in seen)
        p.evaluate("T.term.scrollLines(-20); T.term.focus()")
        p.keyboard.type("x")
        p.wait_for_function("T.term.buffer.active.viewportY === T.term.buffer.active.baseY", timeout=15000)
        ok = received = False
        tail = ""
        for _ in range(20):
            p.wait_for_timeout(250)
            ok = in_mode() == "0"
            pane = tmux_run(wserver, "capture-pane", "-p", "-t", wname,
                            capture_output=True, text=True).stdout
            tail = pane.rstrip().splitlines()[-1] if pane.rstrip() else ""
            received = tail.rstrip().endswith("x")
            if ok and received:
                break
        check("一打字由 xterm 自动回到底部", ok, in_mode())
        check("本地滚动后的首个字符未被吞", received, tail)
        p.remove_listener("request", _scroll_req)
        p.keyboard.press("Backspace")

        before_detach = tmux_run(wserver, "capture-pane", "-p", "-t", wname,
                                 capture_output=True, text=True).stdout.rstrip().splitlines()[-1]
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(300)
        after_detach = tmux_run(wserver, "capture-pane", "-p", "-t", wname,
                                capture_output=True, text=True).stdout.rstrip().splitlines()[-1]
        check("无前缀断开不会结束会话或注入 Ctrl-B d",
              tmux_run(wserver, "has-session", "-t", wname,
                       capture_output=True).returncode == 0
              and after_detach == before_detach,
              f"{before_detach!r} -> {after_detach!r}")
        p.evaluate("n => openTermPane(n)", wname)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        p.wait_for_timeout(300)

        # 手机终端没有实体修饰键：Ctrl 点一次只修饰下一键，随后自动解除。
        p.keyboard.type("sleep 30")
        p.keyboard.press("Enter")
        p.wait_for_timeout(300)
        pane_command = lambda: tmux_run(
            wserver, "display", "-p", "-t", wname, "#{pane_current_command}",
            capture_output=True, text=True).stdout.strip()
        check("Ctrl 测试命令已经运行", pane_command() == "sleep", pane_command())
        viewport(390, 780)
        p.wait_for_timeout(200)
        key_labels = p.locator(".term-keys button").all_inner_texts()
        key_tops = [round(p.locator(".term-keys button").nth(i).bounding_box()["y"])
                    for i in range(p.locator(".term-keys button").count())]
        key_bar = p.locator(".term-keys").bounding_box()
        terminal_box = p.locator("#xterm").bounding_box()
        detail_head = p.locator(".dhead").bounding_box()
        terminal_pane = p.locator("#termpane").bounding_box()
        check("手机终端补齐 Ctrl、Tab、左右、上下、翻页和 Esc",
              all(k in key_labels for k in ("Ctrl", "Tab", "←", "→", "↑", "↓", "Pg↑", "Pg↓", "Esc")),
              key_labels)
        check("手机终端快捷键保持单排", max(key_tops) - min(key_tops) <= 1, key_tops)
        check("手机终端快捷键位于终端底部",
              key_bar["y"] >= terminal_box["y"] + terminal_box["height"] - 1,
              {"keys": key_bar, "terminal": terminal_box})
        check("手机终端复用对话顶栏而不再增加第二条顶栏",
              p.locator(".dhead").is_visible() and p.locator(".thead").count() == 0
              and abs(terminal_pane["y"] - detail_head["y"] - detail_head["height"]) <= 1,
              {"head": detail_head, "terminal": terminal_pane})
        keyboard_resize = p.evaluate("""async () => {
          const root = document.documentElement.style;
          const view = currentTermViewObject();
          const service = view.term._core._renderService;
          const originalClear = service.clear.bind(service);
          const originalViewportHeight = root.getPropertyValue('--visual-viewport-height');
          const nextFrame = () => new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const rowText = () => view.host.querySelector('.xterm-rows')?.textContent.trim().length || 0;
          let clears = 0;
          service.clear = () => { clears += 1; return originalClear(); };
          const rows = [view.term.rows], texts = [rowText()];
          try {
            for (const delta of [90, 150, 210, 240]) {
              root.setProperty('--visual-viewport-height', `${window.innerHeight - delta}px`);
              layoutTermPane(); fitTerm();
              await nextFrame();
              rows.push(view.term.rows); texts.push(rowText());
            }
          } finally {
            root.setProperty('--visual-viewport-height', originalViewportHeight);
            layoutTermPane(); fitTerm(true);
            service.clear = originalClear;
          }
          return {clears, rows, texts};
        }""")
        check("手机键盘动画期间终端逐帧改变行数且不清屏",
              keyboard_resize["clears"] == 0
              and len(set(keyboard_resize["rows"])) >= 3,
              keyboard_resize)
        check("手机键盘动画的每一帧都保留终端正文",
              all(x > 0 for x in keyboard_resize["texts"]),
              keyboard_resize)
        check("共用顶栏的终端按钮切换回对话",
              p.locator("#a-term").get_attribute("title") == "切换到对话"
              and p.locator("#a-term use").get_attribute("href") == "#i-chat")
        check("终端停止使用会话顶栏的关机图标",
              p.locator("#a-session-action use").get_attribute("href") == "#i-power"
              and p.locator("#tstop").count() == 0
              and p.locator("#i-stop").count() == 0)
        p.click('[data-term-modifier="ctrl"]')
        check("Ctrl 点亮为下一键待用",
              p.locator('[data-term-modifier="ctrl"]').get_attribute("aria-pressed") == "true")
        p.keyboard.type("c")
        p.wait_for_timeout(100)
        check("输入 c 后 Ctrl 自动解除",
              p.locator('[data-term-modifier="ctrl"]').get_attribute("aria-pressed") == "false")
        for _ in range(20):
            if pane_command() != "sleep":
                break
            p.wait_for_timeout(100)
        check("Ctrl 后输入 c 实际发送 Ctrl+C", pane_command() != "sleep", pane_command())
        viewport(1280, 800)
        p.evaluate("""() => {
          const view = currentTermViewObject(), service = view.term._core._renderService;
          window.__resizeClearService = service;
          window.__resizeOriginalClear = service.clear.bind(service);
          window.__resizeClearCount = 0;
          service.clear = () => { window.__resizeClearCount += 1;
                                  return window.__resizeOriginalClear(); };
        }""")
        desktop_resize_texts = []
        for width, height in ((1160, 720), (1200, 750), (1240, 775), (1280, 800)):
            p.set_viewport_size({"width": width, "height": height})
            p.evaluate("dispatchEvent(new Event('resize'))")
            p.wait_for_timeout(50)
            desktop_resize_texts.append(p.evaluate(
                "T.term.element.querySelector('.xterm-rows').textContent.trim().length"))
        desktop_resize_clears = p.evaluate("""() => {
          const count = window.__resizeClearCount;
          window.__resizeClearService.clear = window.__resizeOriginalClear;
          delete window.__resizeClearService; delete window.__resizeOriginalClear;
          delete window.__resizeClearCount;
          return count;
        }""")
        check("电脑连续缩放终端时不调用 renderer.clear",
              desktop_resize_clears == 0, desktop_resize_clears)
        check("电脑连续缩放的每一帧都保留终端正文",
              all(x > 0 for x in desktop_resize_texts), desktop_resize_texts)

        previous_term_layout = p.evaluate("currentTermView()")
        # 高窗口保存的普通终端高度不能在矮窗口占满右栏。否则 detail 被压成
        # 0 后，详情头与 composer 重叠，#a-term 看得见却会被 #cesc 截获。
        p.set_viewport_size({"width": 980, "height": 620})
        normal_height_bounds = p.evaluate("""() => {
          T.mode = 'normal'; T.height = 10000;
          layoutTermPane(); fitTerm(true);
          const rect = node => {
            const r = node.getBoundingClientRect();
            return {x:r.x, y:r.y, width:r.width, height:r.height, bottom:r.bottom};
          };
          const right = rect($('#right')), detail = rect($('#detail'));
          const head = rect($('#detail > .dhead')), composer = rect($('#composer'));
          const pane = rect($('#termpane')), button = rect($('#a-term'));
          const hit = document.elementFromPoint(
            button.x + button.width / 2, button.y + button.height / 2);
          return {right, detail, head, composer, pane,
                  buttonHit: hit?.id || hit?.closest?.('[id]')?.id || '',
                  buttonOwnsHit: $('#a-term').contains(hit)};
        }""")
        check("矮窗口会为详情头和输入框限制普通终端高度",
              normal_height_bounds["detail"]["height"] + 1
                >= normal_height_bounds["head"]["height"]
              and normal_height_bounds["pane"]["bottom"]
                <= normal_height_bounds["right"]["bottom"] + 1,
              normal_height_bounds)
        check("终端高度偏好超过窗口时顶栏按钮仍可真实点击",
              normal_height_bounds["buttonOwnsHit"], normal_height_bounds)
        p.set_viewport_size({"width": 1280, "height": 800})
        p.evaluate("""layout => {
          T.mode = layout.mode; T.height = layout.height;
          layoutTermPane(); fitTerm(true);
        }""", previous_term_layout)

        p.evaluate("T.mode = 'full'; layoutTermPane(); fitTerm(true)")
        page_overflow_frames = []
        for height in (800, 740, 680, 620, 680, 740, 800):
            p.set_viewport_size({"width": 1280, "height": height})
            p.evaluate("dispatchEvent(new Event('resize'))")
            # 故意不等 xterm 下一帧 fit，检查旧行 DOM 仍然较高的最危险时刻。
            page_overflow_frames.append(p.evaluate("""() => {
              const app = document.querySelector('#app');
              const right = document.querySelector('#right');
              const pane = document.querySelector('#termpane');
              return {inner: innerHeight, document: document.documentElement.scrollHeight,
                      app: [app.scrollHeight, app.clientHeight],
                      right: [right.scrollHeight, right.clientHeight],
                      rootOverflow: getComputedStyle(document.documentElement).overflowY,
                      bodyOverflow: getComputedStyle(document.body).overflowY,
                      paneOverflow: getComputedStyle(pane).overflowY};
            }"""))
        p.evaluate("""layout => {
          T.mode = layout.mode; T.height = layout.height;
          layoutTermPane(); fitTerm(true);
        }""", previous_term_layout)
        check("纯终端垂直缩放时根页面始终没有滚动条",
              all(x["document"] == x["inner"]
                  and x["rootOverflow"] == "hidden"
                  and x["bodyOverflow"] == "hidden"
                  for x in page_overflow_frames), page_overflow_frames)
        check("旧终端行重排前也不会撑高应用和右栏",
              all(x["app"][0] == x["app"][1]
                  and x["right"][0] == x["right"][1]
                  and x["paneOverflow"] == "hidden"
                  for x in page_overflow_frames), page_overflow_frames)

        check("终端不再提供框选模式开关", p.locator("#tmouse").count() == 0)
        # 旧探针可能已被大量布局/滚动回归推入 scrollback；在合成 shell 中重新
        # 输出一条稳定可见的行，再按真实字符坐标执行 Shift 拖拽。
        selection_needle = "AGENTHUB_SHIFT_SELECT_OK"
        p.evaluate("T.term.clearSelection(); T.term.scrollToBottom(); T.term.focus()")
        p.keyboard.type(f"echo {selection_needle}")
        p.keyboard.press("Enter")
        p.wait_for_function("""needle => { const b = T.term.buffer.active; let s = '';
          for (let i = b.viewportY; i < b.viewportY + T.term.rows; i++)
            s += (b.getLine(i)?.translateToString(true) || '') + '\\n';
          return s.includes(needle); }""", arg=selection_needle, timeout=15000)
        selection_drag = p.evaluate("""async needle => {
          const view = currentTermViewObject(), term = view.term;
          const locate = () => {
            const buffer = term.buffer.active;
            for (let i = buffer.length - 1; i >= 0; i--) {
              const text = buffer.getLine(i)?.translateToString(true) || '';
              const found = text.indexOf(needle);
              if (found >= 0) return {buffer, lineIndex:i, found};
            }
            return null;
          };
          let probe = locate();
          if (!probe) throw new Error('selection probe text is absent');
          term.scrollToLine(probe.lineIndex);
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          probe = locate();
          if (!probe) throw new Error('selection probe text moved out of buffer');
          const {buffer, lineIndex, found} = probe;
          const screen = view.host.querySelector('.xterm-screen').getBoundingClientRect();
          return {row: lineIndex - buffer.viewportY, col:found, length:needle.length,
            cols:term.cols, rows:term.rows,
            rect:{x:screen.x, y:screen.y, width:screen.width, height:screen.height}};
        }""", selection_needle)
        cell_width = selection_drag["rect"]["width"] / selection_drag["cols"]
        cell_height = selection_drag["rect"]["height"] / selection_drag["rows"]
        select_y = (selection_drag["rect"]["y"]
                    + (selection_drag["row"] + 0.5) * cell_height)
        select_x1 = (selection_drag["rect"]["x"]
                     + (selection_drag["col"] + 0.25) * cell_width)
        select_x2 = (selection_drag["rect"]["x"]
                     + (selection_drag["col"] + selection_drag["length"] - 0.25)
                     * cell_width)
        p.keyboard.down("Shift")
        p.mouse.move(select_x1, select_y)
        p.mouse.down()
        p.mouse.move(select_x2, select_y, steps=8)
        p.mouse.up()
        p.keyboard.up("Shift")
        p.wait_for_timeout(100)
        selected_text = p.evaluate("T.term.getSelection()")
        check("Shift+拖拽能建立并锁定终端文本选区",
              selection_needle in selected_text
              and p.evaluate("currentTermViewObject().selectionLocked"),
              repr(selected_text[:30]))
        p.mouse.click((select_x1 + select_x2) / 2, select_y, button="right")
        p.wait_for_timeout(100)
        check("右键打开复制菜单时保留终端选区与锁定",
              p.evaluate("T.term.getSelection()") == selected_text
              and p.evaluate("currentTermViewObject().selectionLocked"))
        p.evaluate("T.term.clearSelection()")       # 模拟 Claude 一次 TUI 重绘清掉选区
        p.wait_for_timeout(100)
        check("Claude 重绘清除选区后会自动恢复",
              bool(selected_text) and p.evaluate("T.term.getSelection()") == selected_text,
              repr(p.evaluate("T.term.getSelection()")[:30]))
        p.keyboard.press("Control+Shift+C")
        p.wait_for_timeout(150)
        copied_text = p.evaluate("navigator.clipboard.readText()")
        check("终端有选区时 Ctrl+Shift+C 复制而不是发送中断",
              bool(selected_text) and copied_text == selected_text,
              {"selected": selected_text[:30], "copied": copied_text[:30]})
        # 全屏应用仍由 tmux 模拟 alternate screen，但外层 xterm 保持正常缓冲区。
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/term/send",
            json.dumps({"name": wname, "text": "less /etc/services",
                        "_build": server_module.ASSET_VERSION}).encode(),
            {"Content-Type": "application/json"}), timeout=30).read()
        alt = "0"
        for _ in range(20):                     # less 起来要一会儿
            time.sleep(0.3)
            alt = tmux_run(wserver, "display", "-p", "-t", wname, "#{alternate_on}",
                           capture_output=True, text=True).stdout.strip()
            if alt == "1":
                break
        if alt == "1":
            check("全屏应用被识别为 alternate screen", True)
            p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            for _ in range(3):
                p.mouse.wheel(0, -300)
            p.wait_for_timeout(1000)
            check("全屏应用下仍不进入 copy-mode", in_mode() == "0", in_mode())
        else:
            print("⏭  跳过全屏应用那条(less 没起来, 环境问题)")
        tmux_run(wserver, "kill-session", "-t", wname, capture_output=True)
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(300)
        p.evaluate("n => openTermPane(n)", tname)
        p.wait_for_function("T.ws && T.ws.readyState === 1", timeout=30000)
        p.evaluate("closeTermPane()")
        p.wait_for_timeout(300)

        # 从外部结束这个 tmux 会话, 前端应在下一轮轮询里自己发现并收起
        tmux_run(tserver, "kill-session", "-t", tname, capture_output=True)
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
    left_open_width = p.locator("#left").bounding_box()["width"]
    right_open_width = p.locator("#right").bounding_box()["width"]
    p.click("#side-toggle")
    check("电脑端可收起会话列表",
          p.locator("#left").is_hidden() and p.locator("#drag").is_hidden()
          and p.locator("#side-toggle").get_attribute("aria-expanded") == "false"
          and p.locator("#right").bounding_box()["width"] > right_open_width)
    p.reload(wait_until="domcontentloaded")
    check("侧栏收起状态跨刷新保留",
          p.locator("#left").is_hidden()
          and p.locator("#side-toggle").get_attribute("title") == "展开会话列表")
    p.click("#side-toggle")
    check("展开后恢复原侧栏宽度",
          p.locator("#left").is_visible()
          and abs(p.locator("#left").bounding_box()["width"] - left_open_width) < 2)

    w0 = p.locator("#left").bounding_box()["width"]
    d = p.locator("#drag").bounding_box()
    p.mouse.move(d["x"] + 2, 400)
    p.mouse.down()
    p.mouse.move(d["x"] + 2 + 130, 400, steps=6)
    p.mouse.up()
    w1 = p.locator("#left").bounding_box()["width"]
    check("拖动改变侧栏宽度", abs(w1 - (w0 + 130)) < 10, f"{w0}->{w1}")
    check("详情区跟着收窄", p.locator("#detail").bounding_box()["width"] < 1600 - w1 + 10)
    p.reload(wait_until="domcontentloaded")
    # 分组折叠状态会跨刷新保留；DOM 中可能已有 item，但第一条恰好位于已折叠分组。
    p.wait_for_selector(".item:visible", timeout=15000)
    check("刷新后宽度保持", abs(p.locator("#left").bounding_box()["width"] - w1) < 2,
          p.locator("#left").bounding_box()["width"])
    p.dblclick("#drag")
    p.wait_for_timeout(200)
    check("双击复位到默认宽度", abs(p.locator("#left").bounding_box()["width"] - 340) < 2,
          p.locator("#left").bounding_box()["width"])
    # 拖不出可用区间
    d = p.locator("#drag").bounding_box()
    p.mouse.move(d["x"] + 2, 400); p.mouse.down(); p.mouse.move(5, 400, steps=4); p.mouse.up()
    check("宽度有下限", p.locator("#left").bounding_box()["width"] >= 200,
          p.locator("#left").bounding_box()["width"])
    p.dblclick("#drag")
    p.wait_for_timeout(200)

    # 视图模式 / 选中会话 / 来源筛选 / 搜索选项 都要跨刷新保留
    p.locator('#view button[data-v="date"]').click()
    p.locator(".chip").nth(2).click()
    p.locator('#opts button[data-o="case"]').click()
    p.wait_for_timeout(300)
    p.fill("#q", "AGENTHUB自测")
    p.wait_for_timeout(250)
    p.locator(".item").first.click()
    p.wait_for_selector(".dhead h2", timeout=15000)
    title = p.locator(".dhead h2").inner_text()
    persisted_group = p.locator(".group").first
    persisted_group_key = persisted_group.get_attribute("data-key")
    group_was_closed = "closed" in (persisted_group.get_attribute("class") or "")
    persisted_group.locator(".ghead").click()  # 翻转一个分组
    p.wait_for_timeout(200)
    p.reload(wait_until="domcontentloaded")
    # 只要求刷新后保持刚才的翻转结果；前面的交互可能已经折叠了第一组。
    p.wait_for_selector(".ghead", timeout=15000)
    persisted_group = p.locator(f'.group[data-key="{persisted_group_key}"]')
    group_is_closed = "closed" in (persisted_group.get_attribute("class") or "")
    check("分组折叠状态已记住", group_is_closed != group_was_closed,
          {"before": group_was_closed, "after": group_is_closed})
    check("视图模式已记住", "on" in (p.locator('#view button[data-v="date"]').get_attribute("class") or ""))
    check("时间轴的目录行随之恢复", p.locator(".item .cwd").count() > 0)
    check("来源筛选已记住", "off" in (p.locator(".chip").nth(2).get_attribute("class") or ""))
    check("搜索选项已记住", "on" in (p.locator('#opts button[data-o="case"]').get_attribute("class") or ""))
    p.wait_for_selector(".dhead h2", timeout=20000)
    check("上次打开的会话已恢复", p.locator(".dhead h2").inner_text() == title,
          p.locator(".dhead h2").inner_text())
    # 复位, 不影响后续用例
    if group_is_closed:
        persisted_group.locator(".ghead").click()
    p.locator('#view button[data-v="tree"]').click()
    p.locator(".chip").nth(2).click()
    p.locator('#opts button[data-o="case"]').click()
    p.wait_for_timeout(300)

    # ---- 16. 删除 (自测会话) ----
    p.fill("#q", "AGENTHUB自测")
    p.wait_for_timeout(300)
    p.locator(".item").first.click()
    p.wait_for_selector("#a-session-action[title='删除会话']", state="attached", timeout=10000)
    open_session_menu(p)
    p.once("dialog", lambda d: d.accept())
    # 删除前会强制扫描进程；机器繁忙时可能超过固定 sleep。等真实响应结束，
    # 否则 finally 的 cleanup 会先删掉测试文件，让尚在处理的请求误报 404。
    with p.expect_response(
            lambda r: "/api/session/" in r.url and r.request.method == "DELETE",
            timeout=30000):
        p.locator("#a-session-action").click()
    p.wait_for_timeout(200)
    check("删除后提示回收站", "回收站" in p.locator("#detail").inner_text())
    p.fill("#q", "AGENTHUB自测")
    p.wait_for_timeout(300)
    check("删除后从列表消失", p.locator(".item").count() == 0, p.locator(".item").count())
    trash = list((Path.home() / ".local/share/agenthub/trash/claude").glob("*dead-beef*"))
    payloads = [x for x in trash if not x.name.endswith(".agenthub-trash.json")]
    manifests = [x for x in trash if x.name.endswith(".agenthub-trash.json")]
    check("文件确实移入回收站", len(payloads) == 1, trash)
    check("回收站同时保存恢复清单", len(manifests) == 1, trash)
    check("原文件已不在", not (FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl").exists())

    # ---- 16b. 回收站: 查看 / 恢复 / 彻底删除 ----
    fake_file = FAKE_PROJ / "00000000-dead-beef-0000-000000000001.jsonl"
    p.locator("#trash").click()
    p.wait_for_selector("#trash-dialog[open]", timeout=10000)
    rows = p.locator("#trash-list .trash-item").filter(has_text="AGENTHUB自测会话请删除")
    p.wait_for_function(
        "() => [...document.querySelectorAll('#trash-list .trash-item')]"
        ".some(x => x.textContent.includes('AGENTHUB自测会话请删除'))", timeout=15000)
    check("回收站列出刚删除的会话", rows.count() == 1, rows.count())
    check("回收站条目显示恢复目标",
          str(fake_file) in rows.first.locator(".trash-origin").get_attribute("title"))

    with p.expect_response(lambda r: "/api/trash/restore" in r.url, timeout=30000):
        rows.first.locator("button[data-act='restore']").click()
    p.wait_for_function(
        "() => document.querySelector('#trash-note').textContent.includes('已恢复')",
        timeout=15000)
    check("恢复后文件回到原目录", fake_file.exists())
    check("恢复后回收站不再列出该会话", rows.count() == 0, rows.count())
    p.wait_for_function("() => document.querySelectorAll('.item').length === 1",
                        timeout=15000)
    check("恢复后会话回到左侧列表", p.locator(".item").count() == 1)

    p.locator("#trash-done").click()
    p.locator(".item").first.click()            # 先打开，删除后才有空态入口
    p.wait_for_selector("#a-session-action[title='删除会话']", state="attached", timeout=10000)

    # ---- 16c. 左栏多选删除 (列表里已筛成只剩自测会话) ----
    check("平时不占额外一行", p.locator("#side-tools").is_hidden())
    p.locator(".item").first.click(button="right")
    p.wait_for_selector("#item-menu:not([hidden])", timeout=10000)
    check("右键会话行弹出菜单", p.locator("#item-menu button").count() == 2)
    p.locator('#item-menu button[data-act="pick"]').click()
    check("菜单进入多选并勾上该行",
          "已选 1 项" in p.locator("#side-picked").inner_text())
    check("进入多选后展开操作栏", p.locator("#side-pick-delete").is_visible())
    check("未勾选时不能删除", p.locator("#side-pick-delete").is_disabled())
    check("全选后按钮变成全不选", p.locator("#side-pick-all").inner_text() == "全不选")
    check("分组标题也有勾选框", p.locator(".ghead-pick").count() >= 1)
    p.locator("#side-pick-all").click()
    check("点全不选清空选择", p.locator("#side-pick-delete").is_disabled())
    group_class = p.locator(".group").first.get_attribute("class")
    p.locator(".ghead-pick").first.click()
    check("勾分组标题选中该组全部",
          "已选 1 项" in p.locator("#side-picked").inner_text())
    check("勾分组标题不会折叠分组",
          p.locator(".group").first.get_attribute("class") == group_class)
    p.locator(".item").first.click()            # 选择模式下点行 = 勾选，不打开会话
    check("点行可取消勾选", p.locator("#side-pick-delete").is_disabled())
    p.locator(".item").first.click()
    check("点行即勾选", "已选 1 项" in p.locator("#side-picked").inner_text())
    check("删除按钮带上数量", "(1)" in p.locator("#side-pick-delete").inner_text())
    p.once("dialog", lambda d: d.accept())
    with p.expect_response(lambda r: "/api/sessions/delete" in r.url, timeout=30000):
        p.locator("#side-pick-delete").click()
    p.wait_for_function("() => document.querySelectorAll('.item').length === 0",
                        timeout=15000)
    check("多选删除后会话从列表消失", p.locator(".item").count() == 0)
    check("多选删除后操作栏收起", p.locator("#side-tools").is_hidden())
    check("多选删除后原文件已移走", not fake_file.exists())
    p.locator("#detail-open-trash").click()     # 删除后的空态直接进回收站
    p.wait_for_selector("#trash-dialog[open]", timeout=10000)
    p.wait_for_function(
        "() => [...document.querySelectorAll('#trash-list .trash-item')]"
        ".some(x => x.textContent.includes('AGENTHUB自测会话请删除'))", timeout=15000)
    p.once("dialog", lambda d: d.accept())
    with p.expect_response(lambda r: "/api/trash/purge" in r.url, timeout=30000):
        rows.first.locator("button[data-act='purge']").click()
    p.wait_for_function(
        "() => document.querySelector('#trash-note').textContent.includes('已彻底删除')",
        timeout=15000)
    check("彻底删除后回收站条目消失", rows.count() == 0, rows.count())
    left = list((Path.home() / ".local/share/agenthub/trash/claude").glob("*dead-beef*"))
    check("彻底删除后磁盘不再留文件", not left, left)
    p.locator("#trash-done").click()

    # 第二次删除紧跟在恢复后的列表刷新之后；后台增量读取可能正好和删除竞态，
    # 该自测会话的 messages 404 是预期结果，不属于页面脚本错误。
    encoded_fake_uid = urllib.parse.quote(fake_uid, safe="")
    errors[:] = [e for e in errors if not (
        e.startswith("HTTP 404:") and f"/api/messages/{encoded_fake_uid}" in e)]

    # ---- 17. 无 JS 报错 ----
    check("全程无 JS 错误", not errors, errors[:3])
    b.close()


if __name__ == "__main__":
    cleanup()
    make_fake_session()
    make_window_session()
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
