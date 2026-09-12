<div align="center">

# 🤖 Antigravity Telegram Remote

**把 Telegram 私聊打造为你专属的 Google Antigravity CLI (`agy`) 远程云端交互终端**

[![CI Tests](https://github.com/shixiaoheia/agy-telegram-remote/actions/workflows/tests.yml/badge.svg)](https://github.com/shixiaoheia/agy-telegram-remote/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero%20External-success)](#-零第三方依赖)
[![Telegram Channel](https://img.shields.io/badge/TG%E9%A2%91%E9%81%93-Xiaohei%E7%9A%84%E7%A7%98%E5%AF%86%E5%9F%BA%E5%9C%B0-2CA5E0?logo=telegram)](https://t.me/xiaoheidemimi)
[![BandwagonHost VPS](https://img.shields.io/badge/%E6%8E%A8%E8%8D%90VPS-%E6%90%AC%E7%93%A6%E5%B7%A5-red)](https://bandwagonhost.com/aff.php?aff=80815)

[✨ 核心亮点](#-核心亮点) • [⚡ 极简安装](#-极简三步安装) • [🎮 使用指南](#-使用指南与指令速查) • [🧠 官方模型切换](#-官方全量模型支持与快捷别名-model) • [⚙️ 目录与配置](#-目录与配置说明) • [❓ 常见排错](#-常见问题排查-faq)

</div>

---

> 📢 **官方交流社区**：加入 Telegram 频道 **[Xiaohei的秘密基地](https://t.me/xiaoheidemimi)**，实时获取最新版本发布、使用技巧与答疑交流！<br>
> 🚀 **推荐服务器**：建站与稳定运行 VPS 首选推荐 **[搬瓦工 BandwagonHost（专属优惠通道）](https://bandwagonhost.com/aff.php?aff=80815)**。

---

## 📖 项目简介

**Antigravity Telegram Remote** 能够将 Telegram 私聊转变为你在 Linux 服务器上调用 **Google Antigravity CLI (`agy`)** 的安全控制入口。

只需向 Telegram 机器人发送自然语言指令，程序便会在服务器的隔离工作目录中调用 `agy`，执行代码重构、环境分析、脚本编写与运维调试，并将结构化结果实时回传给你的 Telegram。

### 🔄 架构与工作流

```mermaid
flowchart LR
    User([📱 Telegram 私聊]) <-->|HTTPS TLS / 严格白名单| Bot([🤖 agy-telegram-remote\n专有系统账户 agy-tg])
    Bot <-->|JSON Headless 规范交互| CLI([⚙️ Google agy CLI])
    CLI <-->|安全 API 交互| Cloud([☁️ Google AI / Antigravity 算力])
    CLI <-->|受限读写沙箱| WorkDir[📁 /srv/agy-workspace]
```

### 💬 交互效果演示

```text
👤 你：
/model 3.8

🤖 Antigravity Remote：
🎯 模型已切换为：gemini-3.8-flash-high
💡 最新极速高思考模型（推荐主力使用）

👤 你：
请分析当前目录下的 Python 文件结构并给出性能优化建议

🤖 Antigravity Remote：
⏳ 任务已接收 [task-1741800000] (gemini-3.8-flash-high)
正在受控工作目录中执行，请稍候...

🤖 Antigravity Remote：
✅ 任务完成｜gemini-3.8-flash-high
━━━━━━━━━━━━━━━━━━━━
【分析报告摘要】
1. 已扫描完成当前工作目录下的 6 个核心模块；
2. 发现 2 处潜在边界条件需补充异常捕获；
3. 建议使用生成器优化数据流式处理，可降低约 35% 内存峰值占用。
```

---

## ✨ 核心亮点

- 🛡️ **严格安全边界与权限沙箱**：
  - **白名单机制**：严格拒绝群聊，仅允许预设数字 ID 的白名单私聊用户访问；
  - **非特权专有系统账户**：后台服务强制运行在无额外附加组、无 sudo 特权的系统账户 `agy-tg` 上；
  - **符号链接防护与防穿越**：工作目录与状态文件均开启强制正则检查与符号链接拦截。
- ⚡ **零第三方依赖**：
  - 纯 Python 3.10+ 标准库（`asyncio` / `urllib.request` / `subprocess` / `json` 等）精心打造；
  - 无需 `pip` 安装，不依赖虚拟环境、Redis 或外部数据库，系统轻盈无负担。
- 🧠 **全量 14 种官方模型即时切换（`/model`）**：
  - 内置 Google Antigravity 官方支持的全部 14 种模型，涵盖 Gemini 3.8/3.7/3.6 Flash、Gemini 3.1 Pro、Claude Sonnet 4.6、Claude Opus 4.6 思考模型与开源基座；
  - 支持人性化快捷别名（如 `/model 3.8`、`/model pro`、`/model sonnet`、`/model opus` 等）；
  - 每用户偏好独立私密持久化，退出自动清理，并支持自由透传未来官方新模型。
- 🚦 **高可靠消息引擎与控制解耦**：
  - 引入有界异步队列解耦任务与控制通道，长任务执行期间 `/cancel`、`/status` 秒级响应，彻底杜绝网络卡顿引发的假死；
  - 启动阶段内置网络退避重试，优雅吸收突发 429 或瞬态网络抖动。
- 📦 **极简三步交互式部署向导**：
  - 自带交互式管理菜单（安装/更新、卸载、退出）；
  - 极简三步走：`Bot Token` → `Telegram 数字 ID` → `Google 授权`，自动配置 systemd 守护进程。

---

## 🖥️ 环境要求

- **操作系统**：Debian 12+ 或 Ubuntu 22.04+
- **系统特权**：具备 systemd 环境、拥有 root 或 sudo 管理权限
- **网络条件**：能够正常访问 GitHub、Google 与 Telegram Bot API 网络
- **准备清单**：
  1. Telegram Bot Token（向官方 [@BotFather](https://t.me/BotFather) 申请）
  2. 你的 Telegram 数字用户 ID（向 [@userinfobot](https://t.me/userinfobot) 获取）
  3. 可用于 Google 授权登录的账号与浏览器

> [!NOTE]
> 社区开源项目，与 Google、Telegram 官方无隶属关联。请仅部署到你拥有所有权或获得授权的服务器与 Bot。

---

## ⚡ 极简三步安装

在具备 root 或 sudo 权限的 SSH 终端中执行以下命令（**先下载脚本，再用 bash 交互运行**）：

```bash
curl -fsSLO https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh
bash install.sh
```

> [!TIP]
> **为什么不建议 `curl ... | bash`？**
> 本项目向导需要在终端中安全接收隐藏输入的 Bot Token 与数字 ID，先下载后运行能确保最佳的交互体验与安全审阅。

### 📋 安装向导流程一览

运行 `bash install.sh` 后将首先显示**管理菜单**：

```text
=============================================
 Antigravity Telegram Remote 管理菜单
=============================================
  1) 安装 / 更新
  2) 卸载
  0) 退出

请输入选项 [0/1/2]：1
```

选择 `1` 进入三步极简安装向导：

```text
=============================================
 Antigravity Telegram Remote 极简一键安装向导
=============================================

步骤 1/3：请输入 Telegram Bot Token：
> （密码模式输入隐藏，保障凭据安全）

步骤 2/3：请输入 Telegram 数字 ID：
> 123456789

自动准备运行账户、/srv/agy-workspace、系统依赖与 Google agy……

步骤 3/3：Google 账号授权
- 若已有历史有效授权，将自动识别并复用；
- 首次授权请打开终端显示的授权链接 → 浏览器登录 → 粘贴授权码；
- 进入 agy 终端主界面后输入 /exit 即可返回安装器。

正在验证 agy 回复、Telegram 接口及服务自检……

🎉 安装成功！后台守护服务已启动就绪。
```

> [!TIP]
> **平滑升级**：后续版本更新时，直接重新执行 `bash install.sh` 并选 `1`，步骤 1 和步骤 2 直接按回车即可完整保留原有的 Token、白名单与运行配置，平滑无缝升级！详见 [管理菜单说明文档](docs/INSTALL_MENU.md)。

---

## 🔍 安装后验证

安装成功后，可通过 systemd 状态指令核验后台守护进程：

```bash
# 查看服务实时运行状态
sudo systemctl status agy-telegram-remote --no-pager

# 查看最近服务日志
sudo journalctl -u agy-telegram-remote -n 80 --no-pager
```

### 🤖 私聊 Bot 自检三步曲

打开 Telegram 私聊刚刚绑定的机器人，依次发送：

1. `/start`：验证机器人是否在线，返回欢迎界面与命令说明；
2. 发送测试任务：`只回复“连接成功”，不要使用工具或修改任何文件。`，验证基础执行流程与结果回传；
3. `/last`：取回上一条任务保存的完整结果，验证状态持久化与本地存储。

---

## 🎮 使用指南与指令速查

在与 Bot 的私聊中，支持以下全部指令：

| 快捷指令 | 功能分类 | 详细行为说明 |
| :--- | :--- | :--- |
| 💬 **直接发送文本** | 发送新任务 | 在固定工作目录启动一次独立任务（单次执行，不自动继承上文历史） |
| 🧠 `/model` | 模型管理 | 查看当前选定模型、列出全部 14 种模型全量目录，或快速切换模型 |
| 📊 `/status` | 状态监控 | 查看当前是否有任务正在执行、工作目录占用及当前生效的模型 |
| 🛑 `/cancel` | 应急中止 | 立即请求中断正在执行的自身任务，并安全回收下属全部进程组 |
| 📜 `/last` | 结果回溯 | 重新获取上一次任务执行完成的完整结果（仅读取本地持久化，不消耗算力） |
| 🆔 `/id` | 身份识别 | 在私聊中显示当前账号的 Telegram 数字 ID（内置 2 秒防刷限频） |
| ❓ `/help` | 帮助信息 | 随时呼出命令提示与操作指南 |

---

## 🧠 官方全量模型支持与快捷别名（`/model`）

机器人深度适配了 Google Antigravity CLI 官方全量 14 种 AI 模型，支持按需随时一键切换，并提供人性化快捷别名：

| 类别 / 家族 | 官方模型标识符 | 推荐快捷别名 | 特点与适用场景 |
| :--- | :--- | :--- | :--- |
| 🚀 **Gemini 3.8 Flash** | `gemini-3.8-flash-high` | `3.8` / `3.8-high` / `flash` | 🔥 **官方推荐**，超高思考等级，综合性能强悍 |
| | `gemini-3.8-flash-medium` | `3.8-med` | 中等思考强度，平衡速度与深度 |
| | `gemini-3.8-flash-low` | `3.8-low` | 低思考强度，极速响应简单指令 |
| ⚡ **Gemini 3.7 Flash** | `gemini-3.7-flash-high` | `3.7` / `3.7-high` | 经典高效思考模型（高思考） |
| | `gemini-3.7-flash-medium` | `3.7-med` | 适度思考（中思考） |
| | `gemini-3.7-flash-low` | `3.7-low` | 快速返回（轻度思考） |
| 💡 **Gemini 3.6 Flash** | `gemini-3.6-flash-high` | `3.6` / `3.6-high` | 轻量化推理（高思考） |
| | `gemini-3.6-flash-medium` | `3.6-med` | 基础日常辅助（中思考） |
| | `gemini-3.6-flash-low` | `3.6-low` | 纯指令直接处理（低思考） |
| 🧠 **Gemini 3.1 Pro** | `gemini-3.1-pro-high` | `pro` / `3.1` / `pro-high` | 🎯 **深度推理旗舰**，适合复杂重构与架构设计 |
| | `gemini-3.1-pro-low` | `pro-low` | 快速专业推理 |
| 🎭 **Anthropic Claude** | `claude-sonnet-4-6` | `sonnet` | Claude Sonnet 4.6 深度思考与精准编码 |
| | `claude-opus-4-6-thinking` | `opus` | Claude Opus 4.6 顶级旗舰思考模型 |
| 🌐 **开源顶级基座** | `gpt-oss-120b-medium` | `gpt` / `120b` | 1200 亿参数开源大模型基座 |

### 💡 `/model` 常用操作示例

- **查看当前状态与完整列表**：直接发送 `/model`
- **快捷别名切换**：
  - 切换到 Gemini 3.8 高思考版：`/model 3.8` 或 `/model flash`
  - 切换到 Gemini 3.1 Pro 旗舰版：`/model pro`
  - 切换到 Claude Sonnet：`/model sonnet`
  - 切换到 Claude Opus 思考版：`/model opus`
  - 切换到开源 120B：`/model 120b`
- **重置为系统默认**：`/model default`
- **自由扩展**：未来若官方上线新模型（如 `gemini-4.0-flash`），可直接输入 `/model <新模型名>` 进行直连透传！

---

## 📂 目录与配置说明

系统采用标准化生产级目录划分，遵循 Linux Filesystem Hierarchy Standard (FHS)：

| 路径 | 权限模式 | 作用说明 |
| :--- | :--- | :--- |
| `/etc/agy-telegram-remote/config.env` | `root:agy-tg` (0640) | 核心生产配置文件（含 Token、白名单），严禁对外泄露 |
| `/opt/agy-telegram-remote` | `root:root` (0755) | 当前 root 管理的程序发布软链入口 |
| `/opt/agy-telegram-remote-releases/` | `root:root` (0755) | 程序历史与当前版本发布目录 |
| `/srv/agy-workspace` | `agy-tg:agy-tg` (0700) | 核心任务工作目录（所有任务执行时默认以此目录为根） |
| `/home/agy-tg` | `agy-tg:agy-tg` (0700) | `agy-tg` 账户 HOME、agy 核心二进制与 Google 授权凭据目录 |
| `/var/lib/agy-telegram-remote` | `agy-tg:agy-tg` (0700) | 持久化任务结果、每用户模型偏好及轮询水位存储 |
| `/run/lock/agy-telegram-remote-deploy.lock` | `root:root` | 安装、更新、卸载部署排他锁，防止并发操作踩踏 |
| `/var/backups/agy-telegram-remote/` | `root:root` (0700) | root 专有升级备份目录，保障失败时可安全回滚 |

### ⚙️ 修改配置与重载服务

如需调整参数（如白名单列表、任务超时时限等），可直接编辑配置文件：

```bash
# 安全编辑配置文件（受限权限保护）
sudoedit /etc/agy-telegram-remote/config.env

# 重启服务使新配置立即生效
sudo systemctl restart agy-telegram-remote
```

支持的完整配置项与缺省说明可参考 [.env.example](.env.example)。

---

## 🔄 升级、迁移与维护

项目升级极其简便，无需繁琐的人工步骤：

1. **一键智能更新**：
   ```bash
   bash install.sh
   ```
   输入 `1` 进入安装/更新流程。脚本会自动检测已有配置并提示，**在步骤 1 和步骤 2 中直接回车**，将完整保留原有 Token、白名单、工作目录与 Google 登录凭据，并在通过离线测试后平滑就绪。

2. **常用维护选项**：
   - **强制重新登录 Google 授权**：`bash install.sh --reauth`
   - **为旧版升级明确开启自动审批**：`bash install.sh --enable-auto-approve`
   - **指定固定 GitHub Commit SHA 升级**：`bash install.sh --ref <COMMIT_SHA>`

更多详细迁移、回退及备份机制请参阅 [docs/MIGRATION.md](docs/MIGRATION.md)。

---

## 🗑️ 卸载与清理

提供了两种不同安全等级的卸载模式：

```bash
# 模式 A：安全卸载（默认）—— 选择管理菜单 2，或直接执行：
bash install.sh --uninstall
```
> [!NOTE]
> 安全卸载模式下，系统仅停止并移除 systemd 服务单元，**完整保留程序、配置文件、工作目录成果及 Google 授权**，输入 `UNINSTALL` 确认，防止误删重要资产。

```bash
# 模式 B：彻底清理（Purge 模式）
bash install.sh --uninstall --purge
```
> [!CAUTION]
> 彻底清理模式将永久删除代码发布目录、全部配置与任务记录、工作目录、备份文件以及专有运行账户 `agy-tg`。此操作需要额外输入 `PURGE` 二次确认。

---

## ❓ 常见问题排查（FAQ）

<details>
<summary><b>🔴 报错：检测到账户存在多余附加组权限（如 users 组）？</b></summary>

- **原因**：部分 Debian 12 / Ubuntu 系统的默认安全策略会在创建账户时附加 `users` 组，安装器内置的权限合规检查会拦截该行为，以防权限扩散。
- **解决办法**：若管理员确认该账户确为本项目专用，执行以下命令移除非特权附加组即可继续安装：
  ```bash
  sudo gpasswd -d agy-tg users
  ```
</details>

<details>
<summary><b>🔴 报错：Bot Token 检查失败或无法连通？</b></summary>

- **原因**：通常为复制粘贴时携带了空格、换行符，或者服务器无法正常直连 Telegram API 服务器。
- **排查建议**：
  1. 重新从 [@BotFather](https://t.me/BotFather) 完整复制 Token；
  2. 测试服务器能否正常访问 Telegram API：
     ```bash
     curl -I https://api.telegram.org
     ```
</details>

<details>
<summary><b>🔴 提示：检测到当前 Bot 已被配置 Webhook？</b></summary>

- **原因**：本项目采用标准的长轮询（Long Polling）机制，如果之前使用过该 Bot 配置了 Webhook 会发生接口冲突。
- **解决办法**：使用浏览器或 curl 调用一次 Telegram 官方接口删除旧 Webhook：
  ```bash
  curl -s "https://api.telegram.org/bot<你的TOKEN>/deleteWebhook"
  ```
</details>

<details>
<summary><b>🔴 日志出现 Telegram 409 Conflict 冲突？</b></summary>

- **原因**：说明当前有另外一个实例正在使用相同的 Bot Token 进行轮询。
- **解决办法**：检查是否有其他服务器同时启动了该 Bot，或者本地有残留的手工调试进程。确保同一时刻只有一个服务实例使用该 Token。
</details>

<details>
<summary><b>🔴 如何查看详细错误日志与排查？</b></summary>

- 使用 systemd 诊断日志指令快速追踪：
  ```bash
  # 实时追踪运行日志
  sudo journalctl -u agy-telegram-remote -f
  ```
</details>

---

## 🛡️ 安全设计与架构边界

为保证服务器与数据的绝对安全，使用前请了解以下设计边界：

1. **非特权最小权限原则**：程序绝不以 root 身份运行后台守护进程，专属账户 `agy-tg` 被剥离所有非必要附加组和提权能力。
2. **工作目录边界与防提权**：建议将 `agy` 限制在专用 VPS 或独立工作目录内。非 root 运行与目录隔离能抵御绝大多数越权，但不等同于内核级绝对容器隔离，请勿将敏感生产数据存放在同一系统内。
3. **白名单防线**：任何未在配置白名单中的 Telegram 用户发送的消息均会被静默丢弃，群聊消息一律直接忽略。
4. **单任务互斥执行**：共享工作目录下同一时刻仅允许运行一个任务，杜绝并发竞争写引发的文件冲突。

更详尽的安全规范与安全审计策略请参阅 [SECURITY.md](SECURITY.md)。

---

## 🧪 开发者与测试验证

本项目包含完备的离线自动化测试套件（含 153 项全面断言测试），覆盖语法、沙箱权限、参数注入防御、状态持久化与别名解析：

```bash
# 运行离线测试套件（零外部依赖，使用模拟 Telegram 与虚拟 agy）
bash scripts/verify.sh
```

- 测试规范与本地双权限校验详见 [docs/TESTING.md](docs/TESTING.md)。
- GitHub Actions CI 矩阵配置文件位于 [`.github/workflows/tests.yml`](.github/workflows/tests.yml)。

---

## 🤝 致谢与官方参考

- 🌟 本项目最初灵感与原型参考自开源项目 [whypuss/agy-telegram-bot](https://github.com/whypuss/agy-telegram-bot)，向原作者 [@whypuss](https://github.com/whypuss) 致以崇高谢意！
- 📖 [Google agy 官方文档与安装指南](https://antigravity.google/docs/cli/install/)
- 📖 [Google agy Headless 规范与参数参考](https://antigravity.google/docs/cli/headless/)
- 📖 [Telegram Bot API 官方技术文档](https://core.telegram.org/bots/api)

---

<div align="center">

### 🌐 关注与支持

📢 **Telegram 社区**：[Xiaohei的秘密基地](https://t.me/xiaoheidemimi)（欢迎进群交流体验与反馈问题）<br>
🚀 **优质 VPS 推荐**：稳定高速 VPS 推荐使用 [搬瓦工 BandwagonHost（专属推广通道）](https://bandwagonhost.com/aff.php?aff=80815)

⭐ **如果这个项目对你有帮助，欢迎在 GitHub 点亮右上角的小星星 Star 支持一下！** ⭐

</div>
