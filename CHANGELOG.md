# Changelog

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
