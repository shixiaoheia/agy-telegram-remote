# Changelog

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
