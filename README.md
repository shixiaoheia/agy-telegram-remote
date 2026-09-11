# AGY Telegram Remote

通过 Telegram 私聊，远程使用服务器上的 Google Antigravity CLI（`agy`）。

> 这是一个将 Telegram 作为远程控制入口的工具：你发送任务 → 服务器上的 agy 在指定工作目录执行 → 结果返回 Telegram。

它不是群组机器人，也不是普通 AI 聊天机器人：默认不监听群聊，只接受白名单用户的私聊消息。

---

## 功能

- 在 Telegram 私聊中向 agy 提交任务
- 显示任务正在分析、读文件、执行命令等进度
- 将 agy 的最终回复分段发回 Telegram
- 每位授权用户保留独立会话，方便继续同一个项目任务
- `/status` 查看是否有任务运行
- `/cancel` 中止当前任务
- `/reset` 清除当前会话，开始新任务
- 仅允许配置的 Telegram 用户 ID 使用

## 工作流程

```text
Telegram 私聊
      ↓
AGY Telegram Remote
      ↓
受限 Linux 用户 agy-tg
      ↓
Google Antigravity CLI（agy）
      ↓
指定工作目录，例如 /srv/agy-workspace
      ↓
结果和进度回到 Telegram
```

## 部署前准备

| 项目 | 是否必需 | 说明 |
|---|---:|---|
| Linux 服务器 | 是 | 推荐 Debian 12 或 Ubuntu 22.04+ |
| Telegram Bot Token | 是 | 由 @BotFather 创建 |
| Google Antigravity CLI | 是 | 实际执行任务的 `agy` |
| Google 登录或 Gemini API Key | 是 | 为 agy 提供授权 |
| Telegram 数字 ID | 是 | 用作私聊白名单 |
| Git | 建议 | 方便 agy 管理项目改动 |

本项目使用 Telegram 长轮询，**不需要域名、Nginx、Webhook 或开放新的 HTTP 端口**。

---

## 1. 创建 Telegram Bot

1. 在 Telegram 搜索 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示设置机器人名称和用户名。
4. 保存 BotFather 返回的 Token。

Token 类似：

```text
123456789:AAExampleToken
```

Token 相当于机器人的密码。不要发给别人、不要放截图、不要提交到 GitHub。

---

## 2. 创建专用的 Linux 账户

不要用 `root` 跑 Telegram Bot 或 agy。

```bash
sudo adduser --disabled-password --gecos "" agy-tg
sudo mkdir -p /srv/agy-workspace
sudo chown -R agy-tg:agy-tg /srv/agy-workspace
```

确认：

```bash
id agy-tg
```

请不要把 `agy-tg` 加到 `sudo`、`docker` 或其他高权限用户组。

---

## 3. 安装 Google Antigravity CLI

切换到专用账户：

```bash
sudo -iu agy-tg
```

按 Google 官方方式安装：

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
agy --version
```

通常 `agy` 位于：

```text
/home/agy-tg/.local/bin/agy
```

官方文档：

- [安装与认证](https://antigravity.google/docs/cli/install/)
- [AGY CLI 快速开始](https://antigravity.google/docs/cli/getting-started/)

---

## 4. 为 agy 授权

### 方式 A：Google 账号登录

在 `agy-tg` 用户下运行：

```bash
agy
```

远程 SSH 环境会显示授权链接。用自己的电脑或手机浏览器打开链接，完成 Google 登录后回到 SSH 等待成功提示。

### 方式 B：Gemini API Key

适合没有图形界面的服务器。先创建 Gemini API Key，再创建配置：

```bash
mkdir -p ~/.gemini/antigravity-cli
nano ~/.gemini/antigravity-cli/settings.json
```

写入：

```json
{
  "modelProvider": "gemini"
}
```

之后将 `GEMINI_API_KEY` 写入项目 `.env`，并确保该文件仅专用账户可读。

---

## 5. 先独立验证 agy

在接入 Telegram 前，先确认 agy 本身工作正常：

```bash
mkdir -p /srv/agy-workspace/demo
cd /srv/agy-workspace/demo
git init
printf '# AGY Demo\n' > README.md
agy
```

在 agy 中输入：

```text
阅读当前目录的 README.md，告诉我它写了什么。不要修改文件。
```

能正常回答才继续下一步。

---

## 6. 安装本项目

```bash
cd /home/agy-tg
git clone https://github.com/你的用户名/agy-telegram-remote.git
cd agy-telegram-remote

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 7. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

最小配置：

```env
# 来自 BotFather
TELEGRAM_BOT_TOKEN=替换成你的BotToken

# 仅允许你自己；多个用户可用英文逗号分隔
ALLOWED_USER_IDS=123456789

# 使用绝对路径，不依赖 systemd 的 PATH
AGY_PATH=/home/agy-tg/.local/bin/agy

# agy 只在该目录及其项目中工作
AGY_WORKSPACE=/srv/agy-workspace

# 单项任务最长 15 分钟
AGY_TIMEOUT_SECONDS=900

# 默认保留 agy 权限确认；不要默认跳过
AGY_SKIP_PERMISSIONS=false
```

如果采用 Gemini API Key 方式，再加入：

```env
GEMINI_API_KEY=替换成你的GeminiAPIKey
```

`.env` 不得上传。创建 `.gitignore` 后执行：

```bash
git status --short
```

输出中不应出现 `.env`。

---

## 8. 获取 Telegram 数字 ID

首次前台启动后，私聊机器人发送：

```text
/id
```

将返回的纯数字填入 `ALLOWED_USER_IDS`，然后重启服务。这个数字不是用户名，也不是手机号。

---

## 9. 前台测试

```bash
cd /home/agy-tg/agy-telegram-remote
source venv/bin/activate
python bot.py
```

在 Telegram 私聊 Bot，按顺序发送：

```text
/start
```

```text
请列出当前工作目录里的文件名。不要修改任何文件。
```

成功时会依次看到任务已接收、agy 运行进度和最终结果。按 `Ctrl + C` 停止前台测试。

---

## 10. 设置开机自启

```bash
sudo cp deploy/agy-telegram-remote.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agy-telegram-remote
sudo systemctl status agy-telegram-remote
```

查看实时日志：

```bash
sudo journalctl -u agy-telegram-remote -f
```

修改 `.env` 或程序后：

```bash
sudo systemctl restart agy-telegram-remote
```

---

## Telegram 使用方法

直接私聊发送任务即可。

### 只读任务示例

```text
查看这个项目有哪些 Python 文件，并说明每个文件的用途。不要修改任何内容。
```

```text
运行测试，告诉我失败原因。先不要修改代码。
```

### 修改任务示例

```text
检查测试失败原因。先给出修改计划，等我确认后再改代码。
```

```text
将 README 的安装部分改成中文。完成后列出修改了哪些文件。
```

### 控制命令

| 命令 | 用途 |
|---|---|
| `/start` | 查看说明 |
| `/help` | 查看简短帮助 |
| `/id` | 显示自己的 Telegram 数字 ID |
| `/status` | 查看当前任务状态 |
| `/cancel` | 中止正在执行的任务 |
| `/reset` | 清除当前 agy 会话 |

---

## 安全说明：务必阅读

agy 可能读取、修改项目文件并执行命令。Telegram 只是远程入口，不会降低这种风险。

推荐默认组合：

```text
专用 Linux 用户
+ 专用工作目录
+ Telegram 私聊白名单
+ 非 root 运行
+ agy 默认权限确认
```

不要：

- 以 root 运行 Bot。
- 让 Bot 接收群聊或陌生用户消息。
- 把 `AGY_WORKSPACE` 设置为 `/`、`/etc` 或整个 `/home`。
- 将 SSH 私钥、密码、生产密钥放入工作目录。
- 默认开启跳过 agy 权限确认。
- 将 `.env`、日志、会话数据或 agy 登录凭据提交到 GitHub。

如果你未来选择无人值守模式，必须先确认白名单、专用账户和工作目录隔离已经完成。

---

## 常见问题

### `agy: command not found`

```bash
ls -l /home/agy-tg/.local/bin/agy
```

将 `.env` 中的 `AGY_PATH` 改为该绝对路径，然后重启服务。

### Bot 能回复，但 agy 执行失败

```bash
sudo systemctl status agy-telegram-remote
sudo journalctl -u agy-telegram-remote -n 100 --no-pager
sudo -iu agy-tg
cd /srv/agy-workspace
/home/agy-tg/.local/bin/agy
```

最后一条如果也失败，问题在 agy 的安装或授权，不在 Telegram。

### Bot 提示未授权

确认你正在私聊 Bot，且 `.env` 中的 `ALLOWED_USER_IDS` 是正确的 Telegram 数字 ID。修改后重启服务。

### 任务太久没有结束

先在 Telegram 发送：

```text
/cancel
```

然后用 `journalctl` 查看原因。新手建议先保持 `AGY_TIMEOUT_SECONDS=900`。

---

## 公开版项目结构

```text
agy-telegram-remote/
├── bot.py                         # Telegram 私聊入口、白名单和控制命令
├── agy_runner.py                  # 用参数数组启动、读取和终止 agy
├── session_store.py               # 用户会话 ID 持久化
├── config.py                      # 配置检查与安全默认值
├── requirements.txt
├── .env.example                   # 无任何真实凭据
├── .gitignore                     # 排除 .env、日志、会话和缓存
├── deploy/
│   └── agy-telegram-remote.service
├── LICENSE
└── README.md
```

不会包含 Bot Token、Google 或 Gemini Key、服务器 IP、SSH 密码/私钥、聊天记录、现有项目代码、日志、缓存、数据库或 agy 登录凭据。
