# AgentHub

Claude Code / Codex / Grok 三家 CLI 会话的统一网页浏览与管理服务。后端只用 Python 标准库；前端为原生 JS，KaTeX 与 xterm.js 静态内置，无在线依赖和构建步骤。

## 启动

```bash
./run.sh                       # 0.0.0.0:8710, 放行 192.0.2.134
PORT=9000 ./run.sh             # 换端口
ALLOW=192.0.2.134,192.0.2.147 ./run.sh   # 放行多个 IP
```

访问：`http://192.0.2.177:8710`（本机 `http://127.0.0.1:8710`）。

非白名单 IP 一律 403。白名单默认包含本机回环 + `--allow` 指定的地址。

## 数据来源

| 来源 | 路径 | 说明 |
|---|---|---|
| Claude | `~/.claude/projects/<编码cwd>/<uuid>.jsonl` | 标题取会话内的 `ai-title`；`<uuid>/subagents/*.jsonl` 为子代理会话，不单列，在详情标题下拉中单独切换 |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 元数据取首行 `session_meta`；标题优先用 `~/.codex/session_index.jsonl` 的 `thread_name`；`thread_source=subagent` 的协作 agent 不单列，也不会被误作回滚分支隐藏父会话 |
| Grok | `~/.grok/sessions/<urlencoded-cwd>/<uuid>/` | 元数据取 `summary.json`，正文取 `chat_history.jsonl` |

只读原始文件，不改动任何 CLI 的数据。

## 界面

- **左栏**：两种视图 —— 📁 项目树（按 cwd 分组）/ 🕒 时间轴（按日期倒排，每条单独一行显示所在目录）。分组可折叠，折叠状态存 localStorage。家目录缩写成 `~`，过长的路径中间省略（`/a/b/…/y/z`）——不能用 `direction: rtl` 截左边，bidi 会把开头的 `/` 挪到末尾。
- **会话星标**：列表项与详情标题共用一个星标开关；星标会话在当前项目/日期分组内靠前。状态写入权限为 `0600` 的 `~/.local/share/agenthub/session-meta.json`，不修改 Claude/Codex/Grok 原始记录，换浏览器或重启服务后仍保留，并会同步到其他打开的页面。
- **来源筛选**：顶栏三个 chip，各带图标与数量，点击开关。
- **搜索**：输入即按标题/路径过滤；按 `Enter` 对全部会话中解析后的用户、助手和思考正文做全文搜索，结果带命中次数和上下文片段。工具协议、系统注入、compact/记忆上下文和原始 JSON 包装不参与匹配，避免出现会话正文中看不到的大量假命中。快捷键 `/` 聚焦搜索框。
- **搜索选项**（搜索框内三个开关，状态记在 localStorage）：`Aa` 大小写敏感、`ab|` 全词匹配、`.*` 正则表达式。前端过滤、后端搜索、正文高亮共用同一套匹配规则。

  两个实现上的坑：
  - **全词匹配用环视 `(?<!\w)…(?!\w)` 而不是 `\b`**。`\b` 在字节模式下要求边界一侧是 ASCII 单词字符，中文字节全部落在 `\w` 之外，加 `\b` 会导致中文词永远匹配不到。
  - 搜索在适配器解析后的 Unicode 正文上运行，字面、全词和正则模式语义一致；每个会话的可搜索正文按文件版本缓存在内存中，首次解析后后续搜索可直接复用。

  正则语法错误在前端就拦下（不发请求），后端另有一道 400 兜底。
- **搜索词高亮**：左栏标题、上下文片段以及右栏的用户、助手和思考正文会标黄。打开搜索结果后自动滚到第一处匹配（当前项标橙），头部显示 `3/17 处匹配` 和 `↑ ↓` 跳转按钮。命中落在被截断的长消息里时会自动展开。工具、系统和注入上下文不计入搜索或高亮。
- **海量命中的限流**：搜 `a` 这类词会命中几乎所有内容，各层都有上限，且都会告知而不是静默截断：
  - 后端凑满 **60 个会话**就停，返回 `truncated` 标记，顶栏显示「命中超过 60 个会话（已截断，请细化条件）」
  - 单会话命中数最多数到 **200**，列表显示 `命中 200+`
  - 单页最多生成 **3000** 个高亮节点，超出显示 `3000+ 处匹配`
  - 最多自动展开 **40 条**命中消息，其余在标题栏标一个橙点 `●`，需要时手动点开

  没有这些限制时，搜一个单字母会把整个会话全量展开并渲染上万个高亮节点，页面直接卡死。
- **右栏**：消息流采用聊天气泡布局，任何状态都不显示重复的角色/时间 header；用户气泡靠右、其他消息靠左，并用背景色区分角色。气泡不强制预留 10% 空白，长内容可铺满消息栏。用户、助手、思考、系统提示和注入上下文一律不折叠，超长时只有“展开全文”。只有多行工具调用/输出及工具组可以折叠；连续工具组展开后内容直接铺开，不再出现外层 `grp-body` 加内层消息气泡的双重容器。
- **Markdown 渲染**：正文里的表格渲染成真表格（表头灰底、支持 `:---:` 对齐、宽表独立横向滚动不撑破布局），另外支持有序/无序列表、引用块、分隔线、多级标题、行内代码与粗斜体。围栏代码按语言标签使用本地内置的 highlight.js 高亮；无标签时只做常用语言的高置信自动识别，日志和纯文本保持原样。高亮模块按需加载，不阻塞会话列表首屏，也不依赖外网。
- **公式与图片**：KaTeX 渲染 `$...$`、`$$...$$`、`\(...\)`、`\[...\]`，代码块和行内代码不解析公式。支持 Claude/Codex/Grok 的结构化内嵌图片、Markdown 图片，以及正文中确实存在的本地图片路径；图片惰性加载、可点开原图。内嵌和本地图片通过有界内存注册表及不透明 `/api/media/` 地址提供，浏览器看不到本机路径；不存在的相对路径只显示文字占位，不会误发 HTTP 请求。为安全起见仅代理常见光栅格式，不代理 SVG。
- **工具调用分组**：连续 3 条以上的工具调用/输出自动并成一组，组头显示「N 次工具调用」和工具名摘要（如 `Write ×3 · Bash ×2 · Edit ×3`），整组默认折叠。展开后工具内容默认使用 Ubuntu 26.04 同款 Ubuntu Sans Mono（也可切换 Cascadia Mono）、纯黑底柔和灰字单层气泡，中文依次回退到 Noto/Sarasa CJK、微软雅黑或苹方，超长内容只提供「展开全文」。这样一屏能看到完整的对话脉络，而不是被几十条工具输出淹没。
- **状态与询问**：直接解析 Codex `task_started/task_complete/turn_aborted` 和 Claude 回合事件，在消息流底部显示临时 `Working…`、「等待回答」、「已中断」或失败状态，完成后自动移除，不计入消息数或历史正文。`Working…` 还会与当前 CLI 进程的启动时间交叉校验，resume 后停在输入提示符的新进程不会继承旧回合状态。Claude `AskUserQuestion` 和 Codex `request_user_input` 按问题、选项和说明排成专用气泡，回答按用户消息显示；全程读取结构化 JSONL，不做 OCR。
- **手机适配**：720px 以下改为“会话列表 → 会话详情”的单栏导航，不再硬挤左右栏，并记住当前在列表还是详情，刷新后原页恢复。详情使用紧凑返回键，消息数放在标题栏右侧，运行状态改为终端图标左上角的绿/蓝点；标题栏不设三点菜单，只保留直接操作图标，次要元信息默认省略。状态、来源、视图与刷新压在同一行，极窄屏只省略来源数量；输入框使用短提示词，手机 Enter 只换行、点击发送按钮才提交，并兼容安全区域；展开终端时直接覆盖消息区和输入框，提供 Ctrl（下一键生效）、Tab、方向、翻页和 Esc 触控键。
- **管理操作**：Claude 子代理可在会话标题处下拉切换，各自保留独立时间线；运行中的会话显示停止按钮，停止后原位变为删除按钮。
- **问题报告**：顶栏虫形按钮会冻结当前页面、发送账本、tmux scrollback 和最近 15 分钟跨层事件，随后在 agenthub 项目目录自动新建一条 Codex 会话处理。原页面不会被切走，右下角可随时打开处理会话。该操作会使用当前 Codex 配置并产生模型用量。

## 会话深链

`http://<host>:8710/?sid=<source>:<sid>` 直接打开指定会话（也接受不带 `source` 的裸 sid），
优先于 localStorage 里记的上次浏览位置；找不到匹配时退回默认行为。`sid` 是各 CLI 的原生会话号
（Claude 的 uuid、Codex 的 rollout id、Grok 的 uuid），不是列表里的 `uid` —— `uid` 是会话文件
路径的散列，换目录就变，不适合被外部系统长期引用。

T0 研究台账（`research_catalog` 的 `sessions` 表）和 labdesk 的“关联会话”区块就用这个深链回跳。

## 界面状态

桌面端左右栏之间的分割线可拖动（下限 200px，右侧至少留 320px），**双击复位**到默认 340px。

刷新后原样恢复的状态，全部存在 localStorage 的 `agenthub.*` 键下：

| 键 | 内容 |
|---|---|
| `width` | 侧栏宽度 |
| `view` | 项目树 / 时间轴 |
| `off` | 被关掉的来源 chip |
| `closed` | 折叠的分组 |
| `opts` | 大小写 / 全词 / 正则 三个搜索开关 |
| `sel` | 上次打开的会话（重开自动载入；已删除的会跳过） |

搜索词本身不记——重开就停在搜索态会很别扭。

## 删除行为

删除**不做真删**，把原文件/目录移入 `~/.local/share/agenthub/trash/<source>/<时间戳>-<原名>`，
同时在旁边写一份 `…​.agenthub-trash.json` 清单，记下原始路径、标题、cwd 和星标等自有元数据。

顶栏的垃圾桶按钮打开**回收站**：列出每个已删会话的标题、来源、删除时间、占用大小和将要恢复到的
路径，可以逐条恢复、逐条彻底删除，或一次清空。

- 恢复按清单放回原路径。会话 uid 由路径散列而来，因此放回后 uid 不变，星标等元数据一并回来；
  原路径若已被同名会话占用则拒绝覆盖，条目留在回收站并说明原因。
- 清单出现之前删除的旧条目照样能查看和清除；恢复时按 CLI 固有的目录规则推断原路径
  （Claude 用会话内的 cwd 反推项目目录，Codex 用 rollout 文件名里的日期），推断不出来的条目
  明确标为不可恢复，不猜位置。
- 彻底删除会连同该会话遗留在原目录的同名子代理目录一起回收；那个目录只在主会话确实不在时才算孤儿。

## 接管会话（远程控制）

**默认关闭**，服务端要显式加 `--terminal` 才启用 —— 这等于给白名单 IP 开放本机 shell。

```bash
./run.sh --terminal          # 或 python3 -m agenthub.server --terminal
```

启用后，任何会话的详情页都会出现终端图标。点一下，服务端在后台把这个会话用 tmux 起起来（`claude --resume` / `codex resume` / `grok --resume`，自动沿用它原来的工作目录），**消息流底部立刻出现一个输入框** —— 打字回车就发给会话，回复通过增量同步自动出现在上面的历史里。

顶栏的「＋ 新建」可以直接创建 Claude / Codex / Grok 会话。弹窗用一个目录选择面板同时承载最近使用与实时补全：没有输入时显示全部 recent dir；普通关键词先按路径全文、不区分大小写列出匹配的 recent dir，再追加文件系统补全建议；以 `/` 开头时顺序反过来，补全建议优先、recent 匹配随后。相同路径只显示一次，并保留当前优先组中的条目。`Tab` 只用补全建议补齐唯一候选或公共前缀，方向键加 `Enter` 或鼠标可跨两组选择；面板固定在输入框下，不依赖失焦显隐。选择后在该目录启动独立终端，并立即打开网页。若启动目录不存在，网页会显示规范化后的绝对路径并询问是否递归创建；首次提交不会创建该目录，只有确认后才创建并继续。目录补全只在显式启用 `--terminal` 时开放，只返回目录且限制候选数量；创建时后端仍会重新解析并验证目录。后端只接受三种固定 CLI，不接受浏览器传任意 shell 命令。CLI 生成会话文件后，临时终端会自动关联到新会话并切换为正常的对话＋终端视图。

标题栏的终端图标统一负责全部终端操作：没接管时是「接管会话」，接管后在「展开终端」和「收起终端」之间切换。输入框只保留 `Esc` 中断和发送；输入框为空时按 `↑` 可打开当前会话的输入历史，使用 `↑/↓` 浏览、`Enter` 回填、`Esc` 关闭。日常对话用输入框就够，需要方向键选菜单、回答批准提示或看 TUI 全屏界面时，再从标题栏展开终端。

普通网页模式下 `Ctrl+T`、`Ctrl+W` 等由浏览器保留，xterm 收不到组合键。桌面终端聚焦时按一下右 Ctrl 会锁定下一键，随后直接按 `T` 即向 tmux 发送 `Ctrl+T`，发送后自动解除。左 Ctrl 保持普通行为；手机端的 Ctrl 按钮同样只对下一键生效。

输入框左侧的 `＋` 可添加图片、视频、音频、普通文件和文字引用，也可以直接把剪贴板图片/文件粘贴到输入框或拖入输入区。附件先作为可删除的草稿卡显示，发送时才以原始二进制流上传；服务端按会话 uid 查出真实 cwd，以原文件名保存到 `./agenthub_attachments/<id>/`，其中批次 id 在项目目录内从 1 递增，不接受浏览器指定落盘目录。同名且内容相同的文件会复用，同名但内容不同则依次保存为 `文件__1.png`、`文件__2.png`。点击附件卡会在正文插入稳定的 `[附件1]` 引用；删除附件不会重排编号。发送时正文保持原样，末尾另起一段追加 `附件1:./agenthub_attachments/<id>/文件名` 清单。纯文字同样保持原样，因此 `/rename` 等斜杠命令不受影响。单文件上限 512 MB，失败会保留正文和附件供重试。

Codex 忙碌时，后续输入在轮到处理前只存在于 TUI 内存、尚未写入 rollout。agenthub 会立即在时间线底部持久化显示一条“排队中”的用户消息；切换会话或刷新页面不会消失，原生记录出现后会按正文与时间自动消重。

| 情况 | 行为 |
|---|---|
| 会话没在运行 | 直接起 tmux 并连上 |
| 已经接管过 | 复用那个 tmux 会话，不重复起 |
| **正在运行，但不在 tmux 里** | 先弹确认：接管需要**结束正在运行的实例**，确认后 `SIGTERM` → 必要时 `SIGKILL`，再用 tmux 重开 |
| 正在运行且已在 tmux 里 | 直接连上，不动它 |

底部终端可直接拖动上边界调整高度（记在 localStorage）；拖到底吸附为纯对话，拖到顶吸附为纯终端。吸附后标题栏的终端按钮直接在纯对话和纯终端间切换，不会断开；普通高度下收起终端也只会断开显示，tmux 会话继续跑。要真正结束用「结束会话」。切换到别的会话时终端自动收起。

也可以在本机用包装脚本起会话，效果一样能被接管：

```bash
./agenthub-run                 # 默认 claude
./agenthub-run codex
```

本地终端和网页可以**同时连着同一个会话**。专用 server 中的会话可用 `tmux -L agenthub attach -t <name>` 本地接入；普通 `tmux` 命令仍连接用户原来的默认 server。

### 为什么绕 tmux

已经在跑的会话是 `sshd → zsh → claude` 直连 pts，外部进程无法写入它的输入队列；内核的 `TIOCSTI` 注入早已默认关闭（`dev.tty.legacy_tiocsti = 0`）。所以要接入一个已有会话，只能重新用 tmux 把它拉起来 —— 这也是「接管正在运行的会话必须先结束原实例」的原因。

好处是**会话独立于 agenthub 存活**：关掉浏览器、重启 agenthub、甚至 agenthub 崩了，会话照常跑。

新会话运行在 `tmux -L agenthub` 专用 server 中，并加载 [`agenthub/tmux.conf`](agenthub/tmux.conf)：关闭状态栏、前缀键、tmux 鼠标、自动改名和通知，缩短 Esc 延迟，同时开启 focus events、扩展键和真彩色。默认 tmux server 完全不改；升级前已经存在的 `agenthub-*` 会话仍会被发现并路由回原 server，直到自然结束。

systemd 部署使用独立的 [`deploy/agenthub-tmux.service`](deploy/agenthub-tmux.service) 以前台模式持有这个 server。它和网页服务处于不同 cgroup，因此重启或升级 agenthub 不会结束 CLI；systemd 也能监测并重启异常退出的后端，而不必采用 `KillMode=process` 留下失联的网页 attach 子进程。

服务端起一个 PTY 跑 `tmux attach`，WebSocket 双向转发原始字节。专用 server 不让 attach 切换浏览器 xterm 的 alternate screen；连接时只做一次 `capture-pane` 历史回放，之后滚轮完全使用 xterm 本地 scrollback，不再触发 tmux copy-mode 或把滚轮改成方向键。方向键、`Ctrl-C`、批准提示和 CLI 全屏 TUI 仍按真实终端字节传递。WebSocket 是按 RFC 6455 手写的最小实现（`wsock.py`，约 100 行），后端仍然零第三方依赖。

浏览器终端静态内置 xterm.js 6、Unicode 11 和 WebGL renderer，不从 CDN 下载。WebGL2 可用时用单一纹理提交 Codex 的同步重画帧；不可用或 context loss 时自动退回 DOM renderer。Linux 安装了 Sarasa Mono SC / Noto Sans Mono CJK SC 时还会实测中英文格宽，只在汉字宽度严格接近两个西文格时把它作为整套终端字体，避免混合字体造成中文标点错位。

要杀掉哪个进程也不是猜的：裸 `claude` 启动的会话命令行里没有 session id，只有子 shell 的环境变量能认出来，所以要顺着 `/proc` 的进程树往上找到真正的 CLI 主进程。会话 id 只允许 UUID 字符，避免拼命令时被注入。

## 活跃会话检测

正在运行的会话在列表里标一个状态点、标题加粗，顶栏用绿/蓝两种圆点分别显示活动数量，不再附加文字标签。整个计数胶囊也是筛选开关：点击后左栏只显示活动会话，再点恢复，并可与来源、标题和全文搜索叠加。每 3s 探测一次（页面不可见时不探测）；未开启活动筛选时只改小圆点、不重渲染列表，避免打断滚动和选中。

三家留下的痕迹完全不同，所以三种信号都收（全部只读 `/proc` 与状态文件，不触碰任何 CLI 进程）：

| 来源 | 信号 |
|---|---|
| Codex | 常驻持有会话文件的 fd → `/proc/<pid>/fd` 直接给出文件路径 |
| Claude | 进程参数 `--session-id` / `--resume <uuid>`，子进程还有 `CLAUDE_CODE_SESSION_ID` 环境变量 |
| Grok | 自己维护 `~/.grok/active_sessions.json` |

Claude 和 Grok 写完就关文件，所以**不能只靠 fd**；反过来 Codex 的会话 id 不出现在命令行里，所以也不能只认 session id。扫描约 40ms，服务端缓存 3s。

被运行中的会话选中时，增量同步间隔自动从 10s 缩到 3s。

## 消息载入与同步

首次点开会话只传最早 100 条和最新 500 条，默认停在最新一条；中间有明确的缺口按钮，点击一次载入完整历史。无论是否补齐，中间到达的新消息都从文件末尾继续增量追加。载入过程有进度条（下载阶段显示字节数，渲染阶段显示条数）。

**保持在最新**：只要没有主动往上翻，窗口缩放、消息展开、新消息到达都会一直跟着最后一条。判断"想离开底部"直接听 `wheel`/`touchmove`/`PageUp` 这些用户动作，而不是推断 scroll 方向 —— 布局重排和程序自身的滚动都会产生 scroll 事件，混在一起分不清谁是谁。窗口缩放时消息会重新折行、`scrollHeight` 一路涨，单次修正追不上，所以补几帧直到布局稳定，同时要避免补偿循环空转（否则用户的上翻会被当成程序自己滚的而忽略）。

**浏览器端 LRU 缓存**：解析好的消息按会话留在内存，默认上限 256MB（可在设置中调整），超出淘汰最久未用的。再次打开同一会话直接上屏。

**更新靠服务端推送，不是客户端轮询**。打开会话后建立一条 SSE 连接（`GET /api/watch?uid=&start=&head=&anchor=`），服务端盯着这个会话文件，一有变化立刻把 diff 推过来。

推的内容只有两种，正好对应文件的两种变化：

| 文件变化 | 推送内容 |
|---|---|
| **追加**（正常对话） | 只解析新追加的那段，推新增消息，客户端接到末尾 |
| **截断 / 改写**（双 Esc 回滚等） | `reset: true` + 整份内容，客户端丢弃缓存重新渲染 |

判定能不能"接着读"有三个条件，缺一不可：**旧 EOF 对应的文件头前缀哈希没变**、**文件没缩短**、**上次读到的偏移点之前 512 字节没变**（`anchor`）。小于 4KB 的新文件使用旧长度的固定前缀，正常追加不会被误判成改写。

第三条是为回滚准备的，而且必不可少：回滚会把记录截断到更早的点，之后新对话继续往后写。这时文件头没变，长度还可能重新超过旧偏移 —— 只看头和长度会判定"可以接着读"，于是从旧偏移读出一段完全不同的内容，历史就错乱了。实测构造了最刁钻的情况（截断后重写、文件比原来更长、头哈希不变），仍被正确判为 `reset`。

**实测延迟：落盘到上屏中位 53ms**（此前客户端轮询方案平均 530ms），期间客户端零主动请求。

两个让它快下来的细节：

- 服务端检测循环持有已发布的 session 快照并调用 `messages_for(session)`，每次只
  `stat` 当前文件。普通 `get(uid)` 也走内存 UID map，不会在消息、live、终端或
  outbox 热路径里隐式扫描磁盘。
- 客户端保留一条 20s 的兜底对账；SSE 断开时才回到自适应轮询（350ms~3s）。EventSource 自带的重连会沿用旧 URL（旧偏移），所以断开时自己关掉重连、带上新偏移。

实测（最大的会话，23MB 文件 / 4775 条消息）：

| 路径 | 耗时 |
|---|---|
| 首次打开（含下载 390ms + 建 DOM 210ms） | ~750ms |
| 缓存命中再打开 | ~150ms |
| 增量同步（无新内容） | 服务端 3ms |

会话消息、列表和搜索等 JSON 响应超过 1 KiB 时，会根据客户端的
`Accept-Encoding` 使用 gzip level 4，并返回 `Vary: Accept-Encoding`。浏览器自动解压，
前端不需要额外处理。当前真实大会话抽样中，37.1 MiB 源文件解析成 4.5 MiB 首包，
压缩后为 1.20 MiB（减少 73%，服务端约增加 75 ms CPU）；增量响应通常很小，不会压缩。
反向代理配置也对 JSON、JS、CSS 和 SVG 提供相同级别的 gzip 兜底，WebSocket 与 SSE 不压缩。

有个坑值得记：渲染必须**先在游离的 `DocumentFragment` 里建好整棵子树再一次性挂上**。若逐批插入已在文档中的容器，每批都会触发一次全量 layout，节点上万时是 O(n²) —— 同一个会话实测 243ms 变成 14.5s。批间让出主线程也要用 `setTimeout` 而非 `requestAnimationFrame`，后者会等一次绘制，又把 layout 成本引回来。

## 索引缓存与列表自动刷新

索引缓存在 `~/.cache/agenthub/index.json`。缓存同时保存每个主会话的 raw 元数据、
公开列表和上次 inventory，文件用 `(路径, size, mtime_ns, inode)` 判断变化。普通 append
只重读对应会话：Claude 同时更新尾部标题与子代理，Codex 在内存中重算分叉继承，
Grok 重读对应 summary；新增、删除和移动也只增删相关 raw row。顶栏 `↻ 刷新`
仍会强制全量重建。当前 175 个真实会话中，全量约 0.40s，单 owner 元数据重读约
4–6ms，一次含 inventory 与缓存落盘的完整增量协调约 20–25ms；缓存过期后的进程
冷启动也只协调变化 owner，当前实测约 27ms。

`load()` 是唯一执行 inventory 协调的入口；`get()`、`/api/live` 和
`/api/term/list` 都读取一次性发布的稳定快照。并发刷新由同一把锁 singleflight，
解析期间再次 append 会把快照标成 dirty，下一轮继续追赶而不会把新签名配给旧列表。
列表游标按 `(路径, size, mtime_ns, ctime_ns, inode)` 缓存；多客户端同时刷新时，同一
文件版本只解析一次，之后只 `stat` 检查，单文件 append 也只重算该文件的游标。

列表每 8s 自动跟进磁盘变化：前端带上手里的签名请求 `/api/sessions?sig=<签名>`，一致时服务端只回 `{"unchanged": true}` —— inventory 会话及子代理文件为个位数毫秒，响应约 70 字节。新会话出现、rename（Claude 的最新 `custom-title`、Codex 的 `session_index.jsonl`、Grok 的 summary）、时间重排和子代理增删都会自动反映，**不打断当前的选中和滚动位置**，搜索态下也不会把结果冲掉。

**拿到新列表也不等于要重画**。活跃会话每隔几秒就变一次大小和时间，每次都重建左栏 DOM 的话，看起来就是一直在闪。所以先比对结构（顺序、标题、目录，时间轴视图下还要比日期分组）：只有结构真变了才重建，否则只把那一行的文字改掉，DOM 节点原地不动。

三个轮询的分工：

| 轮询 | 间隔 | 成本 |
|---|---|---|
| 会话列表签名 | 8s | 个位数毫秒 / ~70 字节 |
| 活跃会话（扫 `/proc`） | 3s | ~40ms，服务端缓存 3s |
| 当前会话更新 | **服务端推送**，检测间隔 50ms | 只 stat 一个文件，微秒级 |
| 推送断开时的兜底 | 自适应 350ms~3s | 无新内容时服务端 3ms |

活跃标识按运行环境分色：绿色表示普通的非 tmux 进程，蓝色表示运行在 tmux 中、可直接接入的会话；详情只显示纯色点，说明放在悬停提示中。

这几个轮询都不写日志（`/api/live`、`sig=`、`start=` 一律静默），否则真正有用的日志会被淹没。

页面切到后台时全部停摆，切回来立即各跑一次。

## 测试

`tests/e2e.py` 用 Playwright 驱动真实 Chromium 点遍全部交互（列表渲染、来源筛选、两种视图、分组折叠、标题过滤、全文搜索、聊天气泡布局、单层工具输出、自带终端字体与配色、滚动条样式、附件上传/粘贴/引用与 prompt 转换、工具输出折叠、展开全文、公式与图片、子代理独立视图切换、增量同步、刷新、快捷键、接管终端、删除入回收站、回收站的查看/恢复/彻底删除，以及页面无 JS/HTTP 错误）。

删除相关的断言跑在一个临时造出来的自测会话上（`~/.claude/projects/-tmp-agenthub-selftest/`），跑完自动清理，不碰真实会话。

```bash
pip install playwright && python3 -m playwright install chromium
./run.sh &                 # 服务需先运行在 8710
python3 tests/e2e.py
```

`tests/claude_monkey.py` 是需要真实 Claude 账号、会产生费用的显式压力测试，不属于
普通测试套件。它会创建至少 6 个隔离会话，交叉执行消息发送、原生忙时排队、丢失 HTTP
响应后的同请求重放、切换会话、缩放窗口和终端/对话切换。脚本固定使用完整的
`claude-haiku-4-5-20251001` 模型 ID，并从 JSONL 再次核对实际模型；不得用可能被配置重映射
的 `haiku` 别名。只有明确接受真实模型费用时才运行：

```bash
python3 tests/claude_monkey.py --base http://127.0.0.1:8710
```

`tests/dual_cli_monkey.py` 是 Claude/Codex 双端的一小时状态机 monkey，也属于显式
付费测试，不能被普通测试套件调用。它固定使用完整的
`claude-haiku-4-5-20251001` 和 `gpt-5.6-luna` 模型 ID，每端创建 10 个隐藏 debug
会话。调度器按当前 tmux 状态和尚未覆盖的转移选择动作，不再按固定阶段顺序重复脚本；
动作包括网页与 tmux 双向输入、首尾空白、服务端已接收但 HTTP 响应丢失后的同 ID 重试、
忙时排队、快/慢 ESC、终端草稿覆盖、选择题、斜杠命令，以及工作期间切会话、切终端、
横纵 resize、刷新、断网恢复和双页面接管。

正确性由独立模型持续核对 tmux 实际画面、每个浏览器页的缓存/DOM、服务端发送账本；
不使用产品自身的 `busy_screen()`/`composer_state()` 给产品判对。每一步均写入带 seed 的
轨迹；失败证据包包含 tmux 画面与 scrollback、浏览器状态/HTML/截图、outbox 和
`replay.json`。调度和不变量可以完全免费地先检查：

```bash
python3 tests/dual_cli_monkey.py --simulate --steps 900 --seed 4815
```

只有明确接受真实模型费用后才能运行或重放：

```bash
python3 tests/dual_cli_monkey.py --duration 3600 --sessions 10 --max-paid-turns 14
python3 tests/dual_cli_monkey.py --replay /path/to/failure-001/replay.json
```

## 结构

```
agenthub/
  adapters.py   三家存储格式的解析器, 输出统一的会话元数据与消息
  index.py      索引缓存、增量读取、全文搜索与删除
  media.py      内嵌/本地图片的校验、限额注册与安全读取
  pending.py    新会话首次落盘前的持久化元数据
  claude_queue.py Claude 网页输入的服务端交付账本与原生记录确认
  audit.py      SQLite 跨层事件审计、压缩正文去重与报告导出
  bug_report.py 私有诊断包与自动 Codex 处理会话
  send_audit.py 兼容旧版的紧凑消息交付日志
  send_protocol.py Claude/Codex 交付状态的公共驱动接口
  send_queue.py Codex 网页输入的服务端持久队列与原生记录确认
  session_meta.py  星标等 agenthub 自有会话元数据
  server.py     ThreadingHTTPServer 路由与 IP 白名单
  static/       前端 (原生 JS, 无构建步骤；vendor/ 含 KaTeX 与 xterm.js)
```

### HTTP 接口

- `GET /api/sessions[?force=1&sig=]` — 全部会话元数据；签名未变时返回轻量结果
- `GET /api/live[?force=1]` — 活跃会话、tmux 子集及当前 CLI 进程启动时间
- `GET /api/messages/<uid>?window=1&start=&head=&anchor=&agent=<id>` — 首尾窗口、整份或增量消息；`agent` 选择单个 Claude 子代理视图
- `GET /api/watch?uid=&agent=&start=&head=&anchor=` — 当前主会话或所选子代理视图的 SSE 增量推送
- `GET /api/media/<token>` — 会话中已登记图片的不透明只读地址
- `GET /api/search?q=&source=claude,codex` — 正文全文搜索
- `GET /api/session/input-history?uid=` — 只返回当前会话的用户输入历史，供输入框按需回填
- `GET /api/meta` — 当前服务端构建标识与主机名
- `GET /api/term/complete-dir?path=` — 返回启动路径的子目录补全候选（需 `--terminal`）
- `POST /api/term/create` / `GET /api/term/new-status?name=` — 创建并关联新 CLI 会话；目录不存在时先返回 `needs_create`，确认后以 `create_cwd: true` 重试（需 `--terminal`）
- `POST /api/session/attachment?uid=&name=&id=` — 上传附件到会话 cwd 的受控批次子目录；首个文件省略 `id`，后续文件复用响应中的 `attachment_id`
- `POST /api/session/star` — 设置会话星标（JSON：`{"uid":"…","starred":true}`）
- `POST /api/session/send` — 把已有 Claude/Codex 会话的网页输入交给服务端持久状态机
- `POST /api/session/outbox/retry` / `POST /api/session/outbox/discard` — 重试可证明尚未触碰终端的输入，或移除页面里的未确认状态；Claude 一旦开始注入终端便拒绝盲目重试
- `POST /api/session/stop` — 从内层 CLI 开始停止运行实例，保留对话记录
- `POST /api/audit/browser` — 浏览器批量回传 SSE 应用、DOM 和交互回执
- `POST /api/bug-report` — 冻结诊断上下文并启动一条 Codex 处理会话
- `GET /api/trash` — 回收站条目、总量与目录位置
- `POST /api/trash/restore` — 把条目放回原路径（JSON：`{"id":"<source>/<文件名>"}`）
- `POST /api/trash/purge` — 彻底删除单条（`{"id":…}`）或清空回收站（`{"all":true}`）
- `DELETE /api/session/<uid>` — 移入回收站

新建 CLI 在产生第一条正式记录前，会写入权限为 `0600` 的
`~/.local/share/agenthub/pending-sessions.json`。因此刷新或离开网页不会让它从左栏
消失；记录保存类型、启动目录、会话 ID、tmux 名和终端尺寸。临时会话也有空对话页、
输入框和附件入口，手机端可以切换终端/对话，并用关机按钮按 tmux 名直接停止。正式会话
关联后自动退出临时列表，尚未发送的正文、引用和附件草稿会迁移到正式 uid，不会消失。

Codex 忙时不会把尚未轮到的输入写进 rollout，tmux 接受粘贴也不能证明 Codex
已经接收。因此已有 Codex 会话的网页输入先持久化到权限为 `0600` 的
`~/.local/share/agenthub/send-queue.json`；观察到原生回合结束且终端画面稳定后才交付，
服务端会独立续读 rollout，浏览器锁屏或断开也不影响队列推进；最终以其中出现对应的
`user` 记录确认。

Claude 输入同样先写入权限为 `0600` 的
`~/.local/share/agenthub/claude-send-queue.json`，然后才触碰终端。服务端以请求 ID 幂等，
并用原生 `user`、`queue-operation` 和 `/rename` 记录确认结果。只有仍处于 `persisted`
（可证明尚未触碰终端）的项目才允许恢复交付；从 `injecting` 开始，即使 HTTP 响应丢失或
服务重启，也只等待原生证据或标记为待核对，绝不自动重发。页面与写请求还携带构建标识，
旧标签页会被服务端在触碰终端前拒绝并提示整页刷新。

### 跨层审计与问题报告

服务端把浏览器 → HTTP → 发送账本 → tmux/PTY → JSONL 解析 → SSE → 浏览器 DOM
记录为同一条可关联时间线。事件存于 `~/.local/share/agenthub/audit.sqlite3`（SQLite
WAL，文件权限 `0600`），顺序由数据库自增序号确定；较大的请求正文、终端片段、规范化
消息批和 DOM 快照以 SHA-256 寻址、zlib 压缩并去重。默认保留 14 天。诊断写入在后台执行，
队列、磁盘或数据库失败不会改变消息发送结果。

审计会保存诊断所需的对话正文、终端输入输出和页面可见文字，因此该数据库本身属于敏感
本机数据，不应上传或随仓库发布。结构化字段会递归移除 Cookie、Authorization、密码、
API key 与 access/refresh token；附件只记录既有引用和元数据，不另复制正文附件。

点「报告问题」后，私有包写到
`~/.local/share/agenthub/bug-reports/<BUG-id>/`，包含 `manifest.json`、用户描述、浏览器状态、
相关事件、tmux scrollback 和 Git 状态。随后新建 Codex tmux，会话提示词要求先按
`AGENTS.md` 用 Playwright 重现，再找出链路中第一个偏差并修复；验证通过后默认只提交本次
报告产生的修改，创建本地 commit，但不会自动 push。若工作区原有改动与修复重叠、无法
安全隔离，或测试没有通过，则保留未提交状态并在处理会话中说明。
如果 Codex 启动失败，诊断包仍会保留。报告按钮会实际调用当前账号配置的 Codex 模型，
并产生相应模型用量。

## 开机自启（可选）

仓库提供两个用户服务：[`deploy/agenthub.service`](deploy/agenthub.service) 运行网页，
[`deploy/agenthub-tmux.service`](deploy/agenthub-tmux.service) 独立持有终端后端。网页服务通过
`Wants`/`After` 依赖后端，但停止或重启网页不会连带停止后端。若项目路径不同，安装前需同步修改两个文件中的绝对路径。

```bash
cp deploy/agenthub.service deploy/agenthub-tmux.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agenthub-tmux.service agenthub.service
```

### hub-host 反向代理

线上入口为 `https://node-a.example.com/agenthub/`。页面资源、API、SSE 和 WebSocket 都使用当前页面的相对基路径，因此根目录直连与 `/agenthub/` 子路径可同时工作。

- 本机服务由 [`deploy/agenthub.service`](deploy/agenthub.service) 托管，只允许局域网管理端和 WireGuard 对端 `10.0.0.1`。
- UFW 仅放行 `wg0` 上 `10.0.0.1 → 10.0.0.2:8710/tcp`。
- ECS 使用 [`deploy/nginx-agenthub.conf`](deploy/nginx-agenthub.conf) 反代，并复用 `snippets/auth.conf` 的 hub-host 统一鉴权。
- Nginx 关闭代理缓冲并保留 Upgrade 头，以支持会话推送和 tmux WebSocket。
