# Changelog

## Unreleased — pure Root mode architecture consolidation and installer fix

- **步骤 3/3 Google 账号 OAuth 授权流完全汉化与交互式 CLI 包装（Localized Google OAuth CLI Wrapper）**（`manage.py`、`install.sh`、`tests/test_installer.py`、`README.md`）：
  - 新增 `manage.py auth-login` 子命令，通过标准库 `pty` 伪终端流式接管底层 `agy` 的 OAuth 授权交互；
  - 彻底拦截并汉化底层 Go CLI 的所有英文提示字符串（如 `Authentication required...`、`Waiting for authentication...`、`Or, paste the authorization code here...` 等）；
  - 终端以青色高亮显著展示 Google OAuth 网页授权链接，并提供清晰中文引导；
  - 在终端提示「👉 请在此处粘贴浏览器显示的授权码并按回车：」，读取用户输入的授权码后自动写入伪终端并流式监听验证结果；
  - 增加详细中文状态提示（「🔄 正在验证授权码并完成配置，请稍候……」、「✅ Google 账号授权成功！」及针对授权码失效/超时/网络故障的精准中文诊断）；
  - 覆盖已授权复用、首次授权交互成功、授权码无效、用户取消、缺少可执行文件及 CLI 参数调度等 6 项自动化单元与集成测试。
- **Telegram Bot Token 输入交互优化与可见性支持（Token Input UX Polish）**（`manage.py`、`tests/`、`README.md`、`docs/INSTALL_MENU.md`）：
  - 将安装向导步骤 1/3 的 Bot Token 输入改为标准可见输入，解决终端粘贴时不显示字符让用户误以为未录入或卡死的问题；
  - 增强已有 Token 提示（直接回车即可保留原 Token，粘贴新 Token 即时更新）；
  - 同步更新测试用例 `test_installer.py` 与 `test_install_menu.py`。
- **消除全新安装时 systemd 单元状态报错提示（Systemd Unit State Warning Fix）**（`install.sh`）：
  - 修复全新安装或未安装服务时，执行 `systemctl is-enabled` 泄漏 `Failed to get unit file state for agy-telegram-remote.service: No such file or directory` 报错信息干扰终端输出的问题；
  - 优化为仅在服务单元文件存在时检测自启状态，并对 `systemctl` 状态查询与自启设置重定向屏蔽标准错误，确保全新部署流程清爽无误导提示。
- **步骤 3/3 改为直接输出 Google 授权链接与纯 CLI 自动认证（Direct Google OAuth Flow）**（`install.sh`、`README.md`、`docs/INSTALL_MENU.md`、`docs/TESTING.md`）：
  - 彻底去除繁琐的 TUI 全屏界面（欢迎 ASCII art、配色方案选择、条款确认按钮、手动输入 `/exit` 等交互干扰）；
  - 改为纯命令行直接输出 Google OAuth 登录网址；用户在浏览器登录后将授权码粘贴回车，即自动完成认证并直接继续后续部署；
  - 授权前自动暂存并移开失效的旧 Token 文件，确保每次需要授权时均能立即干净生成全新登录链接。
- **修复步骤 3/3 授权自检失败无法进入交互式登录流程的缺陷（Google Auth Pre-check Fix）**（`install.sh`、`agy_runner.py`、`manage.py`、`settings.py`）：
  - 修复安装向导在步骤 3/3 进行已有授权复用检查（smoke）时，若返回非 10 状态码（如 15 unknown 错误）会跳过交互式登录流程直接报错中断安装的缺陷；调整为预检任何非零均顺利触发交互式授权引导；
  - 扩充 `agy_runner.py` 认证错误分类规则，广泛覆盖 OAuth、invalid_grant、token expired、unauthorized 及 401 等常见认证过期场景；
  - 优化 `parse_result` 与 `manage.py smoke` 的错误详情展示，完整回显底层返回的错误信息；
  - 加固旧配置迁移合并逻辑（`merged_config`），升级时自动净化历史遗留的非 root 路径（如 `/home/agy-tg` 与 `/srv/agy-workspace`），避免阻断配置验证。


- **全面精简为纯 Root 模式架构（Pure Root Architecture）**（`install.sh`、`settings.py`、`manage.py`、`.env.example`）：
  - 响应单机个人 VPS 用户极简部署诉求，彻底移除历史多用户非特权账户（`agy-tg`）及其配置逻辑；
  - 默认运行账户固定为 `root:root`，主目录 `HOME=/root`，默认工作区 `AGY_WORKSPACE=/root`；
  - `manage.py` systemd unit 默认配置 `User=root`、`Group=root`、`ProtectHome=no`，`smoke` 自检自适应支持 root 环境；
  - `settings.py` 强化工作区安全校验，将工作目录限制严格约束在 `/root` 范围内，禁止逃逸；
  - 卸载清理防御加固：彻底阻断 `--purge` 对 `/root` 目录与 `root` 账户的任何删除操作，绝对保障宿主机个人数据安全。
- **安装器关键进程检查缺陷修复（Installer Process Check Fix）**（`install.sh`）：
  - 修复以 root 模式部署时执行 `pgrep -u root` 错误匹配系统后台进程（如 systemd PID 1、sshd、journald 等）导致安装无条件中断报错的缺陷；
  - 精确调整为 `pgrep -f '/opt/agy-telegram-remote/bot.py'`，仅匹配旧版残留的本服务进程，杜绝误杀与安装阻塞。
- **测试套件与自动化验证更新（Test Suite Updates）**（`tests/`、`scripts/test_account_debian12.sh`）：
  - 更新 `scripts/test_account_debian12.sh`，转为验证 Debian 12 Root 模式运行环境；
  - 更新 `test_settings.py`、`test_installer.py`、`test_install_menu.py` 适配纯 Root 模式配置与菜单项；
  - 全套 172 项自动化断言测试在 `umask 022` 与 `umask 077` 环境下 100% 通过。
- **文档全量核对与同步更新（Documentation Sync）**（`README.md`、`docs/`、`SECURITY.md`）：
  - 全面更新 `README.md`、`docs/INSTALL_MENU.md`、`docs/MIGRATION.md`、`docs/TESTING.md` 与 `SECURITY.md`，移除已废弃的非特权沙箱账户说明，准确描述纯 Root 架构设计与安全边界。

## Unreleased — comprehensive documentation overhaul, system monitoring, workspace browsing, dynamic whitelist, engine effort & mode controls

- **全量安装体验与一键命令清晰化（Installation UX Polish）**（`install.sh`、`README.md`、`docs/INSTALL_MENU.md`）：
  - 核心文档聚焦直观、复制即用的一行式极简 Root 模式安装指令，免除多选项决策门槛；
  - `install.sh` 脚本启动横幅精准匹配生效模式（Root 模式与沙箱模式分别展示对应账户与工作区，杜绝误导提示）；
  - `install.sh` 优化安装就绪后的服务状态卡片、实时日志指引与 Telegram 使用下一步提示；
  - 新增完整的常用运维管理命令速查表（菜单、平滑更新、Root 模式切换、重新授权、日志追踪与安全卸载）。
- **全量系统性文档与使用指南深度重构（Comprehensive Documentation Overhaul）**（`README.md`、`docs/`、`SECURITY.md`）：
  - `README.md` 重构升级：新增覆盖全部 15 种指令与行为的速查表（含完整语法、参数与默认值、操作权限与典型场景）；
  - 新增 `/effort` 思考强度分级建议、`/mode plan` 只读推演零写入安全机制、多轮会话记忆工作流与 `/usage` 报表说明；
  - 补充 `/sys` 硬件与进程监控卡片、`/ls` 11 类 Emoji 文件类型图标映射表、相对子路径漫游沙箱防护详解；
  - 详细梳理单机 VPS 极简 Root 部署模式（`--root`）与标准沙箱模式的全维度对比表；
  - 扩充状态存储目录结构树（`0600` 权限独立状态文件清单）与核心配置项表格；
  - 常见问题排查（FAQ）扩充动态授权白名单安全模型、`/restart` 互斥锁保护机制、`/mode plan` 只读安全保障及 `--root` 适用选型建议；
  - `docs/INSTALL_MENU.md` 补全 `--root` 命令行参数与 Root 极简模式行为说明；
  - `docs/TESTING.md` 补全全部新功能 176 项断言回归测试分类细则；
  - `docs/MIGRATION.md` 增加升级期间动态白名单与用户偏好持久化继承机制以及标准/Root 模式互相平滑迁移指南；
  - `SECURITY.md` 补充动态白名单权限隔离与防踢保护、重启互斥锁、`/ls` 防越权遍历沙箱与 CLI 参数安全白名单规范。
- **原生 Linux VPS 硬件与进程实时监控（`/sys` & `/system`）**（`bot.py`）：
  - 纯标准库直接读取 Linux `/proc/uptime`、`/proc/loadavg`、`/proc/meminfo`、`/proc/<pid>/statm` 与平台系统信息；
  - 零外部三方依赖，秒级回传 VPS 系统负载、物理内存使用率、磁盘可用空间、Bot PID 及常驻内存（RSS MB）；
  - `/status` 指令同步聚合当前模型、思考强度、执行模式、会话轮次与常驻内存指标。
- **思考深度与执行模式精准控制（`/effort` & `/mode`）**（`bot.py`、`agy_runner.py`、`state_store.py`）：
  - 新增 `/effort` 指令与行内键盘切换（`low` / `medium` / `high` / `default`），用户偏好持久化（`effort-{user}.json`），底层向 `agy` 透传 `--effort`；
  - 新增 `/mode` 指令与行内键盘切换（`plan` 只读规划模式 / `code` 自动生成模式 / `default`），用户偏好持久化（`mode-{user}.json`），底层向 `agy` 透传 `--mode`；
  - 任务接收卡片与结果卡片实时显示思考深度与执行模式标签。
- **工作区安全文件浏览（`/ls` & `/files`）**（`bot.py`）：
  - 提供工作区最近修改文件（前 15 个）与相对路径快速浏览，展示文件大小（KB/MB）与修改时间戳；
  - 支持 `/ls <subpath>` 浏览子目录；内置严格路径规范化防穿越（防 `..` 越权、拦截软链接出界与敏感隐藏文件）。
- **Telegram 私聊动态白名单管理（`/whitelist`）**（`bot.py`、`state_store.py`）：
  - 管理员可在私聊直接运行 `/whitelist add <id>` 与 `/whitelist remove <id>` 动态授权与撤回权限；
  - 动态白名单持久化落盘至 `state/whitelist.json`（权限 0600），与静态配置安全合并，内置主管理员删除保护。
- **平滑就地热重载（`/restart`）**（`bot.py`）：
  - 管理员专用 `/restart` 指令，执行前严格校验当前任务槽位状态（正在执行任务时阻断）；
  - 空闲时通过 `os.execv` 原生重载当前进程，零停机更新 Python 代码与执行环境。
- **全面视觉图标与排版细节精细化打磨（UI & Icons Polish）**（`bot.py`）：
  - `/ls` 命令加入智能文件类型图标映射（Python `🐍`、Shell `🐚`、Markdown `📝`、配置 `⚙️`、日志 `📋`、数据库 `🗄️`、压缩包 `📦`、图片 `🖼️`、目录 `📁`、文件 `📄`），支持 `/ls <subpath>` 安全子目录浏览；
  - `/sys` 自动解析 `/etc/os-release` 获取人性化发行版全称（如 `Debian GNU/Linux 12 (bookworm)`）；
  - `/model` 行内按钮面板动态显示当前激活高亮图标（`🔘` / `⚪`）；
  - 全量控制指令菜单（`/help`、`/status`、`/usage`、`/whitelist`）全面规范化表情图标排版与状态卡片分割线。
- **自动化测试扩展至 176 项全面断言**（`tests/`）：
  - 新增 10 项测试覆盖 `/sys` 系统监控采集、`/effort` 与 `/mode` 选项解析与行内回调、动态白名单落盘与权限保护、`/ls` 路径沙箱越权拦截与子目录浏览、`/restart` 权限与空闲检测。

## Unreleased — interactive model keyboard, conversation memory, token metrics, and root mode

- **拟物化卡片与视觉排版全面重构**（`bot.py`）：
  - 任务分派确认（`_accept_and_work`）与最终交付结果（`describe`）全面采用 Antigravity 拟物风格卡片式排版；
  - 统一规范卡片标头、分割线（`━━━━━━━━━━━━━━━━━━━━`）、耗时指示（`⏱️ 12.4s`）与模型标签；
  - 交付卡片底部新增多维度执行元数据区块：Token 消耗明细（输入、输出、总计）、会话轮次进度与任务追踪 ID。
- **一键点击切换模型行内键盘（Inline Keyboard）**（`bot.py`、`telegram_api.py`）：
  - `/model` 指令不仅展示全量官方 14 种模型，同时动态下发 Telegram 行内按钮（`inline_keyboard`）；
  - 当前激活模型标示选中高亮（`🔘` / `⚪`），用户可直接点击按钮切换目标模型；
  - 点击后通过 Telegram `answerCallbackQuery` 实时弹出顶部 Toast 气泡提醒，并更新私聊消息。
- **连续对话与多轮记忆能力（Context Continuity）**（`bot.py`、`agy_runner.py`、`state_store.py`）：
  - 自动持久化与关联当前用户的会话上下文（`conversation_id`）；
  - 后台自动向 `agy` CLI 注入 `--conversation <id>` 保持连续多轮对话记忆与历史上下文；
  - 新增 `/new` 与 `/reset` 指令：随时一键重置清空当前记忆，开启全新独立会话；
  - 任务接收确认时智能指示上下文状态（`已关联上下文 (第 N 轮)` 或 `全新独立会话`）。
- **Token 消耗统计与 `/usage` 报表**（`bot.py`、`agy_runner.py`、`state_store.py`）：
  - 解析 `agy` CLI JSON 响应中回传的真实 Token 用量（`input_tokens`、`output_tokens`、`thinking_tokens`、`total_tokens`）；
  - 状态存储模块新增用户累计 Token 用量落盘（`usage-{user}.json`）与维护清理；
  - 新增 `/usage` 命令：汇总展示当前活动会话 ID、当前轮次进度，以及历史累计消耗的 Prompt / Output / Thinking / Total Token 详细报表。
- **极简 VPS 专用 Root 模式支持**（`manage.py`、`install.sh`）：
  - `install.sh` 新增 `--root` 参数：专为单机 VPS 用户设计，直接以 `root` 身份安装与运行后台守护进程，省去多用户权限切换与 `/home/agy-tg` 依赖；
  - `manage.py unit` 与 `smoke` 增加 `--user` 与 `--allow-root` 支持，适配 `root:root` 与 `ProtectHome=no` 安全策略。
- **全套测试覆盖扩展至 166 项**（`tests/`）：
  - 新增 9 项针对 Telegram 行内键盘、回调查询处理、会话透传、会话重置、Token 报表格式、Root 模式 unit 与 Root smoke 鉴权的自动化测试。

## Unreleased — documentation redesign and visual experience enhancement

基线提交：`1632ac6fe9a9f2d7e4ede47739d49a18fc2923b9`。

- **README 页面全面视觉重构与体验升级**（`README.md`）：
  - 引入居中 Hero Header、徽章集合（CI 状态、Python 3.10+、零依赖、TG 社区、推荐服务器）与快捷锚点导航；
  - 新增 Mermaid 架构与数据流图，直观展现 Telegram 私聊、后台非特权服务、Google agy CLI 与工作目录交互链路；
  - 新增 Telegram 私聊端交互效果演示（涵盖 `/model 3.8` 模型切换、任务分派接收与结构化输出回传）；
  - 结构化整合官方全量 14 种模型与快捷别名速查表；
  - 采用折叠式 `<details>` 交互卡片排版常见问题排查（附加组权限拦截、Token 校验、Webhook 清理、409 冲突等）。
- **Telegram 私聊端交互体验优化与状态指示美化**（`bot.py`、`agy_runner.py`）：
  - 任务执行结果全面升级直观表情徽标（`✅ 任务完成`、`⚠️ 缺少最终文字回复`、`🚫 权限拒绝`、`⏱️ 执行超时`、`🛑 任务已取消` 等）；
  - 任务完成状态新增实际执行耗时统计显示（如 `｜⏱️ 12.4s`）；
  - `/model` 指令列表动态高亮当前生效模型（`👉 [当前使用] ...`）；
  - `/status` 指令增加工作空间磁盘剩余空间汇报（`💾 工作空间可用磁盘：xx.x GB`）；
  - 长文本拆分消息发送时增加分页指示器（`📄 [第 1/3 页]`），阅读上下文更清晰；
  - `/start` 与 `/help` 帮助信息采用多行结构化排版并配备清晰功能图标；
  - 任务接收确认（`⏳ 任务已接收`）、`/id` 与 `/cancel` 响应加入友好图标与排版优化。
- **全套文档视觉风格一致化与校验计数同步**（`docs/TESTING.md`、`docs/INSTALL_MENU.md`、`docs/MIGRATION.md`）：
  - 同步更新测试验收文档中的离线测试断言总数至 157 项；
  - 为管理菜单说明与迁移回退指南全面引入统一风格的表情标头。

## Unreleased — model switching command (/model), full model catalog, and aliases

基线提交：`57423ed304df4675375818936f4cdb697bbf0957`。

- **动态模型切换指令与官方 14 种全量模型支持**（`bot.py`、`settings.py`）：
  - 新增 `/model` 控制命令：
    - `/model`：查看当前生效模型、全量官方 14 种模型列表（Gemini 3.8/3.7/3.6 Flash、3.1 Pro、Claude Sonnet 4.6/Opus 4.6、GPT-OSS 120B）与快捷别名；
    - `/model <模型名或别名>`：为当前用户切换首选模型，支持快捷别名（如 `3.8`、`pro`、`sonnet`、`opus`）与任意合法模型名称（通过安全正则校验 `[A-Za-z0-9._-]{2,64}`）；
    - `/model default`（或 `reset`/`auto`/`clear`）：恢复为系统默认配置；
  - `/status` 指令与任务接收确认消息同步显示当前选中的模型标识；
  - 任务执行结果标题附带模型标识（如 `任务 xxx｜gemini-3.1-pro-high`）；
  - `/start` 与 `/help` 补充 `/model` 使用提示。
- **每用户模型偏好持久化与清理**（`state_store.py`）：
  - 增加 `get_model(user)` 与 `set_model(user, model)`，以 `0600` 私有权限原子落盘至 `model-{user}.json`；
  - `maintain()` 周期性维护与启停阶段自动清理不在白名单内的用户模型偏好文件。
- **底层 agy 命令行透传**（`agy_runner.py`）：
  - `build_command` 与 `Runner.run` 支持透传 `--model <name>` 参数；
  - `Result` 数据结构新增 `model` 字段记录实际调用的模型名称。
- **全局可选模型默认配置与别名解析**（`settings.py`）：
  - `DEFAULTS` 新增可选配置 `AGY_MODEL`，允许在 `config.env` 中配置全局默认模型（支持别名自动映射与严格格式校验）。
- **测试覆盖**：
  - 新增 9 项单元测试（含别名映射、全量展示及安全校验），全套离线测试覆盖增至 153 项。

## Unreleased — config validation, startup retries, and responsive controls

基线提交：`cdb6b5b8ad6f359391c90e922f8fd305dbbc37cc`。

- **未知配置严格校验**（`settings.py`）：
  - `Settings.from_mapping` 遇到未在预设白名单内的配置项（如权限开关拼写错误）时抛出 `ConfigError` 拒绝启动；
  - 报错信息隐去配置具体数值，避免泄露敏感配置；补充相关负向单测。
- **Telegram 初始化退避重试**（`bot.py`）：
  - 启动阶段针对临时性网络故障（`code == 0`）、频控限制（`429`）与服务端错误（`5xx`）执行指数退避重试（带 `retry_after` 支持与最多 30 秒上限）；
  - 接收到停止信号可直接打断重试等待快速退出；
  - 针对 `400`、`401`、`403`、`409` 等明确的鉴权或请求错误保持不重试并立即失败退出。
- **解耦控制回复与长轮询接入**（`bot.py`）：
  - 引入有界异步队列（容量 32）处理 `/status`、`/help`、`/cancel`、`/last` 等控制回复，防止 Telegram 发送缓慢反压阻塞消息拉取与任务取消指令接收；
  - 队列满时丢弃新增非关键控制回复，保障取消信号能始终被调度处理；
  - 停机时安全清理正在等待投递的控制消息。
- **任务接收确认与工作目录互斥保护**（`bot.py`）：
  - 接收确认投递在后台任务（`_accept_and_work`）中异步完成，期间严格锁定全局任务槽 `self.slot`；
  - 若接收确认消息未成功投递、任务被取消或服务正在停止，先落盘持久化 `not_started` 记录，绝不启动 agy，绝不自动重跑；
  - 若任务准备出现未知异常，设置 `runner.blocked = True` 熔断后续任务。
- **公开 `/id` 命令全局频控**（`bot.py`）：
  - 增加 2 秒全局冷却窗口，防止未授权外部用户通过高频 `/id` 指令刷爆控制回复通道。
- **文档与测试更新**：
  - 更新 [`docs/TESTING.md`](docs/TESTING.md) 补充配置与消息可靠性回归说明；
  - 新增 10 项单元测试（涵盖慢回复、慢确认取消、队列满取消、`/id` 限流、初始化瞬态重试与快速停止等），离线测试总数提升至 144 项。

## Unreleased — management menu before sequential setup

基线提交：`da49dac47e2b3305c1ca19a01597159864c16d07`。

- **安装器入口管理菜单**（`install.sh`）：
  - 默认执行 `bash install.sh` 先显示管理菜单：1) 安装 / 更新、2) 卸载、0) 退出；
  - 选项 1 进入原有的三步向导（Token → ID → Google 授权），保留 manage.py 顺序输入；
  - 选项 2 进入原有的卸载流程与 UNINSTALL 确认；
  - 选项 0 或 EOF 安全退出，未进行任何提权、获取部署锁或系统修改；
  - 新增 `--install` 参数直接跳过菜单执行安装；保留 `--enable-auto-approve`、`--reauth`、`--ref`、`--uninstall`、`--purge` 等命令行快捷方式；
  - sudo 提权重启时自动透传选定模式参数，避免重复提示菜单。
- **文档与测试**：
  - 新增管理菜单说明文档 [`docs/INSTALL_MENU.md`](docs/INSTALL_MENU.md)；
  - 新增菜单交互与顺序测试 [`tests/test_install_menu.py`](tests/test_install_menu.py)（23 项新测试，全套测试增至 134 项）。

## Unreleased — account test hardening, deferred startup readiness, and deploy mutex

审阅基线：`8d72c4e233f480dca81ddc7565872500c4a7e776`。

- **账户测试脚本防误用与隔离强化**（`scripts/test_account_debian12.sh`）：
  - 强制要求显式传入 `--confirm-isolated-environment` 且环境变量 `AGY_TEST_ISOLATED_CONTAINER=1`，防止在非隔离环境或生产宿主机误执行；
  - 改用随机测试账户名 `test-agy-${RAND_SUFFIX}` 与 `/home/${TEST_USER}`，绝对禁止使用 `agy-tg` 或 `/home/agy-tg`；执行前若发现任何同名测试资源已存在则直接拒绝执行；
  - 注册严格的 `trap EXIT` 清理，精确跟踪并清理本次测试创建的用户、主组、测试附加组与临时家目录，移除所有无差别清理与盲目 `|| true`；
  - CI 容器工作流增加隔离参数并补充拒绝运行与资源存在的负向单元测试。
- **服务就绪判定递延至首次轮询成功**（`bot.py`、`manage.py`）：
  - 严格区分进程基础初始化完成与首次轮询就绪（`polling_ready`）；
  - 仅在首次长轮询请求成功返回时才在 `ready.json` 写入 `polling_ready: true`，首轮返回的消息确保仅被处理一次；
  - 若首次轮询遭遇 401、409 或网络异常，不写入就绪标志；
  - 服务退出或崩溃时通过 `finally` 撤销就绪文件；
  - `manage.py check-ready` 联合校验 PID、时间窗口（120秒内）及 `polling_ready: true`。
- **安装器部署排他互斥锁**（`install.sh`）：
  - 使用 `flock -n` 锁定 `/run/lock/agy-telegram-remote-deploy.lock`，杜绝并发执行安装、更新或卸载导致的配置覆盖与状态错乱；
  - `--help` 参数保持无副作用，不抢占锁也不触发特权检查。
- **Telegram update_id 边界与防重放核验**（Task 3）：
  - 调研 Telegram 官方规范关于空闲 7 天以上可能随机重置 `update_id` 的行为；由于盲目接受小 ID 会破坏针对网络重传与代理乱序的防重放保护，将其记录为**待验证项（Pending Verification）**，完整保留原有的 `uid < self.offset` 防重放机制并补充回归测试，不盲目修改核心 offset 逻辑。
- **测试覆盖扩展**：
  - 单元测试增至 111 项，新增用例覆盖就绪标记递延/撤销、部署排他锁、测试脚本防误用门禁及 `update_id` 防重放逻辑。

## Unreleased — dedicated service account without default user groups

复核基线：`fa51d06340ba1eac3b3ccd27d35876442031e2ed`。

- 修复安装账户兼容性问题：
  改用 `adduser --system --group --home "$APP_HOME" --shell /bin/bash "$APP_USER"` 创建专有系统服务账户，避免 Debian 12 默认将普通用户加入 `users` 附加组导致自身安全检查阻断。
- 附加组检查失败时打印实际所属组名单；仅在唯一附加组为 `users` 且管理员确认专用于本项目时，提示由 root 执行 `gpasswd -d agy-tg users`。
- 说明安装器由 root 或具备 sudo 权限的管理账户运行；`agy-tg` 仅用于运行服务，不用于执行安装器。修改安装脚本不会自动修复或清空已有账户的附加组。
- 严格保持已有账户策略：不删除、不重建已有账户，不改变已有 UID，不修改已有 home、工作目录、配置或 Google 授权，不授予 sudo 权限。
- 增加账户创建与组验证的单元回归测试，并在 GitHub Actions CI 中通过一次性 Debian 12 容器环境验证真实系统账户创建流程。

## Unreleased — installer umask regression fix

复核基线：`64d0715694069759853d64746d70bcfcff470cbd`。

- 修复离线测试在安装器 `umask 077` 环境中的误失败：
  公开目录测试显式设置临时目录为 `0755`，不再依赖调用者的 umask。
- CI 在每个 Python 版本增加 `umask 077` 复跑，覆盖安装器的权限环境。
- 同步测试说明；保留三步安装、自动审批、运行时权限检查及所有原有测试。
- 不改变 Bot、Runner、安装器执行逻辑或配置，不引入新依赖；上线和真实账号验收仍需单独进行。

## Unreleased — three-step installer and reliable result handling

审阅基线：`5e9ee9fed91beb4821003f7063b4602e0df4f50b`。这是待发布改造，不代表已经部署。

### 安装与更新
- 仅保留 Bot Token、数字 ID、Google 授权三个阶段。
- 默认目录、受限账户、依赖和 agy 自动准备；新安装默认自动审批。
- 普通更新保留旧权限和配置；新增显式 `--enable-auto-approve`、`--reauth`、`--ref`。
- root 所有代码、系统 Python；移除旧 venv 的特权执行与更新风险。
- 候选版本自检、配置/入口/unit 备份、失败回退、应用级就绪验证。
- 默认卸载保留程序与数据；彻底清理需要 `--purge` 和二次确认。

### 执行与回传
- 统一 JSON 协议；拒绝损坏、截断、缺少状态或无效字段。
- 空回复不再冒称任务完成；从不自动重复任务。
- 有界输出、超时、取消、父进程先退出后的同组子进程回收。
- 先保存结果，再回传 Telegram；新增 `/last`，只有消息 429 做有限投递重试。
- 私聊白名单、单工作目录互斥、持久更新水位和中断记录恢复。
- 标准库 Telegram HTTP 客户端，消除运行时 pip 依赖。

### 文档与测试
- 重写并同步 README、配置示例、迁移文档、安全边界与验收指南。
- 添加离线单元/集成测试、真实 Linux 进程测试、本地 HTTP 全链路测试与 GitHub Actions。
- 保留原项目致谢、社区和已有推广链接。
