# Antigravity Telegram Remote

把你的 Telegram 私聊变成服务器上的 `agy` 远程工作台。

[![CI Tests](https://github.com/shixiaoheia/agy-telegram-remote/actions/workflows/tests.yml/badge.svg)](https://github.com/shixiaoheia/agy-telegram-remote/actions/workflows/tests.yml)
[![Telegram Channel](https://img.shields.io/badge/TG%E9%A2%91%E9%81%93-Xiaohei%E7%9A%84%E7%A7%98%E5%AF%86%E5%9F%BA%E5%9C%B0-2CA5E0?logo=telegram)](https://t.me/xiaoheidemimi)
[![BandwagonHost VPS](https://img.shields.io/badge/%E6%8E%A8%E8%8D%90VPS-%E6%90%AC%E7%93%A6%E5%B7%A5-red)](https://bandwagonhost.com/aff.php?aff=80815)

📢 **Telegram 社区**：[Xiaohei的秘密基地](https://t.me/xiaoheidemimi)（欢迎交流体验与反馈）
🚀 **VPS 推荐**：[搬瓦工 BandwagonHost（专属推广通道）](https://bandwagonhost.com/aff.php?aff=80815)

你在 Telegram 发一句话，机器人就会在你的 VPS 上调用 Google Antigravity CLI（`agy`）完成任务，并把结果发回 Telegram。

> 这是给自己或完全信任的人使用的工具。它能在服务器工作目录中执行 AI 任务，请不要把 Bot Token 或白名单权限交给陌生人。

## 你能做什么

- 在手机上让 AI 查看、编写、修改代码或排查服务器问题。
- 选择模型和思考强度：发送 `/model` 后直接点击按钮。
- 任务运行时看到安全的执行摘要，每 5 秒更新一次，也可以点击“取消任务”。
- 任务较长时，Telegram 会持续显示“正在输入”；需要临时改方向可用 `/steer` 纠正。
- 发图片、日志或代码文件给机器人分析，单个文件最大 10 MB。
- 使用 `/history` 查看最近 10 条任务，并点开完整结果。

## 安装前准备

你需要：

1. 一台 Debian 12+ 或 Ubuntu 22.04+ 的 VPS，并能用 root 登录。
2. 一个 Telegram Bot Token：在 Telegram 找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示创建机器人并复制 Token。
3. 你的 Telegram 数字 ID。可通过可信的 Telegram ID 查询机器人取得；不要填写用户名。

首次安装时，脚本会引导你完成 Google `agy` 授权。

## 一键安装

登录服务器后，复制整段命令执行：

```bash
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh && bash install.sh --root
```

脚本会依次询问：

1. Telegram Bot Token；
2. 允许使用机器人的 Telegram 数字 ID；
3. Google `agy` 授权（仅首次或授权失效时需要）。

看到“安装成功”后，打开机器人私聊，发送 `/start`，再发送 `/model` 选一个模型就可以开始了。

## 日常使用

直接在 Telegram 私聊机器人发送需求，例如：

```text
检查当前项目的报错，说明原因并修复。
```

任务开始后会有一张状态卡：

- 显示已完成的工具步骤、耗时和经过脱敏的命令摘要；
- 不显示模型的内部推理、Token、密码或文件内容；
- 点击“🛑 取消任务”可请求停止当前任务。

如果任务还在运行，但你想补充或改变要求，发送：

```text
/steer 改为只检查登录模块，不要修改任何文件
```

机器人会先安全停止旧任务，完成进程清理后，自动用“原任务 + 最新修正”重新执行。含附件的任务为避免误用已清理的临时文件，请取消后重新发送附件和完整要求。

任务结束后会以更清爽的三段内容展示：完成状态、回复正文、运行统计。

### 图片与文件

可以直接发送：截图、`.log` 日志、`.txt`、`.md`、`.json`、`.py`、`.js`、`.sh` 等常见文本和代码文件。

机器人会先确认，例如：

```text
已收到：日志文件「error.log」，1.2 MB。本次任务会读取它。
```

附件只会保存在当前任务的私有临时目录，任务结束后自动删除。

## Telegram 指令

| 指令 | 用途 |
| --- | --- |
| `/start` | 查看欢迎页和快速开始说明 |
| `/model` | 点击选择模型，再选择思考强度 |
| `/model refresh` | 重新检查官方模型是否可用 |
| `/mode` | 选择执行模式 |
| `/new` 或 `/reset` | 清空当前对话记忆，开始新会话 |
| `/status` | 查看机器人和当前任务状态 |
| `/cancel` | 取消当前任务 |
| `/steer <修正内容>` | 停止当前纯文字任务，并按原任务加修正重新执行 |
| `/history` | 查看最近 10 条任务并打开完整结果 |
| `/last` | 重新查看最近一次任务结果 |
| `/usage` | 查看本会话的 Token 统计 |
| `/help` | 查看完整指令说明 |

## 模型选择

发送 `/model` 后直接点按钮，不需要输入模型名。

选择 Gemini Flash 或 Gemini Pro 后，机器人会继续展示三个思考强度按钮：

- ⚡ 极速：适合简单提问和快速修改；
- ⚖️ 均衡：速度与分析深度兼顾；
- 🧠 深度：适合复杂分析和大型重构。

Claude 与 GPT-OSS 使用模型自带的思考配置，选中后会直接收起选择卡片并显示当前模型。

## 更新

在服务器运行：

```bash
cd /root
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh
bash install.sh --root
```

更新时直接按回车即可保留原来的 Bot Token 和白名单。脚本会先检查新版本，再替换服务；未完成任务不会自动重跑。

也可以运行 `bash install.sh` 打开菜单：

```text
1) 安装
2) 更新
3) 彻底卸载
0) 退出
```

## 彻底卸载

选择菜单 `3`，然后按提示输入 `UNINSTALL` 和 `PURGE` 两次确认。

如果是旧版安装且检测到标准 `agy`，脚本还会询问是否一并删除它和 Google 登录资料；希望一起清除时输入 `ERASE_AGY`。

这会删除本项目的：

- systemd 服务和运行文件；
- 程序发布版本；
- Bot Token 配置、白名单、任务历史和模型偏好；
- 备份文件与 Bot 接收附件的临时目录；
- 如果 `agy` 是本安装器安装的，还会删除该 `agy` 和它的 Google 登录资料。

为了避免误删你的项目，`/root` 工作目录和里面的代码、文件不会删除。BotFather 里创建的 Telegram Bot 也不会被删除；如不再需要，请到 BotFather 手动删除。

## 常见问题

### 能查看服务器，但 Agent 修改文件失败

查看成功不代表有写入权限。`/sys` 直接读取系统信息，不经过 Agent；安装自检也只验证固定文字回复，不验证文件修改。

先发送 `/status` 查看执行模式、工具自动审批和额外可写目录。三层限制分别处理：

1. **只给计划、不执行**：发送 `/mode code` 选择落地编辑。`/mode default` 跟随服务器上的 agy 设置，不保证自动编辑。
2. **`soft-denied` / 工具审批拒绝**：旧安装可能保留 `AGY_SKIP_PERMISSIONS=false`。无交互运行无法弹出审批窗口；可以配置 agy 的精确 `permissions.allow` 规则，或在完全信任的个人服务器上明确使用安装器 `--enable-auto-approve`。
3. **`Read-only file system` / `EROFS`**：服务默认启用 `ProtectSystem=strict`，仅允许 home、工作目录、状态目录及私有临时目录写入。即使 root、落地编辑和工具自动审批均已开启，`/etc`、`/opt` 等应用目录仍可能只读。

需要修改工作区外的应用文件时，先备份目标应用配置，再用 `--write-paths` 明确开放**具体且已存在的目录**。例如只管理 Nginx 配置（实际目录不同请替换）：

```bash
cd /root
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh
bash install.sh --root --write-paths /etc/nginx
```

多个目录以逗号分隔，例如 `--write-paths /etc/nginx,/opt/my-app`。此参数替换完整的额外目录列表；不传时保留旧列表，传 `--write-paths ''` 清空列表。不接受根目录、整个顶层目录、符号链接或不存在的目录，不会自动创建、改属主或放宽文件权限。升级仍保留原 Token、白名单和自动审批选择；只有确实需要开启所有工具审批时，才额外加 `--enable-auto-approve`。

这些目录对所有已授权白名单用户生效。`--write-paths` 只解除指定目录的 systemd 只读挂载限制，**不授予完整服务器管理权限**；文件权限、ACL、只读磁盘和 `CapabilityBoundingSet` 仍可能阻止操作。`Operation not permitted` 不应通过反复重新 Google 授权来解决。

更新后可在 SSH 中查看实际生效的服务限制（`/status` 展示的是配置值，额外 systemd drop-in 可能改变实际行为）：

```bash
systemctl show agy-telegram-remote -p ProtectSystem -p ReadWritePaths -p NoNewPrivileges -p CapabilityBoundingSet
```

再在 Telegram 用 `/mode code`，明确让 Agent 在已授权的测试目录创建一个无关紧要的测试文件，读回并核对内容。核实后自行删除测试文件；不要直接用生产配置测试。已有任务不会自动重跑，已经写入的内容不会自动恢复。

参考：[agy 无交互权限](https://antigravity.google/docs/cli/headless/#permissions-in-headless-mode)、[执行模式](https://antigravity.google/docs/cli/modes/)。

### 机器人不回复

先在服务器执行：

```bash
systemctl status agy-telegram-remote --no-pager
journalctl -u agy-telegram-remote -n 80 --no-pager
```

如果服务不是 `active`，先再次运行更新命令。若日志提示授权失效，运行：

```bash
cd /root && bash install.sh --root --reauth
```

### 模型无法使用

在 Telegram 发送 `/model refresh`，等待检测完成后再选择模型。不同 Google 账号可用的模型可能不同。

### 我想停止任务

点击任务卡下面的“🛑 取消任务”，或发送 `/cancel`。已经写入工作目录的文件不会自动恢复。

## 目录说明

通常不需要手动修改这些目录：

| 路径 | 用途 |
| --- | --- |
| `/opt/agy-telegram-remote` | 当前运行的程序版本 |
| `/etc/agy-telegram-remote/config.env` | Bot Token 与白名单配置，仅 root 可读 |
| `/var/lib/agy-telegram-remote` | 任务记录、模型偏好与 Telegram 轮询状态 |
| `/var/backups/agy-telegram-remote` | 更新前的安全备份 |

## 安全提醒

- 只把自己的 Telegram 数字 ID 加入白名单。
- 不要把 Bot Token、Google 授权码、密码或私钥发到聊天记录、截图或公开仓库。
- AI 的执行结果需要你自己复核，特别是涉及删除文件、重启服务或修改生产配置的任务。

## 开发与测试

在 Debian 或 Ubuntu 上克隆项目后运行：

```bash
python3 -B -m unittest discover -s tests
```

项目使用 Python 标准库，无需安装第三方 Python 包。

## 致谢与支持

- 感谢开源项目 [whypuss/agy-telegram-bot](https://github.com/whypuss/agy-telegram-bot) 提供早期灵感与原型参考，也感谢 [Google agy 文档](https://antigravity.google/docs/cli/install/) 与 [Telegram Bot API](https://core.telegram.org/bots/api)。
- 📢 欢迎加入 [Xiaohei的秘密基地](https://t.me/xiaoheidemimi) 获取更新、交流使用体验和反馈问题。
- 🚀 如需稳定 VPS，可通过 [搬瓦工 BandwagonHost 专属邀请链接](https://bandwagonhost.com/aff.php?aff=80815) 支持项目。

⭐ 如果这个项目对你有帮助，欢迎在 GitHub 右上角点一个 Star 支持一下！
