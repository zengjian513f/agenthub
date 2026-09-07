# 多机器 AgentHub

同一个 checkout 提供两个入口：`python3 -m agenthub.server` 运行独立本机服务，
`python3 -m agenthub.hub` 在 hub-host 运行中央服务。两者使用相同的静态前端。
本地模式默认只有自己，不需要注册或连接中央；中央停止不会结束本机 CLI、tmux 或已接收的发送队列。

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

   命令不输出凭据，文件已存在时拒绝覆盖。通过可信方式将文件内容填入中央注册表单。
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

3. 登录中央页面，点“管理机器”，填写机器名称、私网 URL 和节点凭据。
   Hub 会验证身份和协议，成功后保存为权限 `0600` 的
   `~/.local/share/agenthub/hub-nodes.json`。节点凭据不返回浏览器、不存入 localStorage。
   同一个节点重新注册可以改名、更新 IP 或轮换凭据；移除注册不触碰本机会话。

## 操作和数据边界

- 顶部机器按钮支持多选，双击仅选一台，“全部”恢复所有机器；与 Agent Type 筛选取交集。
- 项目树按 `(node_id, cwd)` 分组，时间轴和详情均标注机器名称。
- 新建弹窗明确选择机器；最近目录、目录补全、可用 CLI 都来自目标节点。
- 列表、搜索、运行状态和回收站聚合；新建、发送、接管、停止、上传、星标和恢复在目标机器执行。
- 原始 JSONL、搜索索引、附件、元数据、回收站、审计和发送账本继续保存在节点。
  中央只持久保存注册信息；列表/终端快照有界缓存于内存，正文和附件按需代理。
- 全文搜索保持每台机器最多 60 个会话、每会话最多 200 次命中的原有限制。
  三台机器最多合并 180 条，按更新时间排序；响应标明截断节点和失败节点，不提供全局精确总数或分页。
- 某台机器失败时返回其他机器的结果，列出未完成的机器。缓存列表标注“离线缓存”，
  运行状态视为未知；搜索不使用旧结果冒充新查询。缓存未预热或 Hub 重启后离线节点无列表数据。
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
POST /api/nodes                       注册或更新（name/url/token）
DELETE /api/nodes/<node_id>            移除注册
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
