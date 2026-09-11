#!/usr/bin/env bash
# Antigravity Telegram Remote 交互式一键安装器（Debian 12 / Ubuntu 22.04+）
set -Eeuo pipefail
APP=/opt/agy-telegram-remote
USER=agy-tg
WORK=/srv/agy-workspace
REPO=https://github.com/shixiaoheia/agy-telegram-remote.git
fail(){ printf '
安装失败：%s
' "$*" >&2; exit 1; }
[[ ${EUID} -ne 0 ]] || fail '请使用普通 sudo 用户运行，不要直接使用 root。'
. /etc/os-release
[[ "${ID:-}" == debian || "${ID:-}" == ubuntu ]] || fail '仅支持 Debian 或 Ubuntu。'
sudo -v
printf '
=============================================
'
printf ' Antigravity Telegram Remote 一键安装向导
'
printf '=============================================
'
echo '接下来会按步骤收集信息，并自动安装依赖、配置服务。'
echo 'Bot Token 输入时不会显示；请勿将它发给任何人。'
printf '
步骤 1/5：请输入 Telegram Bot Token（BotFather 返回的 Token）：
'
printf '> '; read -r -s TOKEN; echo
[[ "$TOKEN" == *:* ]] || fail 'Bot Token 格式似乎不正确。'
printf '
步骤 2/5：请输入允许使用此工具的 Telegram 数字 ID：
'
echo '提示：这是你的纯数字 ID，不是 @用户名，也不是手机号。'
printf '> '; read -r TG_ID
[[ "$TG_ID" =~ ^[0-9]+$ ]] || fail 'Telegram 数字 ID 必须只包含数字。'
printf '
步骤 3/5：请输入 agy 的专用工作目录。
'
printf '直接回车使用默认目录 [%s]：
> ' "$WORK"; read -r INPUT
WORK=${INPUT:-$WORK}
[[ "$WORK" != / && "$WORK" != /etc && "$WORK" != /home ]] || fail '请使用专用工作目录，不可填写 /、/etc 或 /home。'
printf '
正在安装系统依赖、创建受限账户并下载项目，请稍候……
'
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv curl ca-certificates
id "$USER" >/dev/null 2>&1 || sudo adduser --disabled-password --gecos '' "$USER"
sudo install -d -o "$USER" -g "$USER" -m 0750 "$WORK"
if [[ -d "$APP/.git" ]]; then
  sudo -u "$USER" git -C "$APP" pull --ff-only
elif [[ -e "$APP" ]]; then
  fail "$APP 已存在但不是本项目；请先移动该目录后重新运行。"
else
  sudo install -d -o "$USER" -g "$USER" "$APP"
  sudo -u "$USER" git clone "$REPO" "$APP"
fi
sudo -u "$USER" python3 -m venv "$APP/venv"
sudo -u "$USER" "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u "$USER" "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
AGY=/home/$USER/.local/bin/agy
printf '
步骤 4/5：检查 Google Antigravity CLI（agy）。
'
if [[ ! -x "$AGY" ]]; then
  read -r -p '未检测到 agy。现在按官方方式安装吗？[Y/n] ' GO
  [[ ! "${GO:-Y}" =~ ^[Nn]$ ]] && sudo -iu "$USER" bash -lc 'curl -fsSL https://antigravity.google/cli/install.sh | bash'
else
  echo '已检测到 agy，跳过安装。'
fi
[[ -x "$AGY" ]] || fail '未安装 agy。请完成安装后重新运行本脚本。'
printf '
步骤 5/5：请完成 agy 的 Google 授权。
'
echo "请在另一个 SSH 终端执行：sudo -iu $USER agy"
echo '根据出现的链接完成 Google 登录；完成后回到这里。'
read -r -p '确认授权完成后，请输入 YES 继续： ' READY
[[ "$READY" == YES ]] || fail '未确认授权完成。请授权后重新运行脚本。'
sudo install -m 0600 -o "$USER" -g "$USER" /dev/null "$APP/.env"
sudo -u "$USER" tee "$APP/.env" >/dev/null <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
ALLOWED_USER_IDS=$TG_ID
AGY_PATH=$AGY
AGY_WORKSPACE=$WORK
AGY_TIMEOUT_SECONDS=900
EOF
unset TOKEN
sudo tee /etc/systemd/system/agy-telegram-remote.service >/dev/null <<EOF
[Unit]
Description=Antigravity Telegram Remote
After=network-online.target
[Service]
User=$USER
Group=$USER
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/bot.py
Restart=on-failure
NoNewPrivileges=yes
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now agy-telegram-remote
printf '
安装完成！
'
echo '查看运行状态：sudo systemctl status agy-telegram-remote'
echo '查看实时日志：sudo journalctl -u agy-telegram-remote -f'
