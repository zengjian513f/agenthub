# 生产更新与验证

中央 Hub 独立提供 HTML、JS、CSS 和静态资源，并通过 API 访问各节点。
仅更新节点、重启本机或推送 GitHub，都不会更新中央站。
日常开发的完成条件见仓库顶层 [AGENTS.md](../AGENTS.md)。

## 目标配置示例

下表全部为示例，不对应真实生产环境。实际主机、账号、目录、网段及服务覆盖项保存在
不纳入 Git 的 `DEPLOYMENT.local.md` 中；部署前必须读取并核对，不能使用下表代替。
该本地文件应限制访问权限，随机器配置保留，不能上传到 GitHub 或复制到公开文档。

| 目标 | SSH / 代码目录 | 用户服务与监听地址 | 公网入口 |
| --- | --- | --- | --- |
| 中央 Hub | `hub@203.0.113.10`，`/home/hub/Projects/agenthub` | `agenthub-hub.service`，`127.0.0.1:8720` | `https://hub.example.com/agenthub/` |
| NodeA | `user@10.0.0.2`，`/home/user/Projects/agenthub` | `agenthub.service`，节点端口 `8710` | `https://node-a.example.com/agenthub/` |
| NodeB | `user@10.0.0.7`，`/home/user/Projects/agenthub` | `agenthub.service`，节点端口 `8710` | `https://hub.example.com/agenthub-node-b/` |

节点可以使用 Git checkout；没有 `.git` 的中央部署可通过 `git archive` 发布已提交文件，
由代码目录中的 `.deployment-commit` 记录已发布版本。具体方式和当前工作分支以本地配置为准，
不要把某个临时功能分支永远写死为发布分支，也不要擅自切换远端正在使用的分支。

## 判断更新范围

| 改动 | 发布与重启范围 |
| --- | --- |
| 共享前端、样式、图标、字体、其他静态资源 | 中央、NodeA、NodeB；更新受影响的 Web 服务 |
| 公共服务模块、API、协议、路由、终端或会话行为 | 检查 Hub 与节点的导入和调用关系，同步相关目标；共享路径通常涉及三处 |
| 确定只被中央或节点使用的实现 | 发布到实际使用该实现的目标，验证中央到节点的相关调用 |
| Nginx / systemd 配置 | 对照线上实际配置做定点修改；配置验证通过后才 reload / restart |
| 仅文档或测试 | 同步提交与文件，不重启 Web；若部署差距中还有运行时代码，按其实际范围发布 |
| ptyhost 二进制（`host-rs/`）| 在一台机器构建静态 musl 产物，拷到各目标的 `bin/ptyhost`；不进 git，节点无需工具链。见 [ptyhost](session-host.md) |

## 发布步骤

1. 读取本地 `git status`、分支、提交，以及各节点的分支、提交和工作区状态。
   中央读取 `.deployment-commit`。检查每个目标从已部署版本到拟发布版本的**全部差异**，
   包括新增资源和删除文件；不能只复制最后一次提交或记忆中的几个文件。
2. 完成与改动相关的验证。UI 问题按 AGENTS.md 从诊断证据定位，能便宜复现再用浏览器复现。
   多机流程可用 `python3 tests/hub_e2e.py`，它使用隔离模拟节点，不启动付费 CLI。
   按实际改动选择其他检查，不因发布而重复运行无关的测试。
3. 只提交本任务已完成的改动并推送 GitHub，保留其他会话的未提交工作。
   节点先 fetch，再在确认分支和工作区状态后使用 `git merge --ff-only` 更新。
   如果不能快进，先核对并处理差异，禁止用 reset/clean/强推丢弃他人的工作。
4. 中央从指定提交生成 `git archive`，将完整部署差异中的已提交文件解包到现有代码目录。
   新文件必须包含在内；删除文件要核对所有权后逐项处理，单纯解包不会删除旧文件。
   不覆盖机器本地配置、数据目录或凭据，不从带有未提交改动的工作区随意打包。
5. 需要重启时，节点仅执行 `systemctl --user restart agenthub.service`，
   中央仅执行 `systemctl --user restart agenthub-hub.service`。
   静态资源更新也要检查运行进程的 build 是否更新，避免网页与 API 版本不一致。
   不重启 `agenthub-tmux.service`，不执行 `tmux kill-server`，不打断现有 CLI。
6. 完成下方验证后记录实际发布提交；中央更新 `.deployment-commit`。
   文档发布可以推进提交记录，但必须确认该版本之前的所有运行时差异也已发布。
   若只完成部分目标，保留准确记录，并向用户说明未完成目标与原因。

## 验证实际运行结果

- 检查相关用户服务为 `active`、`/api/meta` 可用且运行 build 对应已发布文件。
  同一套完整静态文件应有相同 build；存在已核对的本地修改时说明差异。
- 中央检查 `/api/nodes` 中 NodeA、NodeB 的状态，以及本次改动涉及的筛选、搜索、会话等行为。
  通过浏览器检查实际渲染与 JavaScript 错误，保留本机独立 Web 的可用性。
  验证新建/发送按钮时不要向用户的真实会话发消息；写操作可用隔离测试覆盖，
  需要真实 CLI 时按 AGENTS.md 使用最便宜的模型。
- 公网入口仍应有原有登录保护。无登录会话时，可用 SSH 本地转发检查真实中央服务，
  另行检查公网登录跳转；不要关闭鉴权，也不要把隧道验证说成已登录公网验证。
- 重启前后核对终端后端进程与已有 tmux 会话，确保现有会话保留。
- 最后报告已发布提交、完成的目标和相关验证。节点注册与凭据管理仍只在服务器端进行，
  详见 [多机器架构](multi-node.md)。
