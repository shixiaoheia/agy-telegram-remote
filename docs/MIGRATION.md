# 🔄 从旧版迁移、更新与回退指南

本次改造以仓库提交 `5e9ee9fed91beb4821003f7063b4602e0df4f50b` 为审阅基线。发布时若仓库已前进，应先比较差异，不强行覆盖后来的改动。

## 🌟 主要架构变化

原来的 `bot.py + install.sh` 改为几个职责明确的小模块。Python 运行时仅用标准库；不再创建或更新运行账户可写的 venv。程序放在 root 管理的发布目录，通过 `/opt/agy-telegram-remote` 入口切换。真实配置迁移到 `/etc/agy-telegram-remote/config.env`。

旧版 `/opt/agy-telegram-remote/.env` 仅作为数据读取，不作为 shell 脚本执行。程序不会从旧 venv 执行任何特权 Python 代码。

## 🛡️ 配置与权限保留原则

普通更新保留 Token、整个白名单、工作目录、已支持的数值限额、权限开关与 Google 登录。旧版缺少 `AGY_SKIP_PERMISSIONS` 时保留旧默认 `false`。只有全新安装或显式 `--enable-auto-approve` 才默认开启自动审批。

不支持的旧键、带空格/特殊字符的部署路径、未知安装目录、符号链接或带额外权限的运行账户会阻止自动迁移。原数据不会因此自动删除。先备份并人工核对，不要为了通过检查盲目修改所有权或删除账户。

## ⚙️ 更新执行顺序

1. 新的 root 所有目录下载选定提交；检查所有必需文件，收集/保留配置。
2. 以受限账户运行离线测试，验证 Telegram Token，检查没有活动 webhook。
3. 保存旧配置、unit 和服务状态，停止旧服务。正在执行的任务会被停止，不会自动重跑。
4. 安装缺失的 agy，复用或完成 Google 授权，用同一 Runner 做请求固定文字回复的自检（不单独降权）。
5. 将旧普通程序目录保留到 root 私有备份位置，切换新入口、配置与 unit。
6. 重新启动；验证 `getMe`、无活动 webhook、首次更新读取、状态存储及与 MainPID 匹配的就绪标志。
7. 失败时尝试恢复原入口、原配置、原 unit 和原来的服务启用/运行状态。

这是程序部署层的回退，不会撤销 apt 安装、Google 授权、CLI 缓存、已执行任务或其他外部副作用。回退也可能因磁盘/系统故障失败；脚本会明确提示，不宣称事务绝对原子。

## 💾 备份与回退管理

成功后脚本显示 `/var/backups/agy-telegram-remote/deploy-...` 的具体路径。其中可能有旧 Token 和 Google 相关配置，不得上传。旧版程序目录可能整体位于该备份内。

历史发布和备份不会自动删除，以免误删需要恢复的资料。管理员应在确认不再需要后自行清理，先核对当前入口：

```bash
readlink -f /opt/agy-telegram-remote
```

不要把备份目录公开，也不要直接运行备份中的旧虚拟环境。需要人工回退时先停止服务，核对备份中实际内容，再恢复对应的程序入口、配置和 unit；不要复制某个随机备份去覆盖正在运行的版本。

## 🧪 直接测试分支或指定提交

已审阅的新版尚未合并 main 时：

```bash
bash install.sh --ref 完整提交SHA
```

前提是正在执行的 `install.sh` 自身就是这份新版，不是旧下载文件。仅把新版上传到分支并不会改变 `main` 的一键安装地址；必须明确区分“已上传分支”“已创建 PR”和“已合并 main”。

## 💾 动态状态与偏好持久化继承

升级过程中，除核心配置文件之外，所有用户动态产生的状态与偏好文件均完整保留并继承：
- `/var/lib/agy-telegram-remote/whitelist.json`：动态白名单用户授权列表；
- `/var/lib/agy-telegram-remote/model-*.json`：每用户独立选择的 AI 模型；
- `/var/lib/agy-telegram-remote/effort-*.json`：每用户独立的思考深度设定；
- `/var/lib/agy-telegram-remote/mode-*.json`：每用户独立的执行模式设定（plan / accept-edits）；
- `/var/lib/agy-telegram-remote/conversation-*.json`：多轮对话连续上下文与轮次；
- `/var/lib/agy-telegram-remote/usage-*.json`：历史累计 Token 资源消耗报表。

新版程序升级后将自动加载上述私有文件（`0600` 权限保护），老用户无感知无缝过渡。

## 👑 模式迁移（标准沙箱模式 ↔ Root 极简模式）

- **从标准沙箱迁移到 Root 模式**：
  执行 `bash install.sh --root`，安装器将自动把 systemd 服务账户切换为 `root:root`，HOME 调整为 `/root`，工作区调整为 `/root`，同时保留原配置与 Token。
- **从 Root 模式切回标准沙箱模式**：
  执行 `bash install.sh`（或 `bash install.sh --install`），安装器将重新创建/审计 `agy-tg` 账户，恢复受限工作空间与 `ProtectHome=read-only` 策略。

## 🗑️ 卸载语义变化

新版默认 `--uninstall` 只移除服务，保留代码、配置与数据。`--uninstall --purge` 必须二次明确确认，且运行账户不能有剩余进程。彻底清理范围会先完整显示。
