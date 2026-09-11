#!/usr/bin/env bash
# Antigravity Telegram Remote 交互式一键安装器（Debian 12 / Ubuntu 22.04+）
set -Eeuo pipefail

APP=/opt/agy-telegram-remote
APP_USER=agy-tg
WORK_BASE=/srv/agy-workspace
WORK=$WORK_BASE
REPO=https://github.com/shixiaoheia/agy-telegram-remote.git
SERVICE=agy-telegram-remote

fail() {
  echo
  echo "操作失败：$*" >&2
  exit 1
}

read_or_cancel() {
  local variable_name="$1" cancel_message="$2" rc
  shift 2
  if IFS= read -r "$@" "$variable_name"; then
    return 0
  else
    rc=$?
  fi
  if [[ "$rc" -eq 1 ]]; then
    echo
    echo "$cancel_message"
    exit 0
  fi
  fail "无法读取终端输入（read 返回 $rc）。"
}

run_root() {
  if [[ "$EUID" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

run_as_app_user() {
  if [[ "$EUID" -eq 0 ]]; then
    runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$@"
  else
    sudo -u "$APP_USER" -- env HOME="$APP_HOME" "$@"
  fi
}

[[ -t 0 ]] || fail '请在可交互的 SSH 终端中运行本脚本。'
[[ -r /etc/os-release ]] || fail '无法识别系统版本。'
. /etc/os-release
MAJOR_VERSION="${VERSION_ID%%.*}"
[[ "$MAJOR_VERSION" =~ ^[0-9]+$ ]] || fail '无法识别系统主版本号。'
if [[ "$ID" == debian && "$MAJOR_VERSION" -lt 12 ]]; then
  fail '需要 Debian 12 或更高版本。'
fi
if [[ "$ID" == ubuntu && "$MAJOR_VERSION" -lt 22 ]]; then
  fail '需要 Ubuntu 22.04 或更高版本。'
fi

if [[ "$EUID" -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || fail '请使用 root，或拥有 sudo 权限的普通用户运行。'
  sudo -v
fi

uninstall() {
  [[ "$APP" == "/opt/agy-telegram-remote" ]] || fail '卸载目标异常，已停止。'
  [[ "$WORK_BASE" == "/srv/agy-workspace" ]] || fail '工作目录目标异常，已停止。'

  echo
  echo '============================================='
  echo ' Antigravity Telegram Remote 一键卸载'
  echo '============================================='
  echo "默认卸载会停止并删除服务与程序目录：$APP"
  echo '默认不会删除 Telegram Bot、agy、Google 登录状态、运行账户或工作目录。'
  echo "工作目录仍会保留在：$WORK_BASE"
  echo
  printf '确认默认卸载请输入 UNINSTALL，其它输入会取消： '
  read_or_cancel UNINSTALL_CONFIRM '检测到输入结束，已取消卸载，未修改任何内容。'
  [[ "$UNINSTALL_CONFIRM" == UNINSTALL ]] || {
    echo '已取消卸载，未修改任何内容。'
    return 0
  }

  echo '正在停止并移除 systemd 服务……'
  run_root systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  run_root rm -f -- "/etc/systemd/system/$SERVICE.service"
  run_root systemctl daemon-reload
  run_root systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true

  echo "正在删除程序目录：$APP"
  run_root rm -rf -- "$APP"

  echo
  echo '默认卸载完成。工作目录、agy 与 Google 登录状态仍被保留，之后重新安装可继续使用。'
  echo
  echo '如确定再也不需要这些数据，可选择彻底清理：'
  echo "  - 工作目录：$WORK_BASE"
  echo "  - 受限账户及其 home（其中可能有 agy / Google 登录状态）：$APP_USER"
  printf '要执行彻底清理请输入 PURGE，其它输入则保留数据： '
  read_or_cancel PURGE_CONFIRM '检测到输入结束，已保留工作目录、agy 与 Google 登录状态。'
  if [[ "$PURGE_CONFIRM" != PURGE ]]; then
    echo '已保留工作目录、agy 与 Google 登录状态。'
    return 0
  fi

  if id "$APP_USER" >/dev/null 2>&1; then
    command -v pgrep >/dev/null 2>&1 || fail '缺少 pgrep，无法在彻底清理前确认运行账户没有残余进程。'
    REMAINING_PIDS=$(run_root pgrep -u "$APP_USER" || true)
    if [[ -n "$REMAINING_PIDS" ]]; then
      echo "发现运行账户 $APP_USER 仍有进程：$REMAINING_PIDS"
      echo '为避免误杀手动运行的 agy，已保留工作目录和账户；请结束这些进程后重新运行卸载。'
      return 0
    fi
  fi

  echo '正在彻底清理已明确列出的数据……'
  run_root rm -rf -- "$WORK_BASE"
  if id "$APP_USER" >/dev/null 2>&1; then
    run_root userdel -r "$APP_USER" || fail "无法删除运行账户 $APP_USER；请确认没有该账户的进程后重试。"
  fi
  echo '彻底清理完成。Telegram Bot 本身仍由你的 BotFather 账号管理，未被删除。'
}

echo
echo '============================================='
echo ' Antigravity Telegram Remote'
echo '============================================='
echo '请选择操作：'
echo '  1) 安装或更新'
echo '  2) 一键卸载'
echo '  0) 退出'
printf '> '
read_or_cancel ACTION '检测到输入结束，已退出，未修改任何内容。'
case "$ACTION" in
  1|'') ;;
  2)
    uninstall
    exit 0
    ;;
  0)
    echo '已退出，未修改任何内容。'
    exit 0
    ;;
  *) fail '请输入 0、1 或 2。' ;;
esac

if [[ "$EUID" -eq 0 ]]; then
  command -v runuser >/dev/null 2>&1 || fail '系统缺少 runuser，无法创建受限运行账户。'
fi

ORIGINAL_SSH_CONNECTION="${SSH_CONNECTION-}"
ORIGINAL_SSH_TTY="${SSH_TTY-}"

echo
echo '============================================='
echo ' Antigravity Telegram Remote 一键安装向导'
echo '============================================='
echo '可由 root 或普通 sudo 用户启动；Bot 与 agy 始终使用受限账户 agy-tg 运行。'
echo '接下来会收集必要信息、安装依赖、完成 agy 授权并验证服务。'
echo 'Bot Token 输入时不会显示；请勿将它发给任何人。'

echo
echo '步骤 1/6：请输入 Telegram Bot Token（BotFather 返回的 Token）：'
printf '> '
read_or_cancel TOKEN '检测到输入结束，安装已取消，尚未开始系统改动。' -s
echo
[[ "$TOKEN" == *:* ]] || fail 'Bot Token 格式似乎不正确。'

echo
echo '步骤 2/6：请输入允许使用此工具的 Telegram 数字 ID：'
echo '提示：这是你的纯数字 ID，不是 @用户名，也不是手机号。'
printf '> '
read_or_cancel TG_ID '检测到输入结束，安装已取消，尚未开始系统改动。'
[[ "$TG_ID" =~ ^[0-9]+$ ]] || fail 'Telegram 数字 ID 必须只包含数字。'

echo
echo '步骤 3/6：设置 agy 专用工作目录。'
echo "直接回车使用默认目录 [$WORK_BASE]。"
echo "也可输入 /srv/agy-workspace 下的子目录，或只输入子目录名。"
printf '> '
read_or_cancel INPUT '检测到输入结束，安装已取消，尚未开始系统改动。'
if [[ -n "$INPUT" ]]; then
  if [[ "$INPUT" == /* ]]; then
    WORK=$(realpath -m -- "$INPUT")
  else
    WORK=$(realpath -m -- "$WORK_BASE/$INPUT")
  fi
fi
case "$WORK" in
  "$WORK_BASE"|"$WORK_BASE"/*) ;;
  *) fail "工作目录只能是 $WORK_BASE 或其子目录，不能修改其他系统目录。" ;;
esac

echo
echo '步骤 4/6：选择 agy 的任务权限模式。'
echo '默认安全模式会保留 agy 的权限策略：读写工作目录通常可用，执行命令可能被拒绝。'
echo '若输入 YES，白名单用户的 Telegram 任务将自动批准 agy 的所有工具权限（包括命令和写文件）。'
printf '只有在你完全信任白名单与工作目录时输入 YES，其余情况直接回车： '
read_or_cancel PERMISSION_CONFIRM '检测到输入结束，安装已取消，尚未开始系统改动。'
if [[ "$PERMISSION_CONFIRM" == YES ]]; then
  AGY_SKIP_PERMISSIONS=true
else
  AGY_SKIP_PERMISSIONS=false
fi

echo
echo '正在安装系统依赖、创建受限账户并下载项目，请稍候……'
run_root apt-get update -y
run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv curl ca-certificates procps
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail '需要 Python 3.10 或更高版本。请使用受支持的 Debian / Ubuntu 版本。'
fi
id "$APP_USER" >/dev/null 2>&1 || run_root adduser --disabled-password --gecos '' "$APP_USER"
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)
[[ -n "$APP_HOME" ]] || fail "无法读取 $APP_USER 的主目录。"

if [[ -e "$WORK" && ! -d "$WORK" ]]; then
  fail "工作目录路径已存在但不是目录：$WORK"
fi
if [[ -d "$WORK" ]]; then
  WORK_OWNER=$(stat -c '%U' "$WORK")
  [[ "$WORK_OWNER" == "$APP_USER" ]] || fail "工作目录已存在且不属于 $APP_USER；为防止误改权限，已停止。"
fi
run_root install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$WORK"

if [[ -d "$APP/.git" ]]; then
  if ! run_as_app_user git -C "$APP" pull --ff-only; then
    fail "项目更新失败。若你手动改过 $APP，请先备份或处理 Git 改动后再运行。"
  fi
elif [[ -e "$APP" ]]; then
  fail "$APP 已存在但不是本项目；请先移动该目录后重新运行。"
else
  run_root install -d -o "$APP_USER" -g "$APP_USER" "$APP"
  run_as_app_user git clone "$REPO" "$APP"
fi

run_as_app_user python3 -m venv "$APP/venv"
run_as_app_user "$APP/venv/bin/pip" install -q --upgrade pip
run_as_app_user "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
run_as_app_user "$APP/venv/bin/python" -m py_compile "$APP/bot.py" || fail '项目 Python 语法检查失败。'

echo '正在验证 Telegram Bot Token……'
if ! run_as_app_user env TG_CHECK_TOKEN="$TOKEN" "$APP/venv/bin/python" - <<'PY'
import json
import os
import sys
from urllib.request import urlopen

try:
    with urlopen(
        f"https://api.telegram.org/bot{os.environ['TG_CHECK_TOKEN']}/getMe",
        timeout=15,
    ) as response:
        payload = json.load(response)
except Exception:
    sys.exit(1)

sys.exit(0 if payload.get("ok") else 1)
PY
then
  fail 'Telegram Bot Token 验证失败。请确认 Token、服务器网络和 BotFather 配置。'
fi

AGY="$APP_HOME/.local/bin/agy"
echo
echo '步骤 5/6：检查 Google Antigravity CLI（agy）。'
if [[ ! -x "$AGY" ]]; then
  printf '未检测到 agy。现在按 Google 官方方式安装吗？[Y/n] '
  read_or_cancel GO '检测到输入结束，已停止继续部署；已完成的依赖、项目或授权状态会保留，尚未创建或启动服务。'
  if [[ "$GO" =~ ^[Nn]$ ]]; then
    fail '未安装 agy。请完成安装后重新运行本脚本。'
  fi
  if ! run_as_app_user env HOME="$APP_HOME" bash -lc 'set -o pipefail; curl -fsSL https://antigravity.google/cli/install.sh | bash'; then
    fail 'agy 官方安装器运行失败。请检查服务器网络后重新运行。'
  fi
else
  echo '已检测到 agy，跳过安装。'
fi
[[ -x "$AGY" ]] || fail "未在预期位置找到 agy：$AGY"

echo
echo '步骤 6/6：Google 账号授权（在当前 SSH 终端内完成）。'
echo '脚本会启动 agy；它会打印一次性、安全的 Google 授权链接。'
echo '请复制链接到自己的电脑或手机浏览器完成登录。'
echo '浏览器显示的授权码必须粘贴回当前 SSH 终端中 agy 的提示处。'
echo '不要把授权链接或授权码发给他人，也不要写入 GitHub。'
printf '准备好后输入 Y 并回车，开始授权： '
read_or_cancel AUTH_START '检测到输入结束，已停止继续部署；已完成的依赖、项目或授权状态会保留，尚未创建或启动服务。'
[[ "$AUTH_START" =~ ^[Yy]$ ]] || fail '未开始授权。请重新运行脚本。'

if run_as_app_user env HOME="$APP_HOME" SSH_CONNECTION="$ORIGINAL_SSH_CONNECTION" SSH_TTY="$ORIGINAL_SSH_TTY" bash -c 'cd "$1"; exec "$2"' _ "$WORK" "$AGY"; then
  AGY_EXIT=0
else
  AGY_EXIT=$?
fi
if [[ "$AGY_EXIT" -ne 0 && "$AGY_EXIT" -ne 130 ]]; then
  fail 'agy 授权进程异常退出。请检查屏幕上的错误信息后重新运行。'
fi

echo
echo 'agy 已退出。若已完成授权，请继续部署。'
printf '确认已在 agy 内完成授权并退出后，输入 YES： '
read_or_cancel READY '检测到输入结束，已停止继续部署；已完成的依赖、项目或授权状态会保留，尚未创建或启动服务。'
[[ "$READY" == YES ]] || fail '未确认授权完成。请授权后重新运行脚本。'

echo '正在发送一次只读连接测试，用于确认 agy 已能工作……'
if ! AGY_SMOKE_OUTPUT=$(run_as_app_user env HOME="$APP_HOME" "$AGY" \
  --print-timeout 90s \
  --output-format json \
  --print 'Reply with exactly: AGY ready.'); then
  fail 'agy 连接测试失败。请重新运行授权流程；错误信息已显示在上方。'
fi
if ! printf '%s' "$AGY_SMOKE_OUTPUT" | "$APP/venv/bin/python" -c '
import json
import sys

try:
    result = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(1)

if not isinstance(result, dict):
    raise SystemExit(1)
response = result.get("response")
raise SystemExit(
    0
    if result.get("status") == "SUCCESS"
    and isinstance(response, str)
    and response.strip()
    else 1
)
'; then
  unset AGY_SMOKE_OUTPUT
  fail 'agy 连接测试没有返回可读文本。请重新运行授权步骤后再试。'
fi
unset AGY_SMOKE_OUTPUT

run_root install -m 0600 -o "$APP_USER" -g "$APP_USER" /dev/null "$APP/.env"
run_as_app_user tee "$APP/.env" >/dev/null <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
ALLOWED_USER_IDS=$TG_ID
AGY_PATH=$AGY
AGY_WORKSPACE=$WORK
AGY_TIMEOUT_SECONDS=900
MAX_PROMPT_CHARS=12000
MAX_OUTPUT_BYTES=1048576
MAX_REPLY_CHARS=30000
AGY_SKIP_PERMISSIONS=$AGY_SKIP_PERMISSIONS
EOF
unset TOKEN

run_root tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Antigravity Telegram Remote
After=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
Environment=HOME=$APP_HOME
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
KillMode=control-group
TimeoutStopSec=30s

[Install]
WantedBy=multi-user.target
EOF

run_root systemctl daemon-reload
run_root systemctl enable "$SERVICE"
run_root systemctl restart "$SERVICE"
if ! run_root systemctl is-active --quiet "$SERVICE"; then
  echo
  echo '服务没有成功启动，下面是最近日志：' >&2
  run_root journalctl -u "$SERVICE" -n 80 --no-pager >&2 || true
  fail '服务启动验证失败。'
fi

echo
echo '安装完成，服务已经通过启动检查。'
if [[ "$EUID" -eq 0 ]]; then
  PRIV=''
else
  PRIV='sudo '
fi
echo "查看运行状态：${PRIV}systemctl status $SERVICE"
echo "查看实时日志：${PRIV}journalctl -u $SERVICE -f"
