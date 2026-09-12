# 📋 管理菜单与向导交互说明

## 🎮 普通使用与菜单交互

运行 `bash install.sh`，先显示管理菜单：

```text
=============================================
 Antigravity Telegram Remote 管理菜单
=============================================
  1) 安装 / 更新
  2) 卸载
  0) 退出

请输入选项 [0/1/2]：
```

输入 `1` 并回车后，自动准备依赖和项目代码，再开始原有三步向导。
不会在管理菜单中提前要求填写 Token 或 ID。

安装中的顺序保持为：
**显示步骤 1 → 等待 Token 输入并回车 → 显示步骤 2 → 等待 ID 输入并回车
→ 配置、目录、离线测试与 Token 检查 → 准备 agy → 显示步骤 3 → Google 授权/复用检查
→ 验证服务 → 成功提示。**

前两步由 `manage.py` 的 `getpass()` 与 `input()` 顺序执行；本次没有复制这段逻辑到 shell，
也没有通过 shell 变量或命令行参数传递真实 Token。第一步取消时不会显示第二步。
用户仍需通过原有配置验证；本次没有新增“每一步即时校验并重试”。

步骤 3 延用当前的官方 agy 交互流程；已有有效授权按原逻辑尝试复用。
如进入 agy 主界面，需要输入 `/exit` 返回安装器。不承诺粘贴授权码后一定立即自动退出。

## 🗑️ 卸载与退出

输入 `2` 进入原有卸载流程，不会出现安装三步，也不会询问 Bot Token。
仍需输入 `UNINSTALL` 明确确认。普通卸载只移除服务，保留程序、配置、工作目录和授权。
本次没有新增、放宽或自动触发彻底清理。

输入 `0` 或在菜单等待输入时结束输入（EOF），在提权、获取部署锁、
安装依赖及其他安装/卸载操作之前退出。空输入或非法选项只重新提示，不默认安装或删除。

## ⚡ 命令行兼容

| 命令 | 行为 |
|---|---|
| `bash install.sh` | 显示管理菜单 |
| `bash install.sh --install` | 直接进入安装/更新，不再显示管理菜单 |
| `bash install.sh --root` | 直接以 root 账户安装/更新与运行后台守护进程，省去多系统用户隔离（个人 VPS 极简模式） |
| `bash install.sh --enable-auto-approve` | 按原含义直接安装/更新，并明确启用自动审批 |
| `bash install.sh --reauth` | 按原含义直接安装/更新，重新授权 |
| `bash install.sh --ref COMMIT_OR_BRANCH` | 按原含义直接安装选定版本 |
| `bash install.sh --uninstall` | 直接进入卸载确认 |
| `bash install.sh --uninstall --purge` | 保留原来的两次确认与彻底清理限制 |
| `bash install.sh --help` | 只显示帮助，不显示菜单、不获取部署锁 |

没有 sudo 权限的运行账户不能用来安装。普通管理账户选择模式后，
安装器在提权重启时携带所选模式，避免重复显示菜单。

### 👑 Root 极简部署模式说明

使用 `bash install.sh --root` 时：
1. 运行账户直接指定为 `root:root`，HOME 目录为 `/root`；
2. 守护进程 systemd unit 中的 `ProtectHome` 策略调整为 `no`，以便直接访问管理宿主机 `/root` 工作区；
3. smoke 自检测试自动携带 `--allow-root` 参数通过权限校验；
4. 升级时自动保留原配置、动态白名单及用户偏好，省去多系统用户切换与权限排查成本。

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
