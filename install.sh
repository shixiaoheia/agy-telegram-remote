#!/usr/bin/env bash
# One-command interactive installer. Debian 12 / Ubuntu 22.04+ only.
set -Eeuo pipefail
APP=/opt/agy-telegram-remote
USER=agy-tg
WORK=/srv/agy-workspace
REPO=https://github.com/shixiaoheia/agy-telegram-remote.git
fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID} -ne 0 ]] || fail 'Run as a normal sudo user, not root.'
. /etc/os-release
[[ "${ID:-}" == debian || "${ID:-}" == ubuntu ]] || fail 'Debian or Ubuntu only.'
sudo -v
printf 'Telegram Bot Token (hidden): '; read -r -s TOKEN; echo
[[ "$TOKEN" == *:* ]] || fail 'Bot Token format looks invalid.'
read -r -p 'Your Telegram numeric ID: ' TG_ID
[[ "$TG_ID" =~ ^[0-9]+$ ]] || fail 'Telegram ID must be numeric.'
read -r -p "agy workspace [$WORK]: " INPUT; WORK=${INPUT:-$WORK}
[[ "$WORK" != / && "$WORK" != /etc && "$WORK" != /home ]] || fail 'Use a dedicated workspace.'
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv curl ca-certificates
id "$USER" >/dev/null 2>&1 || sudo adduser --disabled-password --gecos '' "$USER"
sudo install -d -o "$USER" -g "$USER" -m 0750 "$WORK"
if [[ -d "$APP/.git" ]]; then
  sudo -u "$USER" git -C "$APP" pull --ff-only
elif [[ -e "$APP" ]]; then
  fail "$APP exists but is not this project; move it then retry."
else
  sudo install -d -o "$USER" -g "$USER" "$APP"
  sudo -u "$USER" git clone "$REPO" "$APP"
fi
sudo -u "$USER" python3 -m venv "$APP/venv"
sudo -u "$USER" "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u "$USER" "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
AGY=/home/$USER/.local/bin/agy
if [[ ! -x "$AGY" ]]; then
  read -r -p 'Install agy with Google official installer now? [Y/n] ' GO
  [[ ! "${GO:-Y}" =~ ^[Nn]$ ]] && sudo -iu "$USER" bash -lc 'curl -fsSL https://antigravity.google/cli/install.sh | bash'
fi
[[ -x "$AGY" ]] || fail 'agy missing. Install it, authenticate it, then rerun this script.'
read -r -p 'Have you completed agy authorization for agy-tg? [y/N] ' READY
[[ "$READY" =~ ^[Yy]$ ]] || fail 'Run: sudo -iu agy-tg agy then rerun this script.'
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
echo 'Done. Check: sudo systemctl status agy-telegram-remote'
echo 'Logs: sudo journalctl -u agy-telegram-remote -f'
