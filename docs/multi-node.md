# 多机器 AgentHub

同一个 checkout 提供两个入口：`python3 -m agenthub.server` 运行独立本机服务，
`python3 -m agenthub.hub` 在 hub-host 运行中央服务。两者使用相同的静态前端。
本地模式默认只有自己，不需要注册或连接中央；中央停止不会结束本机 CLI、tmux 或已接收的发送队列。

已运行环境的后续更新遵循 [生产更新说明](deployment.md)，下面的步骤主要用于初次安装。

## 部署

第一版通过已有 WireGuard 私网访问机器，不提供 NAT 穿透或出站隧道。
Hub 默认只监听 `127.0.0.1:8720`，放在 hub-host **已有登录鉴权**的 Nginx 后面。
这一版的所有获准登录用户共用一个受信工作区，具有全部已注册节点的操作权限；
不提供按账号隔离节点或多租户权限。不要将 Hub 端口直接暴露到公网。

1. 在各机器更新到支持节点协议的代码，生成各自独立的节点凭据文件：

   ```bash
   python3 - <<'PY'
   import os, secrets
   from pathlib import Path
   p = Path.home() / '.local/share/agenthub/node-token'
   p.parent.mkdir(parents=True, exist_ok=True)
   fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as f:
       f.write(secrets.token_urlsafe(48) + '\n')
   PY
   ```

   命令不输出凭据，文件已存在时拒绝覆盖。通过可信方式将凭据文件用于第 3 步的服务器端注册。
   在节点的既有 systemd `ExecStart` 中增加
   `--node-token-file /实际家目录/.local/share/agenthub/node-token`，并保留本地管理 IP、
   WireGuard 对端地址和需要的 `--terminal` 参数。例如：

   ```bash
   python3 -m agenthub.server --port 8710 --allow 192.0.2.134,10.0.0.1 \
     --terminal --node-token-file ~/.local/share/agenthub/node-token
   ```

   本机 IP 白名单仍然生效，节点凭据不能绕过它。本地直接访问不需要节点凭据。
   `node-id` 首次启动写入本机数据目录，之后保持稳定；不要把它复制到其他独立节点。

2. 在 hub-host 启动中央服务：

   ```bash
   python3 -m agenthub.hub --host 127.0.0.1 --port 8720
   ```

   默认允许注册的地址范围为 `127.0.0.0/8,::1/128,10.0.0.0/24`。
   其他私网通过 `--node-networks` 显式配置。注册地址只接受 HTTP(S) 的 IP 和端口，
   不接受域名、内嵌凭据、路径或重定向，避免访问任意内部服务和 DNS 重绑定。

   用户服务示例见 [`agenthub-hub.service`](../deploy/agenthub-hub.service)，
   反代示例见 [`nginx-agenthub-hub.conf`](../deploy/nginx-agenthub-hub.conf)。
   先调整实际部署目录，再安装服务和替换 `/agenthub/` location。
   原来的单机反代入口可以继续保留。Hub 自己提供 HTML/JS/CSS，Nginx 将 API、SSE、
   WebSocket 和附件请求交给 Hub，Hub 再路由到节点。

3. 节点注册、改名和移除只在服务器上操作。网页不提供管理入口，HTTP 注册和删除接口返回 405；
   公开机器列表也不返回节点连接地址。注册信息仍保存为权限 `0600` 的
   `~/.local/share/agenthub/hub-nodes.json`，节点凭据不会发送到浏览器。

   在中央服务器的 checkout 内，可以用本地管理类注册节点。凭据文件通过可信方式传到中央，
   保持 `0600` 权限；下面的命令不输出凭据：

   ```python
   from pathlib import Path
   from agenthub.hub import Registry

   registry = Registry(Path.home() / '.local/share/agenthub/hub-nodes.json',
                       ['127.0.0.0/8', '::1/128', '10.0.0.0/24'])
   registry.register({'name': 'NodeA', 'url': 'http://10.0.0.2:8710',
                      'token': Path('/可信目录/node-a-node-token').read_text().strip()})
   # 同一节点重新注册可更新名称、地址和凭据；移除使用 registry.remove(node_id)。
   ```

   完成后执行 `systemctl --user restart agenthub-hub`，让运行中的 Hub 重新读取配置。
   节点服务与运行中的 CLI 不受影响。

## 操作和数据边界

- 顶部机器、Agent Type 和视图共用第一排分段按钮；机器按钮支持多选，双击仅选一台，与 Agent Type 筛选取交集；不提供“全部”或管理机器按钮。
- 活跃数与总数以两个固定宽度的单选按钮显示，计数随机器和 Agent Type 筛选变化；活跃数为绿色，总数使用主题正文色。选择总数恢复全部会话，搜索命中提示显示在搜索框下方。
- 项目树按 `(node_id, cwd)` 分组，时间轴和详情均标注机器名称。
- 新建弹窗明确选择机器；最近目录、目录补全、可用 CLI 都来自目标节点。
- 列表、搜索、运行状态和回收站聚合；新建、发送、接管、停止、上传、星标和恢复在目标机器执行。
- 原始 JSONL、搜索索引、附件、元数据、回收站、审计和发送账本继续保存在节点。
  中央只持久保存注册信息；列表/终端快照有界缓存于内存，正文和附件按需代理。
- 全文搜索保持每台机器最多 60 个会话、每会话最多 200 次命中的原有限制。
  三台机器最多合并 180 条，按更新时间排序；响应标明截断节点和失败节点，不提供全局精确总数或分页。
- 某台机器失败时返回其他机器的结果，列出未完成的机器。缓存列表标注“离线缓存”，
  运行状态视为未知；搜索不使用旧结果冒充新查询。缓存未预热或 Hub 重启后离线节点无列表数据。
- 机器关机是常态。Hub 自带节点监控线程，每 10 秒探测所有节点的 `/api/sessions`，
  维护在线/离线、离线起始、上次检测时间，并把每台机器最近一次会话列表持久化到
  `~/.local/share/agenthub/hub-cache/`。页面的列表、运行状态、终端、回收站和搜索请求只读监控状态，
  不会等待已知离线的机器；离线机器的会话以“离线缓存”显示，Hub 重启后依然可见。
  点开离线机器的会话或向它发送时，Hub 先用 2 秒快速复检，机器已恢复则直接放行，否则立刻返回
  离线原因并触发一次监控重试。机器栏点击离线机器可查看离线时长与上次检测时间。
- SSE 只连接当前查看的节点；终端 WebSocket 保留原始字节。跨机器同名 tmux 互不冲突，
  同一节点的中央/本地浏览器继续共用节点端的控制权租约。
- Hub 不存放离线发送队列，不自动重放写操作。发送沿用节点账本与 request ID；
  新建额外将启动意图和结果写到节点 `create-requests/`。响应丢失后同 ID 返回原结果；
  进程在启动途中崩溃时标为待核对，拒绝自动再启动。

## 协议与兼容性

本地 API 的 UID、tmux 名和原生 sid 不变。Hub 访问层将本地 UID 表示为
`<source>:<node_id>~<本地UID尾部>`，终端为 `<node_id>~<本地tmux名>`。
这只是跨机器引用；发送到本机前还原，保留前端按 source 选择 CLI 的行为。
浏览器消息、草稿、未读和终端缓存以此隔离；中央 localStorage 另按入口路径分区。

中央深链为 `?node=<node_id>&sid=<source>:<原生sid>`。
没有 node 且匹配多个节点时不擅自选择；本地旧深链继续有效。

主要接口：

```text
GET /api/meta                         模式、前端 build、节点协议版本
GET /api/nodes                        机器列表（不含凭据）
GET /api/sessions[?nodes=...]          汇总列表与签名
GET /api/search?nodes=...&q=...        汇总全文搜索
GET /api/live                         汇总实时状态
GET /api/term/list                    汇总终端和逐节点能力
/api/nodes/<node_id>/api/...           指定节点，携带本地引用的直接代理
```

兼容已有前端的普通 `/api/messages/<全局UID>`、`/api/watch?uid=...`、
`/api/session/send` 等路径，也可以通过全局引用路由。新建使用 `_node`，目录补全使用
`node`；未指定机器或一个请求混入多台机器会在执行前拒绝。批量删除按节点拆分，逐条报告结果。
回收站“清空”固定使用当前已展示的节点范围，不跟随弹窗外临时变化的筛选。

节点之间通过 `X-AgentHub-Node-Token` 认证及 `X-AgentHub-Protocol: 1` 校验协议。
Hub 校验自己的前端 build；通过认证且协议兼容的节点请求不要求各机器拥有相同的前端 hash。
不兼容版本拒绝请求。增加破坏性 API 变更时必须升级协议版本。

## 免费验证

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/hub_e2e.py
```

浏览器测试需要现有 Playwright/Chromium，使用隔离的三台模拟 HTTP 节点，覆盖渲染、筛选、
搜索、SSE、媒体、新建路由、双向 WebSocket、二进制上传、离线、手机和独立本地页面。
不启动 tmux、Claude、Codex，不调用真实模型，不连接正在运行的 AgentHub 服务。
