# ptyhost（tmux 的替代终端后端）

网页控制台之前完全依赖 tmux：会话独立于 Web 服务存活、字节流 attach、`send-keys`
输入，以及 `capture-pane` 加光标位置的屏幕读取。为了让 Windows 机器也能作为节点托管
原生 Claude / Codex 会话，`host-rs/` 提供了 **ptyhost**，覆盖同样四件事，不依赖 tmux。

ptyhost 本体是 Rust 二进制；Python 侧只保留客户端（`agenthub/host/`），由 Web 服务用来
扫描会话目录、发控制请求和转发 attach 字节流。

## 结构

- **每个会话一个独立进程**（`ptyhost run …`），没有中央守护进程。
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
- **模型崩坏不拖垮宿主**：vt100 内部的 panic（已知一例：把行截短时留下半个宽字符，
  之后擦到那一格就越界）在 `screen.rs` 里被拦下，模型按当前画面重建并在 `capture` /
  `cursor` 应答里计入 `resets`；`session.rs` 里的锁在持锁线程 panic 后照常使用（不在 `PoisonError` 上 `unwrap`），绝不因为某个线程 panic
  就让 attach、capture 和 pty 读线程一起失效。截短前还会先把跨越新边界的宽字符擦成空格，
  避免触发那个已知的越界。宿主 stderr（`<name>.log`）里仍会留下 panic 信息供诊断。
- Linux 上宿主进程会尽量通过 `systemd-run --user --scope` 放进独立的 transient scope，
  这样 `systemctl --user restart agenthub.service` 不会连带结束 CLI；没有用户 systemd 时退回
  `start_new_session` 的普通独立进程（`AGENTHUB_HOST_SCOPE=0` 可强制）。Windows 用
  `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB`。
- 与 `agenthub-tmux-host` 一样，CLI 经 `~/.local/bin/with-zshrc` 之类的包装启动以获得交互 shell
  的环境；`AGENTHUB_HOST_ENV_WRAPPER` 可以改路径，设为空字符串则不包装。

## 构建与分发

二进制**一次构建、多机复用**，节点上不需要 Rust 工具链。Linux 用静态链接的 musl
目标，这样不依赖各机器的 glibc 版本：

```bash
rustup target add x86_64-unknown-linux-musl        # 只在构建机上做一次
cd host-rs && cargo build --release --target x86_64-unknown-linux-musl
cp host-rs/target/x86_64-unknown-linux-musl/release/ptyhost bin/ptyhost
```

产物 1.1 MB、`static-pie linked`，拷到每台机器的 `<代码目录>/bin/ptyhost` 即可
（`chmod +x`）。`bin/` 在 `.gitignore` 里，二进制不进 git。Windows 要在该机器上自行
`cargo build --release`（ConPTY 走 MSVC，不做交叉编译）。

`term_host.py` 按以下顺序定位二进制，找不到就报告控制台不可用并给出构建命令：

1. `AGENTHUB_HOST_BIN`（绝对路径）
2. `host-rs/target/release/ptyhost`，然后 `host-rs/target/debug/ptyhost`
3. 仓库内 `bin/ptyhost` ← 部署分发用这个
4. `PATH` 上的 `ptyhost`

二进制是否存在不影响服务启动：缺它时自动退到 tmux，设置面板里「默认宿主」（即 ptyhost，
下拉里排第一）那一项显示不可用并给出构建命令。

## 后端选择与共存

`agenthub/term.py` 是调度层：`term_tmux.py` 是原有的 tmux 后端，`term_host.py` 驱动 ptyhost。

- **默认后端是 ptyhost。** 主后端在网页「设置 → 终端后端」里按机器选择，保存在各机器服务端的
  `~/.local/share/agenthub/terminal-backend`，重启后仍然生效。没选过时用
  `python3 -m agenthub.server --terminal-backend {auto,tmux,ptyhost}` 或环境变量
  `AGENTHUB_TERM_BACKEND` 给的初始默认值，`auto` 即 ptyhost。要让启动参数重新说了算，
  删掉那个文件即可。
- 配置的后端在这台机器上不可用时（典型：还没把 ptyhost 二进制拷到 `bin/`），实际生效的
  退到另一个可用的后端，控制台不会整个消失；设置面板里标为当前的是实际生效的那个，
  不可用的一项附带原因。二进制到位后自动回到配置值。
- 切换只影响新建会话。不可用的后端不能被选中，`/api/term/list` 的 `backends` 会带上原因
  （例如没构建宿主二进制、没装 tmux），网页把它显示在该机器那一行下面。
- 按名称操作（发送、截屏、attach、结束、改名）会在两个后端里查找会话，因此把节点切到 `host`
  之后，仍在 tmux 里跑的旧会话继续可用，直到自然结束。`/api/term/list` 里 ptyhost 会话的
  `server` 字段为 `ptyhost`。
- ptyhost 没有 copy-mode，`scroll`/`leave_copy_mode` 是空操作；`submit_text` 只在应用请求了
  bracketed paste 时才包起止序列，和 tmux `paste-buffer -p` 的行为一致。

## 命令行

```bash
ptyhost list                       # 列出 ptyhost 会话
ptyhost attach agenthub-claude-1234 # 手工接管, Ctrl-\ 退出且不影响会话
ptyhost capture NAME --lines 200 --plain
ptyhost send NAME "文本" --enter
ptyhost kill NAME [--force]
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

### 节点服务在 Windows 上的现状

能起来了，`/api/meta`、`/api/sessions`、`/api/live`、`/api/term/list`、`/api/trash` 都正常应答，
会话能建、能列、能开控制台。两处为此做了改动：

- `term_tmux.py` 原来在模块级 `import fcntl/pty/termios`。`server → term → term_tmux`
  是无条件导入链，所以 Windows 上整个节点服务根本起不来。现在缺这些模块时只让
  `available()` 报 False，tmux 后端自动退出候选，主后端取宿主。
- `live.py` 的运行状态判断依赖 `/proc`。没有 `/proc` 的系统上退化为"查不出运行状态"：
  会话照常列出、搜索和开控制台，只是不显示活跃标记。

**由此带来的两个已知限制**（接管前务必知道）：

- 接管会把一条其实在跑的会话当成没在跑，直接另起一个实例。Windows 上如果手工在别处
  开了同一条会话，再从网页接管就会出现两个实例。把 `live.py` 换成 psutil 能根除，尚未做。
- Rust 命令行自己的 `list` 不清理崩溃残留的信息文件（Linux 靠读 `/proc` 判断宿主是否还在）。
  Web 服务走 Python 客户端，用 psutil 判断，不受此影响。

Windows 上节点服务由用户自己启动，没有做自启。注意不要用计划任务：它把服务放进不允许
breakaway 的 Job，ptyhost 接管会话时 `CREATE_BREAKAWAY_FROM_JOB` 会被拒（WinError 5）。

**winget 装的 CLI 是符号链接，提权进程穿越不了。** `winget install` 的可移植包（Codex、Grok CLI）
本体在 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\…`，PATH 上只有
`%LOCALAPPDATA%\Microsoft\WinGet\Links\codex.exe` 这样的符号链接。管理员从 OpenSSH 里起的
服务拿的是提权令牌（High integrity），Windows 不让它穿越用户目录里的重解析点：`os.stat`、
`os.path.exists`、直接按链接 `CreateProcess` 全部报 `ERROR_UNTRUSTED_MOUNT_POINT`（448），
`shutil.which` 因此找不到，而交互桌面和 `where codex` 都正常。`term._which_cli` 在 `shutil.which`
与 `~/.local/bin` 都落空后，按 PATH 与 PATHEXT 找到链接并自己 `os.readlink`，用目标 exe 检测和
启动（`live.py` 已按 `codex-*` 前缀认主进程）。

还差：`adapters.py` 识别 Windows 项目目录 slug 与盘符路径（Claude Code 会把 cwd 写进 JSONL，
所以多数会话的分组是对的，回退解码才会出错）、文件管理器的根目录判断。
