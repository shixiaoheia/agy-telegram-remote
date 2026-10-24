# Changelog

## 2026-10-24 控制卡片与点击体验完善

- “查看结果”和 `/last` 在同一结果投递期间及成功后的两秒内防重复发送；失败后释放占用，稍后仍可手动取回，不重新执行任务。状态和菜单的内容未变化不再补发卡片，同一编辑失败的重复点击仅提供一次备用卡片。
- `/usage`、状态详情、帮助、文件列表和白名单改为安全转义的 HTML 排版，清除裸露反引号与长分隔线；会话和 ID 使用代码样式，累计用量使用原生引用统计卡片，保存值中的 HTML 保持字面显示。
- 模型检测在同一张卡片内显示进度、取消清理和最终结果。全部或部分检测失败使用警告图标，结束后收起取消按钮并提供返回模型入口；不改变模型偏好和原有检测资格规则。
- 待确认补充复用原卡片，仅最新内容有效；忽略、过期、任务结束或提交修正时更新说明并收起按钮。补充不会自动执行，原任务的取消和清理规则保持不变。
- “新建对话”按钮增加 90 秒内的确认与保留入口，确认前不清空记忆；对话状态变化后必须重新确认。保留历史结果和累计用量，运行中的任务仍在结束后重置，文字 `/new` 和 `/reset` 保留原有直接操作语义。
- 新增 23 项回归，完整测试 423 项。代码的 10 项 CI 检查通过后再更新日志和说明；真实 Telegram 客户端点按与视觉效果尚未代发消息验收。

## 2026-10-24 原生卡片与操作视觉整理

- 按参考图采用顶部提问引用块、独立 Markdown 正文、底部模型/耗时/Token 引用统计；模型使用友好名称和档位，移除回复顶部的重复统计。附件回显中文名称，旧记录缺失信息时不虚构指标。
- 模型和思考强度统一双列网格与图标，选中项保留勾选，返回/关闭固定在底部；模型资格、档位过滤和实际选择规则保持原有语义。
- 任务开始、运行和结果复用同一气泡。进度保留提问、阶段、耗时与有界执行摘要，长摘要使用原生折叠引用；完成后短回复替换原气泡，长回复仅末页附操作按钮。
- HTML 编辑明确被拒绝时只在原气泡降级一次；网络结果不明不重新发送答案。后续页失败保留已显示的首屏，提示结果已保存及取回入口，不重新执行任务。
- 长引用原生折叠，显式来源标注独立放在引用下方；代码内类似来源的文字保持原样。分页优先段落边界，能完整放入一页的短代码块尽量整体移至下一页。
- 执行摘要和附件回显按上限脱敏、私有保存，历史及 `/last` 继续保留 Markdown 和同一任务按钮。新增 24 项回归，完整测试 400 项；先确认代码 CI 全通过，再更新文档。原生外观颜色由 Telegram 主题决定。

## 2026-10-24 Telegram Markdown 显示修复

- 原始 Markdown 继续保存在结果和历史中，只在发送时转为 Telegram 支持的 HTML；保留标题、强调、代码、引用、链接及列表的阅读层级，不直接信任模型提供的 HTML。
- 代码围栏优先解析，代码内的表格、图示、引用和换行标记保持原样；支持长反引号围栏、波浪线围栏，以及未闭合代码的安全收尾。
- 修复嵌套强调、字面下划线、反斜杠转义、带括号及标题的链接；引用改为原生引用块，表格中的转义竖线与额外列不再丢失。
- 长消息按显示文字的 UTF-16 长度分页，保持 HTML 实体完整，并为每页关闭和恢复格式标签；明确格式拒绝后的备用文本保留链接地址，不重发已成功页。
- 异常长的不完整语法受解析预算约束，剩余文字保留可读内容，避免阻塞消息处理。新增 28 项回归，完整测试 376 项；代码 CI 通过后再更新文档。

## 2026-10-23 状态概览与日常交互

- `/status` 默认显示当前任务、阶段、耗时、模型与部署版本；“详细信息”和“刷新状态”按钮切换或刷新原卡片，`/status details` 保留完整信息。
- 本人的纯文字任务运行中再发补充，会提供“作为修正 / 忽略补充”。只有点击确认才停止旧任务、清理并按原需求加补充执行；仅最新一条有效，5 分钟失效，旧按钮、跨用户与重复点击不会启动任务。
- 历史列表增加服务器本地时间、模型简称和未送达/未完整送达标记，按钮仍定位同一任务，不重新执行。
- 模型菜单增加“检测当前模型 / 检测全部”、当前模型上次检测时间，以及进度和取消按钮；单模型检测保留其他模型的原有检测时间，不切换用户模型。检测仍会消耗模型额度。
- 新增 20 项交互和权限边界回归，完整测试共 348 项；先确认代码 CI 全部通过，再更新本文档。

## 2026-10-23 六项执行与持久化边界修复

- 工具启动状态独立记录，不随最近六条显示进度截断；记录过工具活动后，不因容量错误重发原任务。
- `/steer` 从旧任务清理到修正任务启动持续占用任务位，阻止新文本、相册和模型检测插入，并在接手前核对归属；交接期间 `/cancel` 会取消替代任务，关闭时等待交接完成。
- 最终结果先保存，再更新会话和用量。用量失败提示统计可能缺失；会话更新失败保留结果并暂停后续任务，避免误用上下文。异常内容不直接显示给用户。
- 长回复的某一页被明确拒绝 HTML 格式时，仅该页降级为纯文本；不重发已成功页，网络结果不明或降级发送失败时停止后续投递。
- 安装器原子写入部署版本标记并检查失败；回退时恢复原标记，原先没有标记时移除本次新增记录。
- 服务中断的任务恢复后补入历史；保存有界、脱敏的任务标题，历史补写可重复恢复，不自动重跑任务。

## 2026-10-23 单次执行与模型检测取消

- 统一 Runner 接口，移除捕获 `TypeError` 后减少参数再次执行的兼容逻辑；内部异常沿用原有失败处理暂停新任务，不自动重跑。
- `/model refresh` 支持通过 `/status` 查看当前检测序号和模型，发起者或主管理员可用 `/cancel` 取消目录读取或模型调用；重复取消不会再次打断清理。
- 检测取消后等待进程清理完成再释放占用，保留上次完整检测结果；关闭服务会取消并等待检测结束，清理失败仍暂停新任务。
- 模型检测避让相册接收和纠正任务交接，不更改用户选定模型、权限与任务历史。

## 2026-10-23 历史按钮容错与部署版本显示

- “查看结果”按钮遇到历史读取或权限异常时提示错误，保留原文件，不自动重跑任务；异常不再向外传播并中断后续消息处理。
- `/status` 显示运行版本的提交号及带时区的服务启动时间。版本只从当前发布目录的有效 `.commit` 标记读取，缺失或异常时显示未知，不执行 Git 或猜测版本。

## 2026-10-23 历史记录容量与安全重启修复

- 历史读取预算按最近 10 条完整结果及 JSON/Unicode 字节开销计算，避免多条长中文回复导致历史无法读取或暂停新任务；兼容旧版已超过原读取上限的合法历史文件。
- 历史写入与读取共用字节预算，继续保留最近 10 条、原有单条回复字符上限及私有权限；拒绝超预算的新写入，不覆盖已保存历史。
- `/restart` 在任务取消清理、相册接收、模型检测或修正任务交接期间拒绝执行；重启等待期间不接收新任务或模型检测，避免重复安排重启，并在实际重新载入前再次核对空闲状态。
- 服务停止会取消尚未执行的重启；清理或保存异常时要求在服务器检查并重启服务，不用进程内重载绕过未确认状态。

## 2026-10-23 完成卡与中文附件体验

- 完成卡增加“查看结果”“最近任务”“新建对话”按钮，复用原有私聊白名单、用户历史和延后清空上下文规则；取回结果不会重新执行任务。
- 执行结果与发送状态分开显示。发送失败或不完整时明确提示已保存的结果及取回入口；卡片无法更新时尝试发送简短提示。
- 接收提示与模型任务说明保留中文附件名，去除路径与控制字符，磁盘继续使用私有安全文件名并处理重名。

## 2026-10-23 消息交互与模型目录整理

- 附件准备移到独立任务，下载期间仍可处理状态查询与取消；取消后等待下载线程停止并清理整组文件，再释放工作目录。
- 进度卡遇到网络或服务端错误后退避恢复，遇到限流遵守等待时间；完成前停止刷新，避免覆盖结束状态。
- 最近结果与历史保留思考强度、执行模式，并同步最终投递状态。
- 移除 GPT-OSS 120B、Claude Opus/Sonnet 4.6、Gemini 3.7/3.6 Flash；过滤缓存、CLI 动态发现与旧按钮，旧偏好提示重新选择。
- Sonnet/Opus 快捷别名改为 5.5 均衡档，保留历史记录与配置文件，不自动重跑任务。

## 2026-10-14 账号兼容补充

- 免费与付费账号按各自 CLI 与实际调用结果显示模型，不强制切换套餐或用户选定模型。
- 区分明确的模型资格错误与网络、额度、容量、授权及地区问题；临时失败保留菜单选项并提示原因，不把它当作套餐无权限。
- 同一模型有已验证档位时优先打开该档位；新增免费/付费菜单兼容及临时失败可选回归。

## 2026-10-14 模型目录适配

- 加入 Gemini 4 Argon 和 Claude Sonnet/Opus 5.5 的模型及思考档位；旧 5.5 标识解析到对应新版均衡档。
- 保留明确选择的 Claude 4.6 和其他旧模型，不强制迁移配置或历史。
- `/model refresh` 先读取服务器 CLI 目录，再检测实际调用；保存新发现的模型并隐藏已缺失或不可用选项，目录读取失败保留旧检测结果。
- 新增目录读取的输出、超时与进程组清理边界；档位使用完整模型标识，避免重复传入 `--effort`。
- 文档同步套餐限制与 2026-11-02 旧模型移除计划。真实服务器、Google 和 Telegram 调用需另行验收。

## 2026-10-03 模型修复

- 按 agy 1.2.16 的实际模型列表恢复 Claude Sonnet/Opus 4.6，撤销错误的 4.6 → 5.5 强制转换。
- 将错误版本保存的 5.5 偏好及旧按钮恢复为对应 4.6 模型，保留配置、权限及历史记录；当前显示名称与实际调用一致。
- 单独识别 Google 地区限制错误，提示检查服务器出口和账号地区，不自动重跑任务。

## 2026-10-03

- 将官方模型目录、Telegram `/model` 菜单及 Sonnet/Opus 快捷名称中的 Claude 4.6 替换为 Claude 5.5（Thinking）。
- 旧 Claude 模型名、默认配置及保存的用户偏好按原系列解析到新版，保留原状态文件与历史结果。
- 保留 Gemini 和 GPT-OSS 选项，说明新版 Claude 的账号要求与 `agy models` 可用性核对方式。

## 自动更新流程

- 已有安装运行 `bash install.sh` 直接更新，自动沿用已保存的 Token、白名单和其他设置。
- 首次安装或缺少必要字段时才询问；显式 `--reconfigure` 可重新填写，`--menu` 可打开管理菜单。


## 2026-09-18

- `/model` 改为简洁按钮菜单，使用静态勾号标识当前模型，去除重复的完整模型列表与装饰图标。
- Gemini 选定具体档位后生效，返回/关闭不修改设置；Pro 只显示有效档位，旧面板的档位按钮绑定具体模型。
- 模型与执行模式切换在原卡片确认，编辑失败时回退为新卡片；回调验证私聊与本人会话。
- 同步短轮询补丁及消息接收、确认耗时诊断，缩短网络卡顿后的恢复等待。


## Unreleased — 可选完整主机权限

- 新增 `--full-host-access` / `AGY_HOST_ACCESS=full`，明确启用完整 root 主机权限和工具自动审批，支持更新保留及切回受限模式。
- `/status` 显示权限模式；保留 Telegram 身份校验与结果、进程管理。
- 回归覆盖完整权限服务生成、配置保留、恢复受限模式和非法配置拒绝。

## Unreleased — Agent 写入权限诊断与按目录授权

- 区分只读文件系统、操作系统权限不足和工具审批拒绝；不把带权限失败诊断的 SUCCESS 当作正常完成。
- 新增安装器 `--write-paths` 与 `AGY_WRITE_PATHS`，仅显式配置后开放已有应用目录，支持替换、清空和升级保留；保持默认服务加固。
- `/status` 显示自动审批、执行模式和额外可写目录配置；纠正“默认模式必然自动编辑”和“root 必然能修改所有系统文件”的说明。
- 补充配置保留、路径注入/符号链接拒绝、服务生成和错误回传测试。真实 Google/Telegram 和目标服务器写入需单独验收。

## Unreleased — audit fix for OAuth diagnostics, CI test environment, callback queries, primary owner ID, and root security

- **OAuth 授权包装器诊断增强与进程生命周期加固（OAuth Wrapper Diagnostics & Process Lifecycle）**（`manage.py`、`tests/test_installer.py`）：
  - **环境最小化透传与凭据脱敏**：实现 `oauth_environment()` 严格过滤环境变量，仅保留 SSH 远程识别变量（`SSH_CLIENT`、`SSH_CONNECTION`、`SSH_TTY`）与网络代理环境变量（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`），彻底剥离 Telegram Bot Token、SSH Agent、云密钥及 AI API Key 等敏感凭据；
  - **进程组干净终止**：通过 `start_new_session=True` 与 `kill_process_group()` 在会话超时或中断时对进程组发送 SIGTERM 及 fallback SIGKILL，彻底杜绝 PTY 子进程失控残留；
  - **PTY 管道状态检测与即时反馈**：在向 PTY master 写入前及写入异常时精确感知子进程退出或 EIO 异常，即时给出「授权会话已结束，请重新开始」明确错误，消除写入失败假象；
  - **可配置与充裕的令牌交换超时**：替换原固定的 30+5 秒超时，改为总超时 180 秒并专为令牌网络交换预留 90 秒，避免慢网络与复杂交换被误杀；
  - **精准细分错误分类与脱敏摘要**：新增 `classify_auth_error()` 与 `redact_auth_data()`，细分鉴别 `invalid_grant`（授权码错误/已过期）、网络连接失败、超时、地区/资格受限、协议提示变更及未知错误，脱敏抹除输出中的 code 与 token 并严格限制未知错误输出长度；
  - **测试覆盖**：新增写入端提前关闭、EIO 写入异常、慢成功令牌交换、凭据脱敏与未知错误截断、环境变量最小化透传以及超时杀死进程组等 6 项自动化测试。
- **GitHub Actions CI 与跨平台权限环境修复（CI & Non-Root Environment Fixes）**（`scripts/test_account_debian12.sh`、`install.sh`、`scripts/verify.sh`、`settings.py`、`tests/test_installer.py`）：
  - **可执行权限修复**：在 Git 索引中为 `install.sh`、`scripts/test_account_debian12.sh`、`scripts/verify.sh` 补充可执行位（`chmod +x`），并在 `test_account_debian12.sh` 中采用 `bash "$ROOT_DIR/install.sh" --help` 防御性调用，消除 CI 报 Permission denied (126)；
  - **解耦非 Root CI 运行环境**：重构 `tests/test_installer.py` 中 `test_auth_login_cli_dispatch` 与 `test_prepare_config_preserves_old_settings`，使用 `tempfile.TemporaryDirectory()` 替代硬编码 `/root`，避免非 root CI runner 因权限不足报错 21；
  - **软链接防御异常兼容**：在 `settings.py` 的 `check_no_symlink()` 中增加对不可访问父路径的 `PermissionError` 防御捕获，适配非 root CI 测试。
- **Telegram 行内键盘回调事件恢复（Inline Keyboard Callback Queries Fix）**（`bot.py`、`tests/test_bot.py`）：
  - 修复 `bot.py` 中两处 `getUpdates()`（启动丢弃积压与常规长轮询）将 `allowed_updates` 写死为 `["message"]` 的缺陷，扩充为 `allowed_updates=["message", "callback_query"]`，确保 Telegram 行内键盘点击回调事件正常投递；
  - 补充自动化回归测试，验证授权用户点击按钮正确变更模型/effort/mode，未授权用户回调请求被严格拒绝。
- **多白名单用户主管理员判定一致性对齐（Consistent Primary Owner ID Selection）**（`settings.py`、`bot.py`、`tests/test_settings.py`、`tests/test_bot.py`）：
  - 在 `Settings` 中新增不可变属性 `owner_id: int`，以配置中声明的第一个合法 ID 确定为主管理员（而非对 set 排序后的数值最小者），与配置说明完全一致；
  - 统一 `bot.py` 中的 `/restart` 与 `/whitelist` 权限鉴权，使用 `self.settings.owner_id` 进行校验；
  - 增加「第一个用户 ID 数值非最小」的解析与权限控制测试，以及默认 fallback 逻辑测试。
- **Root 运行更新供应链加固与安全边界规范（Root Supply Chain Hardening & Security Docs）**（`install.sh`、`SECURITY.md`、`README.md`）：
  - **不可变 Commit 追踪与版本锁定**：安装与更新流程中记录并展示不可变 git commit SHA，将版本号写入 `$release/.commit` 与 `$CONFIG_DIR/current_commit`，提供 `--ref <commit-sha>` 确定性版本部署与回滚机制；
  - **官方安装脚本完整性验证**：在下载底层官方 `agy` 安装脚本时校验 HTTPS 协议、bash header 及核心结构标记，支持可选 `AGY_INSTALLER_SHA256` 环境变量哈希校验；
  - **安全文档透明化与风险说明**：更新 `SECURITY.md` 与 `README.md`，真实阐述 Root 特权模式与 `--dangerously-skip-permissions` 的高信任模型，明确白名单凭据泄露或 Prompt Injection 的系统级影响范围与边界。

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

