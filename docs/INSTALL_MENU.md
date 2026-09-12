# 📋 管理菜单与向导交互说明

## 🚀 快速一键直装命令

在 VPS 终端中直接复制粘贴运行：
```bash
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh && bash install.sh --root
```

## 🎮 普通使用与菜单交互

运行 `bash install.sh`，先显示管理菜单：

```text
=============================================
 Antigravity Telegram Remote 管理菜单
=============================================
  1) 安装 / 更新 (极简 Root 模式)
  2) 卸载服务
  0) 退出

请输入选项 [0/1/2]：
```

输入 `1` 并回车后，自动准备依赖和项目代码，再开始原有三步向导。
不会在管理菜单中提前要求填写 Token 或 ID。

安装中的顺序保持为：
**显示步骤 1 → 等待 Token 输入并回车 → 显示步骤 2 → 等待 ID 输入并回车
→ 配置、目录、离线测试与 Token 检查 → 准备 agy → 显示步骤 3 → Google 授权/复用检查
→ 验证服务 → 成功提示。**

前两步由 `manage.py` 的标准输入顺序执行；本次没有复制这段逻辑到 shell，
也没有通过 shell 变量或命令行参数传递真实 Token。第一步取消时不会显示第二步。
用户仍需通过原有配置验证；本次没有新增“每一步即时校验并重试”。

步骤 3 优先尝试静默复用已有授权；若无有效授权，则直接输出 Google 授权网址。
用户在浏览器授权后粘贴授权码按回车即可自动完成认证并继续，无需手动输入 `/exit`。

## 🗑️ 卸载与退出

输入 `2` 进入原有卸载流程，不会出现安装三步，也不会询问 Bot Token。
仍需输入 `UNINSTALL` 明确确认。普通卸载只移除服务，保留程序、配置、工作目录和授权。
彻底卸载 (`--uninstall --purge`) 会清理程序发布版本与配置，但受最高安全防护，**绝对不会删除或清空宿主机 `/root` 用户主目录及用户文件**。

输入 `0` 或在菜单等待输入时结束输入（EOF），在提权、获取部署锁、
安装依赖及其他安装/卸载操作之前退出。空输入或非法选项只重新提示，不默认安装或删除。

## ⚡ 命令行兼容

| 命令 | 行为 |
|---|---|
| `bash install.sh` | 显示管理菜单 |
| `bash install.sh --install` | 直接进入安装/更新（Root 模式），不再显示管理菜单 |
| `bash install.sh --root` | 兼容参数，直接进入安装/更新（Root 模式） |
| `bash install.sh --enable-auto-approve` | 按原含义直接安装/更新，并明确启用自动审批 |
| `bash install.sh --reauth` | 按原含义直接安装/更新，重新授权 |
| `bash install.sh --ref COMMIT_OR_BRANCH` | 按原含义直接安装选定版本 |
| `bash install.sh --uninstall` | 直接进入卸载确认 |
| `bash install.sh --uninstall --purge` | 保留原来的两次确认与彻底清理限制（保护 /root 安全） |
| `bash install.sh --help` | 只显示帮助，不显示菜单、不获取部署锁 |

非 root 用户运行安装器时会自动尝试 `sudo` 提权。

### 👑 极简 Root 部署模式说明

本版本针对个人独立 VPS 精简为纯 Root 部署模式：
1. 运行账户直接指定为 `root:root`，HOME 目录为 `/root`；
2. 守护进程 systemd unit 中的 `ProtectHome` 策略调整为 `no`，以便直接访问管理宿主机 `/root` 工作区；
3. smoke 自检测试直接在 root 环境下完成权限校验；
4. 升级时自动保留原配置、动态白名单及用户偏好，杜绝多系统用户切换与权限冲突问题。

## 📦 改动范围说明

只修改 `install.sh` 的入口与参数分流，新增 `tests/test_install_menu.py`
和本文档。`manage.py` 与安装器实际部署、卸载、回退代码均未改动。
新安装默认自动审批、旧安装保留配置的规则不变。

这是一项界面改动，不是对先前“步骤 2 后退出”的根因修复。
不能用新增菜单代替错误定位，也不能据此声称已完成真实账号部署验收。

## 🧪 验证与测试

```bash
python3 -B -m unittest discover -s tests -p test_install_menu.py -v
(umask 022; bash scripts/verify.sh)
(umask 077; bash scripts/verify.sh)
git diff --check
```

新增测试只使用临时目录、模拟提权/部署边界与本地伪终端，不执行真实安装、卸载、
账户变更或 Google/Telegram 请求。先检查测试内容再运行，不要在生产环境演练删除操作。
