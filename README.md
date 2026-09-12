> **TG 频道：[Xiaohei的秘密基地](https://t.me/xiaoheidemimi)**
>
> 发布版本更新、使用技巧与公告。[进入社区交流](https://t.me/xiaoheidemimi)

# Antigravity Telegram Remote

把 Telegram 私聊作为自己服务器上 Google Antigravity CLI（agy）的远程入口：发送任务，在固定工作目录运行，再把最终结果发回私聊。

**三个安装阶段：Bot Token → Telegram 数字 ID → Google 授权。** 自动创建工作目录、安装依赖和 agy、设置运行账户。全新安装默认自动审批；普通更新尊重旧配置。

社区项目，与 Google、Telegram 没有官方关联。只部署到你拥有或获授权的服务器、Bot 和工作目录。

> **推荐服务器（推广链接）：[搬瓦工](https://bandwagonhost.com/aff.php?aff=80815)**

## 安装前必须知道

自动审批意味着白名单用户可让 agy 执行命令、修改文件。非 root 账户、专用目录和 systemd 加固**不是完整沙箱**，也不是对恶意白名单用户的凭据隔离。仅允许自己或完全信任的人使用，建议使用专用 VPS。详见 [SECURITY.md](SECURITY.md)。

程序不接收群聊任务；没有 Web 后台、数据库、Redis 或额外入站端口。Python 部分只依赖 Python 3.10+ 标准库，不需要 `pip`、虚拟环境、`python-telegram-bot` 或 `python-dotenv`。Google agy 二进制是单独安装的运行依赖，不包含在仓库里。

## 支持环境

Debian 12 及以上，或 Ubuntu 22.04 及以上；必须有可用的 systemd、root 或 sudo 权限，以及能连接 GitHub、Google 与 Telegram 的网络。不支持 Alpine、无 systemd 的普通容器，也不需要域名或 Nginx。

准备自己的 BotFather Token、自己的 Telegram 数字用户 ID、Google 账号和可打开授权链接的浏览器。不要把 Token、授权链接、授权码、服务器私钥或真实配置上传到 GitHub。

## 三步安装

在可交互的 SSH 终端执行，**先下载，再用 bash 运行**：

```bash
curl -fsSLO https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh
bash install.sh
```

不要使用 `curl ... | bash`：本项目向导需要终端输入。安装脚本会读取 GitHub 上当前 `main` 并显示使用的精确提交号；先审阅代码再以特权账户运行。为固定已审阅的版本，可使用 `bash install.sh --ref 完整提交SHA`。

界面示意：

```text
=============================================
 Antigravity Telegram Remote 极简一键安装向导
=============================================

步骤 1/3：请输入 Telegram Bot Token：
> 输入隐藏

步骤 2/3：请输入 Telegram 数字 ID：
>

自动准备运行账户、/srv/agy-workspace、依赖与 agy。

步骤 3/3：Google 账号授权
已有有效授权自动复用。
否则打开授权链接 → 登录 → 粘贴授权码。
进入 agy 主界面后输入 /exit 返回安装器。

验证 agy 回复、Telegram 接口及服务初始化……

🎉 安装成功！服务已启动。
```

这是三个安装阶段，不是承诺 Google 或 agy 自身永远只有三次输入。首次授权可能出现 agy 自身的条款或信任提示。安装器没有虚构“只登录后立即退出”的命令，也不自动粘贴授权码。

安装器会自动执行以下工作：

- 新建不带 sudo/额外组权限的 `agy-tg` 用户；创建默认工作目录并检查路径。
- 把**程序代码放在 root 所有的发布目录**，使用系统 `/usr/bin/python3`，不再执行旧用户可写虚拟环境中的解释器。
- 创建候选配置，运行离线测试和 Telegram 检查，再停止旧服务，授权并执行 agy 自检。
- 自检与普通任务共用 `agy_runner.py`，统一 `--output-format json`、账户、HOME、工作目录及权限参数；自检超时单独设为 90 秒。
- 备份旧配置、旧 unit 和旧程序入口，切换发布；必须通过实际 Bot 初始化检查才显示成功。切换失败会尝试恢复旧入口/配置/unit。

首次安装中途取消可能已安装系统依赖或创建账户；不会谎称所有系统改动都撤销。更新会停止旧服务中的任务，先用 `/status` 检查。不要一边手工运行 agy，一边更新。

## 安装后验证

```bash
sudo systemctl status agy-telegram-remote --no-pager
sudo journalctl -u agy-telegram-remote -n 80 --no-pager
```

私聊 Bot，依次发送：

```text
/start
```

```text
只回复“连接成功”，不要使用工具或修改任何文件。
```

再用一个你可核对的只读任务检查实际工作目录和工具能力。最后发送 `/last`，应只取回同一个任务编号的结果，不启动新任务。

**安装成功不等于任意未来任务都一定成功。** 网络、配额、授权、模型行为和上游协议仍可能变化；详见 [测试及真实环境验收](docs/TESTING.md)。

## 使用方法

| 命令 | 行为 |
|---|---|
| 普通文字 | 启动一次独立 agy 任务；不自动继承上一条的对话上下文 |
| `/status` | 查看自己的任务与工作目录占用情况 |
| `/cancel` | 请求取消自己的任务并回收该任务的进程组 |
| `/last` | 取回本人最近的结果，不执行 agy |
| `/id` | 在私聊显示自己的数字 ID |
| `/start`、`/help` | 显示帮助 |

共享工作目录同一时间只有一个任务，不排队。开始执行前先保存任务记录并尝试发送确认；确认发送失败就不启动 agy。完成后先保存结果，再回传 Telegram。只有明确的 Telegram `429` 拒绝会做有限的**消息投递重试**，不会因此再次执行任务。

每个白名单用户最多保存一份最近结果，默认保存 7 天；启动、提交任务和运行中定期清理过期结果。`/last` 的界面过滤不是不同白名单用户之间的操作系统隔离——所有用户必须互信。

服务重启会丢弃此前积压的 Telegram 更新，不自动重新执行中断任务。更新水位在分派前写入磁盘，以避免消息重新投递造成重复操作；崩溃窗口内可能丢掉一条任务，因此**不是 exactly-once 或可靠任务队列**。不要把它用于必须精确一次执行的支付、删除生产数据等高风险流程。

## “没有最终回复”现在如何处理

只有“正常退出 + 完整 JSON 对象 + `status=SUCCESS` + 非空文本 `response`”才作为正常回答处理。空回复、异常状态、缺少字段、损坏 JSON、输出超限、取消及超时分别处理。

`SUCCESS` 但文本为空时，提示“缺少最终回复，需要核对”，不会宣称业务任务已经完成，也不会要求直接重发原任务。模型可能已经修改了文件，先检查 `/last` 和工作目录。

不会把 JSON 碎片、中途进度或未经识别的结构化输出当作最终答案，也不会通过换参数再执行任务来“补救”。stderr 只用于有限的错误分类，不把原始敏感诊断发到聊天或写进普通日志。配额/认证/网络分类是提示性判断，不是完整的上游错误协议。

## 配置和目录

| 路径 | 用途 |
|---|---|
| `/etc/agy-telegram-remote/config.env` | 真实配置，`root:agy-tg`，`0640`；不要上传 |
| `/opt/agy-telegram-remote` | 当前 root 管理的程序发布入口 |
| `/opt/agy-telegram-remote-releases/` | root 所有的程序发布目录 |
| `/home/agy-tg` | agy 安装、缓存与 Google 登录；不要上传 |
| `/srv/agy-workspace` | 默认工作目录，或升级时保留的原有子目录 |
| `/var/lib/agy-telegram-remote` | 私有任务记录及更新水位 |
| `/run/agy-telegram-remote/ready.json` | 当前服务初始化就绪标志 |
| `/var/backups/agy-telegram-remote/` | root 私有的升级/回退备份，可能含旧凭据；不要上传 |

支持的配置项见 [.env.example](.env.example)。只解析有限 dotenv 语法，不执行 `.env`，不展开 `$变量` 或命令替换。部署目录不接受空格、`%`、路径穿越等特殊形式；不支持的旧配置会明确报错，不静默丢弃。

默认任务超时 900 秒。stdout 最多保留 1 MiB，stderr 最多 256 KiB；任一超限即停止该任务并报告超限，不解析截断 JSON。默认最多保存/回传 30,000 个正文字符；超出会明确标记，`/last` 也只能取回已保存的截断副本。**不存在一份自动保存的无限量“完整日志”。**

修改配置后重启服务：

```bash
sudoedit /etc/agy-telegram-remote/config.env
sudo systemctl restart agy-telegram-remote
```

取消/超时不撤销已发生的文件修改。程序不支持通过任务长期留存后台守护进程；详见安全文档中的进程组边界。

## 更新和从旧版迁移

重新下载安装器后运行：

```bash
bash install.sh
```

第 1、2 步直接回车可保留 Token 和原白名单。普通升级保留工作目录、限额、权限开关与登录状态；不会把原本 `false` 的自动审批选项偷偷改成 `true`。旧配置没有该字段时按旧版的安全默认 `false` 保留。

你明确要把旧安装也切换为自动审批时使用：

```bash
bash install.sh --enable-auto-approve
```

需要重新进入 Google 授权时使用：

```bash
bash install.sh --reauth
```

更多迁移、回退及备份说明见 [docs/MIGRATION.md](docs/MIGRATION.md)。

## 卸载

默认只移除服务，**保留程序和全部数据**，不再显示开头的安装/卸载菜单：

```bash
bash install.sh --uninstall
```

输入 `UNINSTALL` 确认。彻底清理必须显式传入 `--purge` 并再输入 `PURGE`：

```bash
bash install.sh --uninstall --purge
```

彻底清理会删除程序、配置、结果、工作目录、备份和 `agy-tg` 账户及 home；运行账户仍有进程时拒绝清理。不会删除 BotFather 中的 Bot。本版本的默认卸载比旧版更保守，保留程序是有意行为。

## 常见问题

**Bot Token 检查失败**：核对 Token 与网络；不要把 Token 发到工单或公开群。

**提示已有 webhook**：本项目使用长轮询。请先确认该 Bot 没被其他系统占用，再人工移除 webhook 或换专用 Bot；安装器不会擅自删除别处的 webhook。

**Telegram 409**：通常要检查是否还有另一个轮询实例。项目防止同一状态目录的重复本地实例，但无法阻止另一台服务器用同一个 Token。

**运行目录不属于 agy-tg 或包含链接**：安装器不会递归更改未知目录所有权。先备份、核对现有目录，不要盲目执行递归 `chown`。

**新任务被暂停**：进程清理或结果持久化未确认完成。先检查日志、磁盘空间和权限，再重启服务；不要直接重复原任务。

**服务初始化失败**：查看 `journalctl`。授权、配额和网络是不同问题。仅在确实需要重新授权时使用 `--reauth`。

## 开发与测试

```bash
bash scripts/verify.sh
```

没有运行时 pip 依赖。离线测试使用伪 agy 可执行文件和回环地址上的模拟 Telegram HTTP 服务，不需要真实 Token 或 Google 凭据。测试中出现的 Token 是构造的假值。

测试覆盖、已知边界与验收步骤见 [docs/TESTING.md](docs/TESTING.md)。GitHub Actions 配置位于 `.github/workflows/tests.yml`；创建了配置不等于 CI 已经在远程执行通过。

## 官方参考与致谢

- [Google agy 安装与 SSH 授权](https://antigravity.google/docs/cli/install/)
- [Google agy headless、JSON 和权限参数](https://antigravity.google/docs/cli/headless/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

本项目最初参考并改造自 [whypuss/agy-telegram-bot](https://github.com/whypuss/agy-telegram-bot)，感谢原作者 [@whypuss](https://github.com/whypuss)。

> **推荐服务器（推广链接）：[搬瓦工](https://bandwagonhost.com/aff.php?aff=80815)**
> **TG 社区：[Xiaohei的秘密基地](https://t.me/xiaoheidemimi)**
