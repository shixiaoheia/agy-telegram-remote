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
- 任务运行时看到安全的执行摘要，默认每 5 秒更新；展开阅读时保持内容不动，也可以随时点击“取消任务”。
- 任务较长时，Telegram 会持续显示“正在输入”；需要临时改方向可用 `/steer` 纠正。
- 发图片、日志或代码文件给机器人分析，单个文件最大 10 MB；同一组最多 10 个附件、合计 30 MB。
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
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh && bash install.sh
```

脚本会依次询问：

1. Telegram Bot Token；
2. 允许使用机器人的 Telegram 数字 ID；
3. Google `agy` 授权（仅首次或授权失效时需要）。

菜单会根据是否检测到已有安装推荐安装或更新。首次安装选择 `1`；输入 Token 前可选择是否隐藏，直接回车默认隐藏，选择显示时内容会出现在终端中。Token 与数字 ID 格式不正确时可直接重新输入。格式正确不代表 Token 已通过 Telegram 联网验证。看到“安装成功”后，打开机器人私聊，发送 `/start`，再发送 `/model` 选一个模型就可以开始了。

安装或更新失败时，脚本会提示排查方向、是否已经尝试回退及当前能查询到的服务状态；如果状态无法确认，请使用提示中的状态与日志命令检查。

## 日常使用

直接在 Telegram 私聊机器人发送需求，例如：

```text
检查当前项目的报错，说明原因并修复。
```

任务开始后会有一张状态卡：

- 顶部引用块回显简短提问与附件名称，正文显示阶段、模型和耗时；
- 执行过程默认显示最近一步，点击“展开过程”查看最近六步安全摘要；
- 不显示模型的内部推理、Token、密码或文件内容；
- 点击“🛑 取消任务”可请求停止当前任务。

展开过程后进入阅读状态：当前内容不再被定时刷新或新步骤改写，后台任务照常执行。点“🔄 刷新过程”查看最新进度并保持展开；点“📥 收起过程”回到简洁视图并恢复自动更新。两个按钮与底部取消入口位置固定，任务结束后仍在原气泡显示结果。

如果任务还在运行，但你想补充或改变要求，发送：

```text
/steer 改为只检查登录模块，不要修改任何文件
```

机器人会先安全停止旧任务，完成进程清理后，自动用“原任务 + 最新修正”重新执行。含附件的任务为避免误用已清理的临时文件，请取消后重新发送附件和完整要求。

也可以在本人的纯文字任务运行时直接发送补充，再点击“作为修正”确认，或点“忽略补充”继续原任务。未确认时原任务继续执行；仅最新一条补充有效，5 分钟后失效，任务结束或进入结果保存、发送阶段后不能使用旧按钮。修正不会撤销旧任务已经发生的修改。

连续补充会更新同一张确认卡片；忽略、过期、任务结束或提交修正后，卡片显示对应说明并收起按钮。

需要排队执行另一条文字任务时，发送 `/queue add <任务内容>`；其他白名单用户在工作目录忙碌时发送的文字任务也会进入队列。最多等待 3 个任务，前一项结束并清理后按顺序执行。发送 `/queue` 查看自己的任务并用按钮逐条取消，`/queue cancel` 取消自己的全部等待任务；运行中的任务仍用 `/cancel`。队首启动失败或服务重启后队列暂停，队首发起者检查后可发送 `/queue resume` 恢复，或者取消等待。重启前已保存的每个等待任务都需要其发起者在到达队首后单独确认。等待任务使用开始时的模型和会话设置，附件暂不支持排队。

等待任务正文会以 `0600` 保存在状态目录中；重启时超过结果保留期限的队列文件会被删除。确认不希望任务正文落盘时不要使用队列。为避免重复执行，任务启动前先从等待记录中移除：如果此时发生崩溃，队首任务可能丢失，需要用户检查实际情况后自行决定是否重发。Telegram 更新水位保存与入队之间的崩溃窗口也可能丢失新请求，无法保证恰好执行一次。结果正常保存失败时会尝试在服务账户主目录 `.agy-telegram-recovery` 中保存私有恢复副本；修复存储后发送 `/last`。如果两个位置都不可写，结果只保留在内存，重启会丢失。恢复目录与状态目录可能位于同一磁盘，磁盘满时无法保证备用副本成功；完全卸载后如不再需要，应在核对内容后手动删除该恢复目录。

发送 `/status` 可查看当前任务、阶段、已用时间、模型与部署版本；点击“刷新状态”更新原卡片，“详细信息”查看路径、权限和服务启动时间，也可直接发送 `/status details`。

安装器的 `check-ready` 用于启动阶段；长期运行时可使用以下检查确认当前进程最近一次成功轮询不超过 90 秒。单次检查失败可能是网络抖动、状态文件不可写或服务问题，不会自动重启服务：

```bash
pid=$(systemctl show agy-telegram-remote -p MainPID --value)
/usr/bin/python3 /opt/agy-telegram-remote/manage.py check-health --file /run/agy-telegram-remote/ready.json --pid "$pid"
```

任务结束后按“提问引用块 → Markdown 正文 → 统计引用块”展示。底部例如 `🤖 Gemini 3.8 Flash (Low) · ⏱ 43s`，下一行显示输入、输出和总 Token；避免顶部再次重复模型和统计。缺失的指标显示未提供，背景色和引用外观跟随 Telegram 客户端主题。

回复和历史保留原始 Markdown；发送到 Telegram 时转换为其支持的格式，显示标题、加粗/斜体、代码、原生引用、链接和列表。代码内容不会被当作表格或图示再次转换，长回复分页会保留完整的格式标签和转义字符。普通消息无法完整表达的表格会转为逐项字段，复杂或未完整闭合的语法保持可读文字；格式被明确拒绝时备用文本仍保留链接地址。这里不是完整的网页 Markdown 渲染器。

短回复直接替换原进度气泡，长回复继续分页，只有最后一页提供“查看结果”“最近任务”“新建对话”按钮。“查看结果”取回该次任务的已保存记录，不重新执行；旧按钮仍定位原任务。若结果发送失败或不完整，会提示已保存的结果及按钮和 `/last` 取回入口。后续页失败不会覆盖已经显示的首屏；网络结果不明时不重发答案。执行状态与发送状态分别记录，发送成功不代表任务执行成功。

如果服务器存储故障导致结果无法保存，机器人会暂时在内存中保留限长的结果，并提示先修复存储。之后发送 `/last`，系统先保存再发送，不重新运行任务；临时结果在服务重启后会丢失。该用户的新任务会等结果保存成功，其他用户不因此被全局暂停。

连续点击“查看结果”或发送 `/last` 时，同一结果发送期间及成功后的两秒内只发送一份，稍后仍能再次手动取回。“新建对话”按钮先显示“确认新建 / 保留当前对话”，90 秒内确认才清空记忆；若确认前对话已发生变化，需要重新确认。历史结果和累计用量保留，运行中确认会安排在当前任务结束后重置。直接发送 `/new` 或 `/reset` 仍按原规则执行。

`/usage` 的累计统计使用原生引用卡片；状态详情、帮助、文件列表和白名单统一标题与代码样式，路径、ID 和命令参数安全转义。

### 图片与文件

可以直接发送：截图、`.log` 日志、`.txt`、`.md`、`.json`、`.py`、`.js`、`.sh` 等常见文本和代码文件。
在 Telegram 中选中多张截图或多个文件作为同一组发送，机器人会收齐后用一次任务分析。每个附件最大 10 MB，一组最多 10 个、合计 30 MB。接收期间可发送 `/cancel` 放弃整组。

机器人会先确认，例如：

```text
已收到：日志文件「error.log」，1.2 MB。本次任务会读取它。
```

附件只会保存在当前任务的私有临时目录，任务结束后自动删除。
中文文件名会保留在接收提示和任务说明中，磁盘上仍使用安全文件名；显示名称会去除路径与控制字符。

任务运行中发送 `/new` 或 `/reset` 时，会在当前任务结束后清空对话记忆；下一条消息才会开启全新对话。

`/restart` 仅主管理员可用，需要等待任务结束或取消后的清理完成，附件接收、模型检测和修正任务交接期间也不能重启。清理失败导致服务暂停时，空闲后仍可使用此命令；服务以失败状态退出，由 systemd 清理服务进程组并重新启动。正在重启时暂不接收新任务，已保存的等待队列重启后暂停，等待队首发起者确认恢复。自定义 unit 需要保留 `Restart=on-failure` 和 `KillMode=control-group`。

任务内容通过标准输入交给 agy，不再写入启动 agy 的命令行参数。需要支持 stream-json 输入的 agy 1.1.15 或更新版本；旧版本不会回退为明文参数。

任务历史仍保留最近 10 条，并遵守原有单条回复保存上限；历史读取容量已覆盖多条长中文、Emoji 及 JSON 转义字符，旧版生成的合法大历史文件可以继续读取。

历史列表显示任务时间、模型简称和投递异常标记，方便区分同名任务；时间按列表注明的服务器本地时区显示。点击历史按钮仍只取回对应记录，不执行原任务。

## Telegram 指令

| 指令 | 用途 |
| --- | --- |
| `/start` | 查看欢迎页和快速开始说明 |
| `/model` | 点击选择模型，再选择思考强度 |
| `/model refresh` | 读取服务器 CLI 模型目录，检测账号可用性并更新菜单 |
| `/mode` | 选择执行模式 |
| `/new` 或 `/reset` | 清空当前对话记忆，开始新会话 |
| `/status` | 查看任务概览，点击详情或刷新；`/status details` 查看完整信息 |
| `/cancel` | 取消当前任务 |
| `/steer <修正内容>` | 停止当前纯文字任务，并按原任务加修正重新执行 |
| `/history` | 查看最近 10 条任务并打开完整结果 |
| `/last` | 重新查看最近一次任务结果 |
| `/usage` | 查看当前会话信息与历史累计 Token 统计 |
| `/help` | 查看完整指令说明 |

## 执行与恢复

- `/steer` 在旧任务清理和修正任务接手期间持续占用工作目录；新文本、相册和模型检测需要等待。交接期间发送 `/cancel` 会取消修正任务。
- 已记录工具活动的任务不会因容量错误重新提交原请求。取消、错误或中断不代表已经发生的修改被撤销。
- 执行结果先保存，再更新会话与用量；统计失败会单独提示。会话保存异常时暂停新任务，已保存结果仍可通过 `/last` 或历史取回。
- 长回复发送中出现格式拒绝时，只降级该页，不重新发送已成功页。网络结果不明时停止发送，可取回已保存结果。
- 服务中断后，任务会作为“已中断”补入最近 10 条历史；旧版没有标题的记录使用通用标题，不自动执行原任务。
- 更新失败回退时，部署版本记录会随旧程序一起恢复。

## 模型选择

模型检测期间可发送 `/status` 查看检测序号与当前模型。检测发起者或主管理员可用 `/cancel` 取消；目录读取和模型调用均可取消。取消后等待进程清理完成，期间拒绝新任务与 `/restart`，不保存本次不完整结果，并保留上次完整检测记录。重复发送取消不会打断清理；服务关闭也会等待检测停止。

发送 `/model` 后，卡片显示当前模型、该模型上次检测时间与双列图标按钮。当前选项用静态 `✓` 标记，不再重复列出所有模型和档位。

点击“检测当前模型”只检测已选的具体模型，保留其他模型的原有检测时间；尚未选择具体模型时会提示先选择。点击“检测全部”沿用 `/model refresh` 的目录读取和完整检测。检测在原卡片中更新，提供“刷新进度 / 取消检测”按钮；只有发起者或主管理员能取消，旧检测的取消按钮不会影响新检测。完成或取消后收起旧按钮，并提供“返回模型 / 关闭”；全部或部分检测失败使用警告图标和分类说明。检测会消耗模型额度，不改变已选模型；仅打开菜单不会检测。

选择模型系列后，再点击思考强度才会切换：Flash 提供极速、均衡、深度，Pro 提供极速和深度，Claude 5.5 提供极速、均衡、深度、最高。点击“返回模型”或“关闭”不会更改当前设置。

思考强度按双列排列：`⚡ 极速 / ⚖️ 均衡`、`🔍 深度 / 🧠 最高`，仅显示该模型支持且未明确不可用的档位；返回和关闭固定在底部。长引用自动采用原生折叠样式；引用末尾显式写出的“来源：”会独立显示在下方。长回复优先在段落处分页，能完整放入一页的短代码块尽量不拆开。

菜单保留 Gemini 4 Argon、Gemini 3.8 Flash、Gemini 3.1 Pro 和 Claude Sonnet/Opus 5.5。这些选项通过完整模型标识调用，不叠加另一份 `--effort` 参数。`/model argon`、`/model sonnet`、`/model opus` 可快捷选择；Sonnet/Opus 别名指向 5.5 均衡档，原有带 `-5.5` 的别名仍可用。

GPT-OSS 120B、Claude Opus/Sonnet 4.6、Gemini 3.7/3.6 Flash 已从目录、快捷别名与动态发现中移除，旧按钮也不能再选择。历史记录保留；旧配置或偏好如仍指向这些模型，会提示先用 `/model` 重新选择，不自动替换模型或执行任务。

发送 `/model refresh` 会先读取服务器 `agy models`，再逐项发出固定回复检测；成功后保存检测结果，并加入新发现的模型。菜单隐藏 CLI 已不提供或明确提示账号/套餐无权限的选项；网络、额度、容量、授权或地区等失败仍保留选项并显示警告，不当作套餐资格判断。同一模型有已验证档位时，菜单优先打开该档位。目录读取失败时保留上次结果。首次检测前菜单是候选目录，不代表账号已能调用；刷新会消耗模型额度。检测不改用户选定模型、配置、权限或历史，也不重跑原任务。免费与付费账号共用同一程序，以各自服务器的检测结果决定可选模型。

任务遇到模型容量不足时不会自动换模型或续跑。请先核对工作目录里已有的修改，再决定是否重新提交任务或手动切换模型。

模型资格可参考[官方模型列表](https://antigravity.google/docs/models/)，实际目录、套餐权限、地区和调用能力均需在目标服务器确认。

若收到 `User location is not supported for the API use`，表示 Google 拒绝了当前地区的调用，需检查服务器出口及 Google 账号所属地区；修改模型名称、刷新模型或重新授权不能保证解决。参见 [官方地区支持说明](https://antigravity.google/docs/faq/)。程序会提示地区限制，不自动重跑原任务。

切换结果在原卡片确认，只影响后续任务；正在运行的任务不受影响。`/mode` 同样在原消息内确认，减少刷屏。仍可使用 `/model <模型名或别名>` 快捷切换，或用 `/model refresh` 主动检查可用性，打开菜单不会触发检测。

## 更新

在服务器运行：

```bash
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh && bash install.sh
```

运行 `bash install.sh` 后先选择 `2) 更新`。更新复用已保存的 Bot Token、白名单、模型、权限和路径；仅缺少必要配置或 Google 授权失效时需要交互。需要改 Token 或白名单时使用 `bash install.sh --reconfigure`。脚本会先检查新版本；停止正在运行的服务前，显示任务可能中断的提示并询问是否继续。子进程检查只是即时快照，任务仍可能在检查后开始；未完成任务不会自动重跑。配额、网络或其他非授权自检失败时不会自动重新授权。需要直接进入更新流程时可使用 `bash install.sh --install`。

也可以运行 `bash install.sh --menu` 打开菜单：

```text
1) 安装
2) 更新
3) 彻底卸载
0) 退出
```

## 彻底卸载

选择菜单 `3`，查看清理范围后输入 `y` 确认，或输入 `n` 取消。取消时不会停用或移除服务。非交互输入结束也会取消。

这会删除本项目的：

- systemd 服务和运行文件；
- 程序发布版本；
- Bot Token 配置、白名单、任务历史和模型偏好；
- 备份文件与 Bot 接收附件的临时目录；
- 如果有本安装器的安装标记，还会删除该 `agy` 和它的 Google 登录资料；独立安装或来源不明的 agy 和资料会保留。

卸载前会读取配置并显示默认和当前工作目录内的附件缓存路径；路径无法安全确认或含符号链接时停止清理。历史上其他工作目录的缓存不会自动搜索。为了避免误删你的项目，`/root` 工作目录和里面的代码、文件不会删除。自定义 systemd 配置、系统日志和不含凭据的部署锁文件也会保留。BotFather 里创建的 Telegram Bot 也不会被删除；如不再需要，请到 BotFather 手动删除。下载到当前目录的 `install.sh` 需自行删除。

## 常见问题

### 让 Telegram Agent 直接管理整个主机（完整 Root 权限）

管理员明确选择后，可用以下安装参数解除本项目的 systemd 文件系统、提权与能力限制：

```bash
bash install.sh --full-host-access --ref <完整提交SHA>
```

该选项保存 `AGY_HOST_ACCESS=full` 并开启工具自动审批，服务以 root 运行，关闭 `ProtectSystem`、`PrivateTmp`、`NoNewPrivileges`、`RestrictSUIDSGID`，不再设置受限的 `ReadWritePaths` 或清空能力集。Agent 可直接修改 `/usr/local/x-ui`、`/etc/x-ui` 等主机目录和管理系统服务，无需逐目录授权。

所有白名单用户都会获得主机 root 操作能力，包括删除文件和停服。Telegram 私聊与白名单身份验证仍然保留。主机/容器自身权限、只读磁盘、模型容量、网络和软件错误仍可能阻止任务；此选项不保证所有任务成功。

新安装默认为 `restricted`；普通升级保留已选模式。使用 `--restricted-host-access` 可恢复受限模式（保留原额外目录列表和自动审批设置）。`/status` 展示配置值，实际权限以 `systemctl show` 与任务内实测为准。已有自定义 systemd drop-in 不会被安装器擅自删除，可能继续施加限制。

### 能查看服务器，但 Agent 修改文件失败

查看成功不代表有写入权限。`/sys` 直接读取系统信息，不经过 Agent；安装自检也只验证固定文字回复，不验证文件修改。

先发送 `/status` 查看执行模式、工具自动审批和额外可写目录。三层限制分别处理：

1. **只给计划、不执行**：发送 `/mode code` 选择落地编辑。`/mode default` 跟随服务器上的 agy 设置，不保证自动编辑。
2. **`soft-denied` / 工具审批拒绝**：旧安装可能保留 `AGY_SKIP_PERMISSIONS=false`。无交互运行无法弹出审批窗口；可以配置 agy 的精确 `permissions.allow` 规则，或在完全信任的个人服务器上明确使用安装器 `--enable-auto-approve`。
3. **`Read-only file system` / `EROFS`**：服务默认启用 `ProtectSystem=strict`，仅允许 home、工作目录、状态目录及私有临时目录写入。即使 root、落地编辑和工具自动审批均已开启，`/etc`、`/opt` 等应用目录仍可能只读。

需要修改工作区外的应用文件时，先备份目标应用配置，再用 `--write-paths` 明确开放**具体且已存在的目录**。例如只管理 Nginx 配置（实际目录不同请替换）：

```bash
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh && bash install.sh --write-paths /etc/nginx
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

如果服务不是 `active`，先再次运行更新命令。若日志提示授权失效，在保存了 `install.sh` 的目录运行：

```bash
bash install.sh --reauth
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

