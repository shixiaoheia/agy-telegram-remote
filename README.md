> **TG 频道：[Xiaohei的秘密基地](https://t.me/xiaoheidemimi)**
>
> 用于发布版本更新、使用技巧与公告。欢迎各位大佬进群交流 → [点击加入 Xiaohei的秘密基地](https://t.me/xiaoheidemimi)

# Antigravity Telegram Remote | Antigravity Telegram 远程控制工具

把 Telegram 私聊作为你自己服务器上 Google Antigravity CLI（agy）的远程入口：发送任务，服务器在专用工作目录执行，再把结果返回私聊。

> **使用前说明**
>
> - 仅部署在你拥有或已获明确授权的服务器、Telegram Bot 与工作目录中。
> - Bot Token、Google 登录凭据与工作目录内容由你自己保管；不要发送给他人，也不要提交到 GitHub。
> - 此项目不监听群聊。只有配置在白名单内的数字 Telegram ID 才能提交任务。
> - agy 可读写工作目录、调用工具；是否允许自动批准工具权限由安装器单独询问。
> - 社区维护，与 Google、Telegram 没有官方关联。

> **推荐服务器（推广链接）：[搬瓦工](https://bandwagonhost.com/aff.php?aff=80815)**  
> 还没有 Linux 服务器？👉 **[点这里前往搬瓦工](https://bandwagonhost.com/aff.php?aff=80815)**，再按自己的地区、预算和线路需求选择。

## 它是怎么工作的

~~~text
你在 Telegram 私聊发送任务
          ↓
Bot 检查是否为私聊、是否在数字 ID 白名单中
          ↓
受限账户 agy-tg 在专用工作目录启动 agy
          ↓
agy 输出结果；程序将结果发回 Telegram 私聊
~~~

Telegram 只是远程控制入口，真正执行任务的是服务器上的 agy。每条 Telegram 任务都会启动一次独立的 agy headless 运行；默认不保留上下文会话。

## 部署前准备

| 需要准备 | 说明 |
|---|---|
| Debian 12 或 Ubuntu 22.04+ 服务器 | 可用 root 或拥有 sudo 权限的普通用户登录 |
| Telegram Bot Token | 在 Telegram 的 @BotFather 创建 Bot 后取得 |
| 自己的 Telegram 数字 ID | 纯数字，不是 @用户名、手机号或群 ID |
| Google 账号 | 用于在服务器上完成一次 agy 登录授权 |
| 一台自己的电脑或手机浏览器 | 用来打开 agy 在 SSH 中显示的一次性 Google 授权链接 |

安装前请先准备好自己的 Telegram 数字 ID。你可以使用自己信任的 Telegram ID 查询方式取得；整个过程中只需要填入数字 ID，**绝不要把 Bot Token 发给任何查询机器人或他人。**

本项目使用 Telegram 长轮询，不需要域名、Nginx、Webhook，也不需要为 Bot 额外开放 HTTP 端口。

## 从 0 开始一键安装

先 SSH 登录服务器。root 和普通 sudo 用户都可以运行下面命令：

~~~bash
curl -fsSLO https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh && bash install.sh
~~~

这条命令会先下载脚本；下载失败时不会假装安装成功。

### 先选择操作

启动脚本后输入 `1` 进入安装或更新；输入 `2` 会进入卸载流程。首次部署请选择 `1`。

### 选择安装后，安装器会逐步询问

1. Telegram Bot Token（输入时不显示）
2. 允许使用工具的 Telegram 数字 ID
3. 专用工作目录  
   直接回车默认使用 /srv/agy-workspace；为避免误改系统目录，只允许该目录或其子目录。
4. agy 任务权限模式  
   默认安全模式；只有输入 YES 才会让 agy 自动批准命令、文件等所有工具权限。
5. 是否按 Google 官方方式安装 agy
6. Google OAuth 授权

安装器会自动完成：

- 安装 Git、Python、虚拟环境和项目依赖
- 创建不具备 sudo 权限的受限账户 agy-tg
- 在 /opt/agy-telegram-remote 部署项目
- 检查 Python 语法和 Bot Token 是否真实可用
- 配置并启动 systemd 服务
- 检查服务是否真的处于运行状态；失败时直接显示最近日志

不需要自行编译 agy 或 Python 项目。

## Google OAuth：在 SSH 中完成

第 6 步会在**当前 SSH 终端**启动 agy。Google 的远程 SSH 登录流程会显示一次性授权链接：

1. 复制 SSH 终端显示的授权链接。
2. 在你自己的电脑或手机浏览器打开链接，登录 Google 账号并允许授权。
3. 浏览器会显示一次性授权码。
4. 将授权码粘贴回同一个 SSH 终端中 agy 的提示处。
5. 授权完成后退出 agy，回到安装器输入 YES。
6. 安装器会自动发送一条只读测试，确认 agy 登录确实可用，然后才启动 Telegram 服务。

不要把授权链接或授权码发给别人，也不要把它们写到 GitHub。

Google 官方参考：

- [安装与 SSH 授权流程](https://antigravity.google/docs/cli/install/)
- [headless 模式与输出格式](https://antigravity.google/docs/cli/headless/)

## 安装后先验证

安装完成后，先看服务状态：

~~~bash
sudo systemctl status agy-telegram-remote
~~~

显示 active (running) 后，私聊你的 Bot，发送：

~~~text
/start
~~~

然后发送一个只读任务，例如：

~~~text
列出当前工作目录中的文件名，并说明每个文件的用途。不要修改任何文件，也不要执行网络请求。
~~~

成功时会先看到“任务已接收”，完成后收到结果。

若需要查看日志：

~~~bash
sudo journalctl -u agy-telegram-remote -f
~~~

## Telegram 使用方法

只在私聊中使用。支持的控制命令：

| 命令 | 作用 |
|---|---|
| /start 或 /help | 显示简短帮助 |
| /id | 显示自己的 Telegram 数字 ID；此命令仅限私聊 |
| /status | 查看自己的任务是否运行中 |
| /cancel | 请求停止自己的当前任务 |

直接发送普通文字就是任务。例如：

~~~text
查看当前项目的 README，列出需要改进的地方。先不要修改文件。
~~~

~~~text
运行项目测试，告诉我失败原因和建议。先不要改代码。
~~~

同一个共享工作目录一次只允许运行一个 agy 任务：同一用户重复发送会被拒绝，其他白名单用户会收到“工作目录忙碌”提示，从而避免同时读写冲突。运行期间发送 /cancel 会停止 agy 及其任务进程组。服务重启时会丢弃积压消息，避免旧任务在你不知情时重新执行。

为避免某条任务输出过大拖垮服务或刷屏，程序最多保留 agy 输出的最后 1 MiB，并最多回传 30,000 个字符；超过部分会明确提示已截断。

## 权限模式：为什么有时命令没有执行

agy 的 headless 模式无法弹出交互式确认。默认权限策略会允许工作目录内的常见文件操作，但可能拒绝 shell 命令等需要确认的工具；这种拒绝可能仍以成功退出，只在诊断信息中提示。

安装器默认将以下配置设为 false：

~~~dotenv
AGY_SKIP_PERMISSIONS=false
~~~

这是推荐起点。如果你已经确认：

- Bot 只有自己或完全信任的人可以私聊；
- 工作目录是专用目录，没有敏感文件；
- agy 使用的是受限账户 agy-tg；
- 你明确需要让 agy 自动执行命令或修改文件；

才可编辑配置：

~~~bash
sudo nano /opt/agy-telegram-remote/.env
~~~

将它改为：

~~~dotenv
AGY_SKIP_PERMISSIONS=true
~~~

然后重启服务：

~~~bash
sudo systemctl restart agy-telegram-remote
~~~

开启后，白名单用户发来的任务会携带 agy 的自动批准权限参数。它不是全服务器 root 权限，但仍可能读写 agy-tg 可访问的内容、修改工作目录并执行命令，所以请谨慎使用。

## 常见问题排查

### 一开始就提示 root 或 sudo 相关错误

新版安装器已同时支持 root 和普通 sudo 用户：

- 你用 root 登录：直接运行安装命令即可。
- 你用普通用户登录：该用户需要可用的 sudo 权限。

服务本身和 agy 不会以 root 运行。

### 提示 agy 未找到或授权测试失败

先确认二进制是否存在：

~~~bash
sudo -u agy-tg -H /home/agy-tg/.local/bin/agy --version
~~~

再以受限账户手动进入工作目录测试：

~~~bash
sudo -u agy-tg -H bash -lc 'cd /srv/agy-workspace && /home/agy-tg/.local/bin/agy'
~~~

如果这里也无法完成登录，问题在 agy 安装或 Google 授权；重新运行安装器即可再次走授权流程。

### Bot 不回复，或服务没有 active (running)

~~~bash
sudo systemctl status agy-telegram-remote --no-pager
sudo journalctl -u agy-telegram-remote -n 100 --no-pager
~~~

重点检查：

- Bot Token 是否来自正确的 BotFather Bot；
- 数字 Telegram ID 是否填对；
- /opt/agy-telegram-remote/.env 是否仍是 agy-tg 所有；
- agy 路径是否为 /home/agy-tg/.local/bin/agy。

### Bot 回复任务完成，但 agy 没有执行命令

检查 Telegram 返回信息是否提到权限策略拒绝。默认模式下这是预期保护。确认白名单和专用目录安全后，可按上面的“权限模式”章节将 AGY_SKIP_PERMISSIONS 改为 true 并重启服务。

### 想更新项目

重新运行一键安装命令，菜单选择 `1` 即可。脚本会使用 Git 快进更新；如果你手动修改过 /opt/agy-telegram-remote 中的项目文件，先备份或提交你的改动，再更新。

## 一键卸载

不再使用时，在 SSH 终端重新运行同一条命令：

~~~bash
curl -fsSLO https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh && bash install.sh
~~~

在菜单输入 `2`，再按提示输入 `UNINSTALL`。默认卸载会停止并删除：

- systemd 服务 `agy-telegram-remote`
- 程序目录 `/opt/agy-telegram-remote`（包括其中的 Bot Token 配置）

默认**保留** Telegram Bot、agy、Google 登录状态、受限账户 `agy-tg` 与工作目录 `/srv/agy-workspace`，方便以后重新安装；卸载前请自行备份需要的项目文件。

只有第二次明确输入 `PURGE` 才会彻底删除 `/srv/agy-workspace`、`agy-tg` 账户及其 home 中可能保存的 agy / Google 登录状态。脚本会先检查该账户是否仍有进程，有残留时不会强行删除。无论哪种卸载方式，都不会删除 BotFather 中的 Telegram Bot 本身。

## 项目结构

~~~text
agy-telegram-remote/
├── .env.example
├── .gitignore
├── README.md
├── bot.py
├── install.sh
└── requirements.txt
~~~

真实 Token、Google 登录凭据、服务器地址、私钥、日志和工作目录内容都不应提交到仓库。

## 致谢

本项目参考并改造自 [whypuss/agy-telegram-bot](https://github.com/whypuss/agy-telegram-bot)。

感谢原项目作者 [@whypuss](https://github.com/whypuss) 的开源分享与贡献。

---

> **推荐服务器（推广链接）：[搬瓦工](https://bandwagonhost.com/aff.php?aff=80815)**  
> 👉 **[点这里前往搬瓦工，选择适合你的服务器](https://bandwagonhost.com/aff.php?aff=80815)**

> **TG 社区：欢迎各位大佬进群交流 → [点击加入 Xiaohei的秘密基地](https://t.me/xiaoheidemimi)**
