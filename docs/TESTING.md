# 测试、复验与真实环境验收

## 一条命令复验

```bash
bash scripts/verify.sh
```

脚本运行 Bash 语法检查、Python AST 语法检查和 `unittest`。安装了 ShellCheck 时也检查 Shell 脚本；没有安装会明确显示跳过，不冒充通过。GitHub Actions 会安装 ShellCheck 并计划在 Python 3.10、3.11、3.12、3.13 上运行。

### 安装器权限环境回归

安装器会设置 `umask 077`，并通过 `runuser` 以非 root 账户运行离线测试。
CI 现在对每个 Python 版本分别运行常规测试和 `umask 077` 测试。
本地可在非 root 账户下复验：

```bash
(umask 022; bash scripts/verify.sh)
(umask 077; bash scripts/verify.sh)
```

权限负向测试必须显式构造“不安全目录”。`mkdir(mode=0o755)` 的实际权限仍会受
调用进程的 umask 限制；在 `077` 下它实际成为 `0700`，不能作为“公开目录”
测试用例。`test_public_state_directory_rejected` 因此在临时目录上显式
`chmod(0o755)` 并核对权限，再断言 Store 拒绝该目录。不要为了通过测试关闭
Store 的权限检查、降低安装器 umask 或跳过安装自检。

本地测试不运行安装器主流程，不调用真实 Google 或 Telegram，不创建系统用户，不删除系统目录。会创建临时文件、短生命周期测试进程及仅绑定回环地址的 HTTP 服务。安装器中的同一离线测试以 `agy-tg` 执行。

## 自动化覆盖

| 测试文件 | 主要覆盖 |
|---|---|
| `test_settings.py` | 默认值、旧配置保留、显式自动审批、无 shell 展开、危险路径/链接/FIFO/大小限制、可选默认模型配置、快捷别名解析与格式校验 |
| `test_runner.py` | 严格 JSON、空回复、错误状态、Unicode、输出上限、真实 Linux 进程组、取消/超时/派生进程、--model 命令行参数构建与透传、Token 用量解析、--conversation 会话参数构建与透传、--effort 思考深度与 --mode 执行模式构建与透传 |
| `test_store.py` | 私有权限、限长、过期、用户路由、凭据替换、崩溃记录、更新水位和实例锁、用户模型偏好落盘/重置/维护清理、连续会话 ID 维护与重置、用户累计 Token 用量统计与维护清理、用户 effort/mode 偏好落盘与维护清理、动态白名单落盘与合并 |
| `test_bot.py` | 白名单、群聊拦截、工作目录互斥、准备/运行取消、投递失败、`/last`、重复更新、初次轮询就绪判定、退出撤销就绪标志、`update_id` 防重放、`/model` 全量官方 14 种模型查询、别名解析与切换、执行耗时显示、当前模型高亮、工作空间磁盘汇报与长消息分页指示、Telegram Inline Keyboard 行内按钮下发、callback_query 回调处理与 Toast 提示、连续会话上下文记忆与自动透传、`/new` 与 `/reset` 重置会话、`/usage` 会话与累计 Token 报表、Antigravity 优雅卡片式排版与分割线渲染、`/sys` 实时系统与进程监控、`/effort` 与 `/mode` 交互与透传、`/whitelist` 动态增删管理、`/ls` 文件浏览与防穿越、`/restart` 权限与空闲槽位重载 |
| `test_telegram_api.py` | 本地真实 HTTP 请求、429、网络/协议错误、UTF-16 分段、完整离线回传链路、reply_markup 行内键盘序列化透传、answerCallbackQuery 响应 |
| `test_installer.py` | Bash 语法、OS 版本门槛、系统服务账户创建参数、已有 UID 保持、附加组拒绝与名单展示、两个配置输入、生成 unit、配置保留、临时目录内的真实回退函数、自检分类、部署排他互斥锁、测试脚本误用与隔离防护、Root 模式 service unit 生成与 Root smoke 测试放行 |
| `test_install_menu.py` | 菜单选项（安装/卸载/退出）、EOF与错误输入重试、--install与命令行快捷方式跳过菜单、sudo模式透传、三步向导顺序性（第一步等待/取消不进入第二步） |

### Debian 12 一次性环境账户创建测试与防误用

为验证真实的系统账户创建命令（`adduser --system --group`）在 Debian 12 环境下的表现，项目提供了 `scripts/test_account_debian12.sh`，并在 GitHub Actions CI 中通过 `container: debian:12` 自动化执行：
- **防误用与隔离门槛**：必须同时传入 `--confirm-isolated-environment` 且设置环境变量 `AGY_TEST_ISOLATED_CONTAINER=1`，否则直接拒绝执行，避免在主机或生产 VPS 误跑；
- **随机资源隔离**：测试使用随机账户名 `test-agy-${RAND_SUFFIX}` 与 `/home/${TEST_USER}`，严禁使用生产账户 `agy-tg` 或 `/home/agy-tg`；在创建前若发现同名资源已存在立即中止报错；
- **严格退出清理**：通过 `trap EXIT` 精确追踪并清理本次测试创建的用户、主组、测试附加组与临时家目录，杜绝无差别删除或盲目 `|| true`；
- **行为验证**：验证新建服务账户仅属于自身同名组，不附带 Debian 默认的 `users` 附加组，UID >= 100 且 home 正确；验证重复执行时复用已有账户并保持 UID；验证附加组存在时阻断并提示。

注意：本地离线单元测试通过模拟函数与环境门禁覆盖上述逻辑（共 176 项离线测试全部通过），不在本地服务器上实际创建或修改真实用户。

### Telegram update_id 边界与防重放机制（待验证项）

- **现象与官方定义**：Telegram Bot API 官方文档指出：“若超过 7 天无新更新，下一个更新的 identifier 将随机选取而非连续递增”；同时规定“只要调用 `getUpdates` 时的 `offset` 大于该 `update_id`，该更新即被 Telegram 服务端确认”。
- **风险分析与决策**：在连续长轮询运行期间，若客户端盲目接受小于当前 `offset` 的 `update_id`，将破坏针对网络重复包、反向代理乱序投递及重发的防重放保护。且服务初次启动已通过 `getUpdates(offset=-1)` 重置最新水位。
- **状态记录**：根据审阅要求，在缺乏明确生产复现证据前，将其列为**待验证项（Pending Verification）**。核心代码保留 `uid < self.offset` 丢弃机制，并增加单元回归测试，不盲目修改核心 offset 逻辑。

错误分类依赖诊断关键词，属于保守提示，不保证覆盖 Google 所有错误文案。测试 fixture 的协议来自当前官方文档，不等于验证所有真实 agy 版本。

## 发布前必须做的真实环境验收

在可重建的 Debian 12+ 或 Ubuntu 22.04+ 测试服务器上，使用专用 Bot 和你自己的 Google 账号：

1. 全新安装：三个阶段均正常；Google 授权后 `/exit` 返回；自检回复严格为 `AGY ready.`；成功提示仅在 Bot 就绪后出现。
2. 私聊只读任务：核对实际工作目录、结果与服务器文件；在一个可丢弃的测试子目录验证所需工具权限。
3. 在**你发起的、可安全中断的任务**执行时使用 `/cancel`，确认子进程没有继续修改测试文件。
4. 模拟回传失败后用 `/last` 取回同一任务编号，不应再次执行原任务。
5. 升级已有 `AGY_SKIP_PERMISSIONS=false` 的安装，确认普通更新仍为 false，白名单、路径和限额未改变。
6. 用 `--enable-auto-approve` 做明确迁移，确认只在这次显式操作后启用。
7. 在测试实例中触发候选版本启动失败，确认旧配置、入口和 unit 被恢复；工作目录内容没有被擅自删除。
8. 检查 root 所有代码目录、私有配置/状态目录，检查实例重复、webhook 冲突和服务启动错误提示。
9. 只在一次性测试服务器上验证默认卸载与二次确认彻底清理，勿在生产服务器测试破坏性清理。

不要为了模拟错误而泄露 Token、真实凭据或重要数据。授权、配额、网络、运行用户、Google keyring 和 systemd 沙箱环境都必须在目标服务器确认；离线测试不能替代这些检查。

## 必须如实区分的状态

“代码已生成”“离线测试通过”“systemd unit 静态校验通过”“GitHub CI 通过”“真实账号端到端验收通过”“已合并 main”“已在服务器部署”是不同状态。

没有真正运行的项目必须标为未验证。测试通过不证明不存在其他缺陷，也不证明模型在任何任务里都不会返回空回复。

## 配置与消息可靠性回归

未知配置项（包括权限开关拼写错误）必须拒绝启动，不输出配置值。
启动阶段的网络错误、429 和 5xx 会退避重试，停止信号可打断等待；
明确的请求或认证错误仍退出，不启动任务、不自动重跑任务。

控制回复使用最多 32 条等待消息的队列。队列满时丢弃新增控制回复，
但仍处理取消信号；结果仍先持久化，任务接收确认失败时不启动 agy。
公开的 /id 全局每两秒最多回复一次，避免陌生用户耗尽发送通道。
这意味着高负载时控制提示可能不显示，/last 可在稍后再次查询。
测试通过实际 consume_updates 路径覆盖慢 /last、慢接收确认、
队列满时取消、/id 限流、初始化恢复与停止清理。

## 模型切换能力回归（/model）

`/model` 允许已授权白名单用户查询与切换后续任务的模型。
模型名称必须通过 `[A-Za-z0-9._-]{2,64}` 安全正则校验，杜绝任何参数注入或非法字符。
`/model default` 重置模型偏好为默认（由 agy 决定）。
偏好以 `0600` 私有权限存储于 `model-{user}.json`，在白名单用户被移除时由 `maintain()` 自动清理。
任务启动时将用户模型透传给 `agy_runner` 的 `--model` 参数，并在接收确认及最终执行结果中回显模型标识。

## 思考深度与执行模式回归（/effort 与 /mode）

- **思考深度（`/effort`）**：
  - 仅接受 `low`、`medium`、`high` 或恢复默认 `default`，支持中英文别名（`低`/`中`/`高`/`med`）；
  - 选项偏好独立原子落盘至 `state/effort-{user}.json`（权限 `0600`），未授权或移除用户在 `maintain()` 时物理删除；
  - `agy_runner` 在任务构建时透传 `--effort <level>`，`test_runner` 断言命令行参数正确装配；
  - `test_bot` 覆盖行内按钮与回调查询（`callback_query`）处理，并向 Telegram 发送 Toast 响应。
- **执行模式（`/mode`）**：
  - 支持推演规划 `plan`（只读，严禁写入任何工作区文件）与落地编辑 `accept-edits`（代码生成），支持中文别名（`规划`/`编辑`/`落地`）；
  - 偏好独立落盘至 `state/mode-{user}.json`（权限 `0600`）；
  - `agy_runner` 在任务构建时透传 `--mode <mode>` 参数，`test_runner` 断言参数透明拼装。

## 连续对话与 Token 统计回归（--conversation 与 /usage）

- **连续对话多轮记忆（Context Continuity）**：
  - `state_store` 记录每用户的 `conversation_id` 及轮次 `num_turns`（落盘至 `conversation-{user}.json`）；
  - `agy_runner` 构建命令时注入 `--conversation <id>`，`test_runner` 断言会话 ID 正确透传；
  - `/new` 与 `/reset` 重置会话，测试断言清除上下文后下一轮开启独立会话；
  - 任务接收卡片智能显示 `已关联上下文 (第 N 轮)` 或 `全新独立会话`。
- **Token 消耗统计与 `/usage` 报表**：
  - 严格解析 `agy` JSON 响应中的 `input_tokens`、`output_tokens`、`thinking_tokens` 和 `total_tokens`；
  - 自动累加至 `state/usage-{user}.json`（含轮次与各项 Token），`test_store` 验证维护清理；
  - `/usage` 汇总当前会话 ID 与历史累计消耗，`test_bot` 断言千分位格式化报表输出。

## 系统监控与工作空间沙箱回归（/sys 与 /ls）

- **系统监控（`/sys` 或 `/system`）**：
  - 纯标准库读取 `/proc/uptime`、`/proc/loadavg`、`/proc/meminfo`、`/proc/<pid>/statm` 与 `/etc/os-release`；
  - `test_bot` 验证在虚拟或不同 Linux 发行版环境下的容错解析，断言格式包含平台、运行时间、CPU、物理内存、磁盘与 Bot PID/常驻内存。
- **工作区速览与防穿越沙箱（`/ls` 或 `/files`）**：
  - 智能文件类型图标映射（`format_file_entry`）涵盖 Python `🐍`、Shell `🐚`、Markdown `📝`、配置 `⚙️`、日志 `📋`、数据库 `🗄️`、压缩包 `📦`、图片 `🖼️`、目录 `📁`、文件 `📄`；
  - 支持 `/ls <subpath>` 浏览子目录；
  - `test_bot` 深度验证路径沙箱安全：使用 `os.path.realpath` 严密阻断 `..` 目录遍历越权逃逸，自动过滤工作区外的恶意软链接。

## 动态白名单与安全平滑重启回归（/whitelist 与 /restart）

- **动态白名单（`/whitelist`）**：
  - 仅主管理员（`ALLOWED_USER_IDS` 首个 ID）有权执行 `add` 与 `remove`；
  - 动态白名单安全保存至 `state/whitelist.json`（权限 `0600`）；
  - `test_store` 验证基础配置用户与动态用户安全合并，验证移除动态用户逻辑；
  - `test_bot` 验证非管理员阻断、基础配置用户防移除保护，以及数字 ID 格式校验。
- **安全平滑热重载（`/restart`）**：
  - 仅主管理员可用；
  - `test_bot` 验证当 `self.slot` 处于任务执行中时强阻断重启，防止掐断长任务；空闲时放行并就地触发 `os.execv`。

## Root 极简部署模式回归（--root 与 --allow-root）

- `test_installer` 验证 `--root` 参数生成以 `root:root` 运行、`ProtectHome=no` 的 systemd unit；
- 验证 `smoke` 自检测试在携带 `--allow-root` 参数时放行 root 执行，未携带时维持拒绝策略；
- 保证个人独立 VPS 用户免除多用户配置困扰，同时不放宽生产级默认安装模式的安全审计。
