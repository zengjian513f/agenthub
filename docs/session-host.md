# 会话宿主（tmux 的替代后端）

网页控制台之前完全依赖 tmux：会话独立于 Web 服务存活、字节流 attach、`send-keys`
输入，以及 `capture-pane` 加光标位置的屏幕读取。为了让 Windows 机器也能作为节点托管
原生 Claude / Codex 会话，`agenthub/host/` 提供了一个自制的会话宿主，覆盖同样四件事，
不依赖 tmux。

## 结构

- **每个会话一个独立进程**（`python3 -m agenthub.host run …`），没有中央守护进程。
  进程持有 pty 跑 CLI，把输出喂给自带的 VT 屏幕模型，并在本地 socket 上接受连接。
  Web 服务重启不影响 CLI；CLI 退出时宿主随之退出并清理文件（对应 tmux 的 `remain-on-exit off`）。
- 会话目录默认 `~/.local/share/agenthub/host/`（权限 `0700`，可用 `AGENTHUB_HOST_DIR` 覆盖）。
  每个会话有 `<name>.json`（名称、宿主 pid、CLI pid、cwd、尺寸、attached 状态）和
  `<name>.sock`；Windows 用 `127.0.0.1` 端口加随机 token 代替 unix socket。
  宿主启动失败的原因写在 `<name>.log`。
- `agenthub/host/screen.py` 是只实现 TUI 真正会用到子集的 VT100/xterm 模型：光标移动、擦除、
  插删行列、滚动区域、SGR、备用屏、自动换行、东亚宽字符与组合字符、有界历史（10000 行），
  并在没有终端连着时代答光标位置 / 设备属性查询。它只服务 capture / cursor 查询和 attach 回放；
  实时字节原样转发给浏览器，滚动由 xterm.js 自己的 scrollback 完成。
- Linux 上宿主进程会尽量通过 `systemd-run --user --scope` 放进独立的 transient scope，
  这样 `systemctl --user restart agenthub.service` 不会连带结束 CLI；没有用户 systemd 时退回
  `start_new_session` 的普通独立进程（`AGENTHUB_HOST_SCOPE=0` 可强制）。Windows 用
  `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB`。
- 与 `agenthub-tmux-host` 一样，CLI 经 `~/.local/bin/with-zshrc` 之类的包装启动以获得交互 shell
  的环境；`AGENTHUB_HOST_ENV_WRAPPER` 可以改路径，设为空字符串则不包装。

## 后端选择与共存

`agenthub/term.py` 现在是调度层：`term_tmux.py` 是原有的 tmux 后端，`term_host.py` 是宿主后端。

- 主后端由 `python3 -m agenthub.server --terminal-backend {auto,tmux,host}` 或环境变量
  `AGENTHUB_TERM_BACKEND` 决定，新会话在主后端创建。`auto` 在 Windows 取 `host`，其他平台仍取 `tmux`。
- 按名称操作（发送、截屏、attach、结束、改名）会在两个后端里查找会话，因此把节点切到 `host`
  之后，仍在 tmux 里跑的旧会话继续可用，直到自然结束。`/api/term/list` 里宿主会话的
  `server` 字段为 `host`。
- 宿主没有 copy-mode，`scroll`/`leave_copy_mode` 是空操作；`submit_text` 只在应用请求了
  bracketed paste 时才包起止序列，和 tmux `paste-buffer -p` 的行为一致。

## 命令行

```bash
python3 -m agenthub.host list                       # 列出宿主会话
python3 -m agenthub.host attach agenthub-claude-1234 # 手工接管, Ctrl-\ 退出且不影响会话
python3 -m agenthub.host capture NAME --lines 200 --plain
python3 -m agenthub.host send NAME "文本" --enter
python3 -m agenthub.host kill NAME [--force]
```

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_host.py'
```

覆盖屏幕模型、协议键名、真实 pty 的宿主进程生命周期（发送、截屏、attach 回放与实时输入、
resize、改名、退出清理、宿主在启动者退出后存活）、`term` 调度，以及通过真实 HTTP 服务和
WebSocket 的控制台往返。测试只用 `sh`，不启动付费 CLI。

## Windows 现状

`agenthub/host/ptyio.py` 的 ConPTY 后端基于 pywinpty 编写，尚未在真实 Windows 节点上验证。
把 Windows 机器接成节点还需要：`live.py` 的运行状态检测改用 psutil、`adapters.py` 识别
Windows 项目目录 slug 与盘符路径、文件管理器的根目录判断，以及用计划任务代替 systemd。
