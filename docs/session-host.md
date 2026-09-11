# 会话宿主（tmux 的替代后端）

网页控制台之前完全依赖 tmux：会话独立于 Web 服务存活、字节流 attach、`send-keys`
输入，以及 `capture-pane` 加光标位置的屏幕读取。为了让 Windows 机器也能作为节点托管
原生 Claude / Codex 会话，`host-rs/` 提供了一个自制的会话宿主，覆盖同样四件事，
不依赖 tmux。

宿主本体是 Rust 二进制；Python 侧只保留客户端（`agenthub/host/`），由 Web 服务用来
扫描会话目录、发控制请求和转发 attach 字节流。

## 结构

- **每个会话一个独立进程**（`agenthub-host run …`），没有中央守护进程。
  进程持有 pty 跑 CLI，把输出喂给自带的 VT 屏幕模型，并在本地 socket 上接受连接。
  Web 服务重启不影响 CLI；CLI 退出时宿主随之退出并清理文件（对应 tmux 的 `remain-on-exit off`）。
  每会话常驻内存 2–3 MB。
- 会话目录默认 `~/.local/share/agenthub/host/`（权限 `0700`，可用 `AGENTHUB_HOST_DIR` 覆盖）。
  每个会话有 `<name>.json`（名称、宿主 pid、CLI pid、cwd、尺寸、attached 状态）和
  `<name>.sock`；Windows 用 `127.0.0.1` 端口加随机 token 代替 unix socket。
  宿主启动失败的原因写在 `<name>.log`。
- pty 由 [`portable-pty`](https://crates.io/crates/portable-pty) 提供，同时覆盖 Unix pty 和
  Windows ConPTY；屏幕模型用 [`vt100`](https://crates.io/crates/vt100)，实测吞吐 44.8 MB/s，
  不会成为瓶颈。屏幕模型只服务 `capture` / `cursor` 查询和 attach 回放；
  实时字节原样转发给浏览器，滚动由 xterm.js 自己的 scrollback 完成。
- **模型与转发解耦**：读线程只做"转发给客户端 + 入队"，屏幕模型在独立线程里消费队列。
  读线程永不等模型——那会直接变成终端卡顿。积压超过 `BACKLOG_LIMIT`（32 MB）时丢掉最旧的
  一段，由 TUI 的下一次整屏重绘自然纠正。`capture` / `cursor` 先给模型最多
  `SCREEN_SYNC_TIMEOUT`（2 秒）追赶，并在应答里报出 `lag`（尚未进入模型的字节）和
  `dropped`（累计丢弃），调用方可据此判断这一帧是否可信。
  attach 的回放 = 历史 + 终端完整状态（`state_formatted`，含备用屏、DECCKM、bracketed paste）
  + 尚未喂入的原始字节，与客户端随后收到的实时字节严格接续。
- Linux 上宿主进程会尽量通过 `systemd-run --user --scope` 放进独立的 transient scope，
  这样 `systemctl --user restart agenthub.service` 不会连带结束 CLI；没有用户 systemd 时退回
  `start_new_session` 的普通独立进程（`AGENTHUB_HOST_SCOPE=0` 可强制）。Windows 用
  `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB`。
- 与 `agenthub-tmux-host` 一样，CLI 经 `~/.local/bin/with-zshrc` 之类的包装启动以获得交互 shell
  的环境；`AGENTHUB_HOST_ENV_WRAPPER` 可以改路径，设为空字符串则不包装。

## 构建与定位

```bash
cd host-rs && cargo build --release      # 产物: host-rs/target/release/agenthub-host
```

`term_host.py` 按以下顺序定位二进制，找不到就报告控制台不可用并给出构建命令：

1. `AGENTHUB_HOST_BIN`（绝对路径）
2. `host-rs/target/release/agenthub-host`，然后 `host-rs/target/debug/agenthub-host`
3. 仓库内 `bin/agenthub-host`
4. `PATH` 上的 `agenthub-host`

## 后端选择与共存

`agenthub/term.py` 是调度层：`term_tmux.py` 是原有的 tmux 后端，`term_host.py` 驱动会话宿主。

- 主后端在网页「设置 → 终端后端」里按机器选择，保存在各机器服务端的
  `~/.local/share/agenthub/terminal-backend`，重启后仍然生效。没选过时用
  `python3 -m agenthub.server --terminal-backend {auto,tmux,host}` 或环境变量
  `AGENTHUB_TERM_BACKEND` 给的初始默认值；`auto` 在 Windows 取 `host`，其他平台取 `tmux`。
  要让启动参数重新说了算，删掉那个文件即可。
- 切换只影响新建会话。不可用的后端不能被选中，`/api/term/list` 的 `backends` 会带上原因
  （例如没构建宿主二进制、没装 tmux），网页把它显示在该机器那一行下面。
- 按名称操作（发送、截屏、attach、结束、改名）会在两个后端里查找会话，因此把节点切到 `host`
  之后，仍在 tmux 里跑的旧会话继续可用，直到自然结束。`/api/term/list` 里宿主会话的
  `server` 字段为 `host`。
- 宿主没有 copy-mode，`scroll`/`leave_copy_mode` 是空操作；`submit_text` 只在应用请求了
  bracketed paste 时才包起止序列，和 tmux `paste-buffer -p` 的行为一致。

## 命令行

```bash
agenthub-host list                       # 列出宿主会话
agenthub-host attach agenthub-claude-1234 # 手工接管, Ctrl-\ 退出且不影响会话
agenthub-host capture NAME --lines 200 --plain
agenthub-host send NAME "文本" --enter
agenthub-host kill NAME [--force]
```

全局 `--dir` 可覆盖会话目录，与 `AGENTHUB_HOST_DIR` 等价。

## 验证

```bash
cd host-rs && cargo test                 # 屏幕模型、协议、积压策略
python3 -m unittest discover -s tests -p 'test_host.py'
```

Rust 单测覆盖屏幕模型（滚动历史分页、软换行合并、宽字符、备用屏、各项模式、回放顺序）、
协议（帧切分、tmux 键名、JSON 行与紧随其后的帧）和积压策略。Python 测试用 Web 服务真实的
客户端连真实的 Rust 宿主，覆盖会话生命周期、发送、截屏、attach 回放与实时输入、resize、
改名、退出清理、宿主在启动者退出后存活、`term` 调度，以及通过真实 HTTP 服务和 WebSocket 的
控制台往返。测试只用 `sh`，不启动付费 CLI；没有构建二进制时相关用例自动跳过。

## Windows / ConPTY

2026-09-11 在一台真实 Windows 机器（conhost，Windows SDK 10.0.26100）上验证过宿主进程本身：
会话列表、send、capture（含历史与 ANSI）、中文、kill 与清理全部正常，Claude Code 的启动界面
也能正确渲染；常驻内存约 7 MB。下面两条是那次验证暴露的、必须由宿主处理的 ConPTY 特性。

**必须应答设备状态查询。** ConPTY 以 `PSUEDOCONSOLE_INHERIT_CURSOR` 创建伪控制台
（`portable-pty` 硬编码），conhost 启动后先发 `ESC[6n` 问光标位置，拿到 `ESC[row;colR`
之前既不产出输出也不消费输入。宿主不应答就是双向死锁：会话看起来活着，`send` 和 `capture`
全部为空，也没有任何报错。Unix pty 从不主动问，所以这个依赖在 Linux 上看不见。

`host-rs/src/dsr.rs` 在读线程里扫出 DSR（`ESC[5n` / `ESC[6n` / `ESC[?6n`），其余字节一概不碰。
查询交给屏幕线程按序应答，因此报的是"流里那个位置"的光标而不是滞后的模型状态；查询本身
不转发给浏览器，xterm.js 看不到就不会再答一遍，应答权完全在宿主这边，有没有客户端连着
行为都一样。积压超限丢数据时绝不丢查询——丢了就等于让应用永远等不到应答。

**伪控制台实现要固定。** `portable-pty` 用 `LoadLibrary("conpty.dll")` 找 sideload 版实现，
默认搜索顺序包含 PATH，于是机器上任何自带 `conpty.dll` 的终端（实测 WezTerm）都会被优先
加载，宿主起的就不是系统 conhost，而且 kill 之后会留下孤儿 `OpenConsole.exe`。宿主启动时
调用 `SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)` 把 PATH 和当前目录移出
搜索顺序。要固定某个版本，把 `conpty.dll` 放到 exe 旁边——那仍然在搜索范围内，但是显式的
部署决定。

**下游解析注意**：ConPTY 把未样式的空白格输出成 `ESC[<n>C` 而不是空格，styled 文本里因此
会出现光标前移序列；`claude_bridge` / `codex_bridge` 都先按 CSI 整段剥离，不受影响，`--plain`
取到的也是正常空格。

把 Windows 机器接成完整节点还差：`live.py` 的运行状态检测改用 psutil、`adapters.py` 识别
Windows 项目目录 slug 与盘符路径、文件管理器的根目录判断，以及用计划任务代替 systemd。
另外 Rust 命令行自己的 `list` 在 Windows 上不清理崩溃残留的信息文件（Linux 读 `/proc` 判断）；
Web 服务走 Python 客户端用 psutil，不受此影响。
