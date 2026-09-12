#!/usr/bin/env bash
# Debian 12+ / Ubuntu 22.04+. Runtime code is root-owned; agy never runs as root.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

APP=/opt/agy-telegram-remote
RELEASES=/opt/agy-telegram-remote-releases
APP_USER='agy-tg'
APP_HOME=/home/agy-tg
WORK_BASE=/srv/agy-workspace
CONFIG_DIR=/etc/agy-telegram-remote
CONFIG=$CONFIG_DIR/config.env
STATE_BASE=/var/lib/agy-telegram-remote
BACKUPS=/var/backups/agy-telegram-remote
UNIT=/etc/systemd/system/agy-telegram-remote.service
SERVICE='agy-telegram-remote'
REPO=https://github.com/shixiaoheia/agy-telegram-remote.git
DEPLOY_LOCK_FILE="${DEPLOY_LOCK_FILE:-/run/lock/agy-telegram-remote-deploy.lock}"

REF=main
ENABLE_AUTO=0
REAUTH=0
MODE=install
PURGE=0
TRANSACTION=0
COMMITTED=0
SWITCHED=0
CONFIG_CHANGED=0
UNIT_CHANGED=0
OLD_ACTIVE=0
OLD_ENABLED=0
OLD_TARGET=
LEGACY=
BACKUP=
STAGE=
CANDIDATE=

fail() { printf '\n错误：%s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：
  bash install.sh                         安装 / 更新（三步向导）
  bash install.sh --enable-auto-approve    更新时明确改为自动审批
  bash install.sh --reauth                 重新进入 Google 授权
  bash install.sh --ref COMMIT_OR_BRANCH   测试已审阅的提交或分支
  bash install.sh --uninstall              移除服务，保留程序、配置和数据
  bash install.sh --uninstall --purge      二次确认后彻底清理本项目数据
EOF
}

supported_os() {
  local os="$1" version="$2" major minor
  [[ "$version" =~ ^[0-9]+([.][0-9]+)*$ ]] || return 1
  major="${version%%.*}"
  major=$((10#$major))
  case "$os" in
    debian) (( major >= 12 )) ;;
    ubuntu)
      minor=0
      if [[ "$version" == *.* ]]; then
        minor="${version#*.}"; minor="${minor%%.*}"; minor=$((10#$minor))
      fi
      (( major > 22 || (major == 22 && minor >= 4) ))
      ;;
    *) return 1 ;;
  esac
}

root_dir() {
  local path="$1" mode="${2:-0755}" group="${3:-root}"
  [[ ! -L "$path" ]] || fail "拒绝符号链接目录：$path"
  if [[ -e "$path" ]]; then
    [[ -d "$path" && "$(stat -c %u "$path")" == 0 ]] || fail "目录不受 root 管理：$path"
    local perms
    perms="$(stat -c %a "$path")"
    (( (8#$perms & 0022) == 0 )) || fail "目录可被非 root 修改：$path"
  fi
  install -d -o root -g "$group" -m "$mode" "$path"
}

user_dir() {
  local path="$1"
  /usr/bin/python3 -I -c '
import pathlib, sys
p = pathlib.Path(sys.argv[1])
for x in [*reversed(p.parents), p]:
    if x.is_symlink():
        raise SystemExit("Refusing a symlink in runtime directory")
' "$path"
  if [[ -e "$path" ]]; then
    [[ -d "$path" && "$(stat -c %U "$path")" == "$APP_USER" ]] \
      || fail "已有运行目录不属于 $APP_USER：$path"
  fi
  install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$path"
}

ensure_service_account() {
  if ! id "$APP_USER" >/dev/null 2>&1; then
    adduser --system --group \
      --home "$APP_HOME" \
      --shell /bin/bash \
      "$APP_USER"
  fi
  [[ "$(getent passwd "$APP_USER" | cut -d: -f6)" == "$APP_HOME" ]] || fail '已有账户 home 不符合预期。'
  [[ "$(id -gn "$APP_USER")" == "$APP_USER" ]] || fail '已有账户主组异常。'
  (( $(id -u "$APP_USER") >= 100 )) || fail '拒绝使用系统特权账户。'

  local user_group_list all_groups supp_groups=()
  user_group_list="$(id -Gn "$APP_USER")"
  read -r -a all_groups <<<"$user_group_list"
  for g in "${all_groups[@]}"; do
    if [[ "$g" != "$APP_USER" ]]; then
      supp_groups+=("$g")
    fi
  done
  if (( ${#supp_groups[@]} > 0 )); then
    if [[ "${#supp_groups[@]}" -eq 1 && "${supp_groups[0]}" == "users" ]]; then
      echo "提示：若管理员确认 $APP_USER 账户为本项目专用，可由 root 执行：" >&2
      echo "  gpasswd -d $APP_USER users" >&2
    fi
    fail "账户存在额外组权限（实际组：$user_group_list），请先人工核对。"
  fi
}

acquire_deploy_lock() {
  local lock_dir
  lock_dir="$(dirname -- "$DEPLOY_LOCK_FILE")"
  [[ -d "$lock_dir" && ! -L "$lock_dir" ]] || fail "锁定目录异常：$lock_dir"
  if [[ "$EUID" -eq 0 ]]; then
    [[ "$(stat -c %u "$lock_dir")" == 0 ]] || fail "锁定目录不受 root 管理：$lock_dir"
  fi
  [[ ! -L "$DEPLOY_LOCK_FILE" ]] || fail "锁定文件是符号链接：$DEPLOY_LOCK_FILE"
  exec 9>>"$DEPLOY_LOCK_FILE"
  chmod 0600 "$DEPLOY_LOCK_FILE" 2>/dev/null || true
  if ! flock -n 9; then
    fail "已有安装、更新或卸载进程正在运行，请等待其完成。"
  fi
}

as_user() {
  runuser -u "$APP_USER" -- env -i \
    HOME="$APP_HOME" USER="$APP_USER" LOGNAME="$APP_USER" \
    PATH="$APP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
    "$@"
}

rollback() {
  echo '部署未通过验证，正在恢复旧配置、旧服务和旧程序入口……' >&2
  systemctl stop "$SERVICE" >/dev/null 2>&1 || true
  if [[ "$SWITCHED" == 1 ]]; then
    [[ ! -L "$APP" ]] || rm -f -- "$APP"
    if [[ -n "$LEGACY" && -d "$LEGACY" && ! -e "$APP" ]]; then
      mv -- "$LEGACY" "$APP"
    elif [[ -n "$OLD_TARGET" && ! -e "$APP" ]]; then
      ln -s -- "$OLD_TARGET" "$APP"
    fi
  fi
  if [[ "$CONFIG_CHANGED" == 1 ]]; then
    if [[ -f "$BACKUP/config.env" ]]; then
      cp -p -- "$BACKUP/config.env" "$CONFIG"
    else
      rm -f -- "$CONFIG"
    fi
  fi
  if [[ "$UNIT_CHANGED" == 1 ]]; then
    if [[ -f "$BACKUP/service" ]]; then
      cp -p -- "$BACKUP/service" "$UNIT"
    else
      rm -f -- "$UNIT"
    fi
  fi
  systemctl daemon-reload || true
  if [[ "$OLD_ENABLED" == 1 ]]; then
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  else
    systemctl disable "$SERVICE" >/dev/null 2>&1 || true
  fi
  if [[ "$OLD_ACTIVE" == 1 ]]; then
    systemctl restart "$SERVICE" || echo '旧服务恢复启动失败，请人工检查。' >&2
  fi
  echo '已尝试回退；依赖安装、Google 登录及已经发生的任务修改不会被撤销。' >&2
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  set +e
  if [[ "$TRANSACTION" == 1 && "$COMMITTED" == 0 ]]; then
    rollback
    (( rc != 0 )) || rc=1
  fi
  if [[ -n "$CANDIDATE" && -f "$CANDIDATE" ]]; then
    rm -f -- "$CANDIDATE"
  fi
  exit "$rc"
}

uninstall() {
  local answer
  echo '此操作移除 systemd 服务，默认保留程序、配置、工作目录及 Google 登录。'
  read -r -p '确认请输入 UNINSTALL： ' answer || exit 0
  [[ "$answer" == UNINSTALL ]] || exit 0
  [[ ! -L "$UNIT" ]] || fail "服务文件是链接，请人工检查。"
  systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$SERVICE"; then
    fail '服务未能停止；未移除服务文件。'
  fi
  rm -f -- "$UNIT"
  systemctl daemon-reload
  if [[ "$PURGE" == 0 ]]; then
    echo '服务已移除，所有程序和数据保留。'
    return
  fi
  echo "彻底清理将删除：$APP、$RELEASES、$CONFIG_DIR、$STATE_BASE、$BACKUPS"
  echo "以及 $WORK_BASE 和账户 $APP_USER 的 $APP_HOME。"
  read -r -p '确认永久删除上述数据请输入 PURGE： ' answer || exit 0
  [[ "$answer" == PURGE ]] || exit 0
  if id "$APP_USER" >/dev/null 2>&1; then
    [[ "$(getent passwd "$APP_USER" | cut -d: -f6)" == "$APP_HOME" ]] || fail '账户 home 异常。'
    if pgrep -u "$APP_USER" >/dev/null; then
      fail '运行账户仍有进程；未执行清理，请先人工结束这些进程。'
    fi
  fi
  for directory in "$RELEASES" "$CONFIG_DIR" "$STATE_BASE" "$BACKUPS" "$WORK_BASE" "$APP_HOME"; do
    [[ ! -L "$directory" ]] || fail "拒绝清理符号链接：$directory"
  done
  rm -rf -- "$APP" "$RELEASES" "$CONFIG_DIR" "$STATE_BASE" "$BACKUPS" "$WORK_BASE"
  if id "$APP_USER" >/dev/null 2>&1; then
    userdel -r "$APP_USER"
  fi
  echo '彻底清理完成；没有删除 BotFather 中的 Telegram Bot。'
}

main() {
  local original_args=("$@")
  while (( $# )); do
    case "$1" in
      --help|-h) usage; return ;;
      --enable-auto-approve) ENABLE_AUTO=1 ;;
      --reauth) REAUTH=1 ;;
      --uninstall) MODE=uninstall ;;
      --purge) PURGE=1 ;;
      --ref)
        (( $# >= 2 )) || fail '--ref 缺少值。'
        REF="$2"; shift
        [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$REF" != *..* ]] || fail '非法 Git ref。'
        ;;
      *) fail "未知参数：$1" ;;
    esac
    shift
  done
  [[ "$MODE" == uninstall || "$PURGE" == 0 ]] || fail '--purge 必须与 --uninstall 一起使用。'
  [[ -t 0 && -t 1 ]] || fail '请在可交互 SSH 终端运行，不要把脚本通过管道传给 bash。'
  if [[ "$EUID" -ne 0 ]]; then
    command -v sudo >/dev/null || fail '请使用 root 或具备 sudo 权限的账户。'
    exec sudo /usr/bin/env SSH_CONNECTION="${SSH_CONNECTION-}" SSH_TTY="${SSH_TTY-}" \
      /bin/bash "$0" "${original_args[@]}"
  fi
  [[ -r /etc/os-release ]] || fail '无法识别系统。'
  # Only the system-owned OS descriptor is sourced. Never source a .env file.
  . /etc/os-release
  acquire_deploy_lock
  supported_os "$ID" "$VERSION_ID" || fail '仅支持 Debian 12+ / Ubuntu 22.04+。'
  [[ -d /run/systemd/system ]] || fail '需要正在运行的 systemd（不支持普通容器或 Alpine）。'
  systemctl show-environment >/dev/null || fail '无法连接 systemd。'
  if [[ "$MODE" == uninstall ]]; then
    uninstall
    return
  fi

  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  echo '============================================='
  echo ' Antigravity Telegram Remote 极简一键安装向导'
  echo '============================================='
  echo '新安装默认自动审批，始终以非 root 账户运行。仅供完全信任的白名单用户使用。'
  echo '正在准备系统依赖和已选择的项目版本……'
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 git curl ca-certificates procps util-linux adduser
  /usr/bin/python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail '需要 Python 3.10+。'
  ensure_service_account
  user_dir "$APP_HOME"
  root_dir "$RELEASES"
  root_dir "$BACKUPS" 0700
  root_dir "$CONFIG_DIR" 0750 "$APP_USER"

  [[ ! -L "$CONFIG" && ! -L "$UNIT" ]] || fail '配置或服务文件是符号链接。'
  if [[ -L "$APP" ]]; then
    OLD_TARGET="$(readlink -f -- "$APP")"
    [[ "$OLD_TARGET" == "$RELEASES/"* && -d "$OLD_TARGET" ]] || fail '程序入口指向未知位置。'
    [[ "$(stat -c %u "$OLD_TARGET")" == 0 ]] || fail '旧发布目录并非 root 所有。'
  elif [[ -e "$APP" ]]; then
    [[ -d "$APP" && -f "$APP/bot.py" && -f "$APP/.env" && ! -L "$APP/.env" ]] \
      || fail '已有程序目录无法安全识别为旧版安装。'
  fi

  STAGE="$(mktemp -d "$RELEASES/.stage-XXXXXXXX")"
  chmod 0755 "$STAGE"
  git -c core.hooksPath=/dev/null init -q "$STAGE"
  git -c core.hooksPath=/dev/null -c protocol.file.allow=never -C "$STAGE" \
    fetch -q --depth=1 "$REPO" "$REF"
  git -c core.hooksPath=/dev/null -C "$STAGE" checkout -q --detach FETCH_HEAD
  local commit release
  commit="$(git -C "$STAGE" rev-parse HEAD)"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail '无法确认 Git 提交。'
  [[ -z "$(find "$STAGE" -path "$STAGE/.git" -prune -o -type l -print -quit)" ]] \
    || fail '发布内容含符号链接，已停止。'
  rm -rf -- "$STAGE/.git"
  find "$STAGE" -type d -exec chmod 0755 {} +
  find "$STAGE" -type f -exec chmod 0644 {} +
  for required in bot.py settings.py state_store.py telegram_api.py agy_runner.py manage.py; do
    [[ -f "$STAGE/$required" ]] || fail "所选提交缺少 $required；请先上传完整新版。"
  done
  echo "采用提交：$commit"

  CANDIDATE="$(mktemp "$CONFIG_DIR/.candidate-XXXXXXXX")"
  chown root:"$APP_USER" "$CANDIDATE"; chmod 0640 "$CANDIDATE"
  local prepare=(prepare-config --output "$CANDIDATE" --home "$APP_HOME")
  if [[ -f "$CONFIG" ]]; then
    prepare+=(--old "$CONFIG")
  elif [[ -f "$APP/.env" ]]; then
    prepare+=(--old "$APP/.env")
  fi
  [[ "$ENABLE_AUTO" == 0 ]] || prepare+=(--enable-auto)
  # This helper and its imports came from a new root-owned checkout, NOT an old venv.
  /usr/bin/python3 -E -s -B "$STAGE/manage.py" "${prepare[@]}"

  local fields=()
  mapfile -t fields < <(/usr/bin/python3 -E -s -B "$STAGE/manage.py" fields --config "$CANDIDATE")
  [[ "${#fields[@]}" == 4 && "${fields[0]}" == "$APP_HOME" ]] || fail '配置读取失败。'
  local work="${fields[1]}" state="${fields[2]}" agy="${fields[3]}"
  user_dir "$work"
  user_dir "$state"
  echo '正在检查本地代码和 Telegram Bot……'
  local test_log
  test_log="$(mktemp "$RELEASES/.tests-XXXXXXXX")"
  if ! as_user /usr/bin/python3 -I -B -m unittest discover -s "$STAGE/tests" -q > "$test_log" 2>&1; then
    tail -n 80 "$test_log" >&2
    fail '离线自检失败，旧服务尚未被停止。'
  fi
  rm -f -- "$test_log"
  as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" check-token --config "$CANDIDATE"

  BACKUP="$(mktemp -d "$BACKUPS/deploy-XXXXXXXX")"
  [[ ! -f "$CONFIG" ]] || cp -p -- "$CONFIG" "$BACKUP/config.env"
  [[ ! -f "$UNIT" ]] || cp -p -- "$UNIT" "$BACKUP/service"
  systemctl is-active --quiet "$SERVICE" && OLD_ACTIVE=1
  systemctl is-enabled --quiet "$SERVICE" && OLD_ENABLED=1
  TRANSACTION=1
  echo '正在停止旧服务；未完成任务不会自动重跑。'
  systemctl stop "$SERVICE" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$SERVICE"; then
    fail '旧服务没有停止，不能继续部署。'
  fi
  if pgrep -u "$APP_USER" >/dev/null; then
    fail '运行账户仍有进程，请先检查手工运行的 agy 或残余任务。'
  fi

  local installed_now=0
  if [[ ! -x "$agy" ]]; then
    [[ "$agy" == "$APP_HOME/.local/bin/agy" ]] || fail '自定义 AGY_PATH 不存在，请先人工安装。'
    echo '正在以受限账户安装 Google 官方 agy……'
    local official_installer
    official_installer="$(mktemp "$RELEASES/.agy-installer-XXXXXXXX")"
    curl --proto '=https' --tlsv1.2 -fsSL https://antigravity.google/cli/install.sh \
      -o "$official_installer"
    chmod 0644 "$official_installer"
    as_user /bin/bash "$official_installer"
    rm -f -- "$official_installer"
    [[ -x "$agy" ]] || fail '官方安装器未在预期位置生成 agy。'
    installed_now=1
  fi

  echo
  echo '步骤 3/3：Google 账号授权'
  echo '已有有效授权会自动复用。新授权请打开链接登录，再粘贴授权码。'
  echo '进入 agy 主界面后输入 /exit 返回安装器；不需要额外输入 YES。'
  local smoke_rc=10
  if [[ "$installed_now" == 0 && "$REAUTH" == 0 ]]; then
    if as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" smoke --config "$CANDIDATE"; then
      smoke_rc=0
    else
      smoke_rc=$?
    fi
  fi
  if [[ "$smoke_rc" == 10 || "$REAUTH" == 1 ]]; then
    local auth_rc=0
    as_user env SSH_CONNECTION="${SSH_CONNECTION-}" SSH_TTY="${SSH_TTY-}" \
      /bin/bash -c 'cd -- "$1"; exec "$2"' _ "$work" "$agy" || auth_rc=$?
    [[ "$auth_rc" == 0 || "$auth_rc" == 130 ]] || fail 'agy 交互授权异常退出。'
    if as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" smoke --config "$CANDIDATE"; then
      smoke_rc=0
    else
      smoke_rc=$?
    fi
  fi
  [[ "$smoke_rc" == 0 ]] || fail \
    "agy 自检未通过（代码 $smoke_rc）。配额、网络和协议问题不能靠反复重新授权解决。"
  as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" check-local --config "$CANDIDATE"

  release="$RELEASES/$commit-$(date +%s)-$$"
  mv -- "$STAGE" "$release"
  STAGE=
  SWITCHED=1
  if [[ -d "$APP" && ! -L "$APP" ]]; then
    LEGACY="$BACKUP/legacy-app"
    mv -- "$APP" "$LEGACY"
  fi
  ln -s -- "$release" "$APP.next.$$"
  mv -Tf -- "$APP.next.$$" "$APP"
  CONFIG_CHANGED=1
  mv -f -- "$CANDIDATE" "$CONFIG"
  CANDIDATE=
  chown root:"$APP_USER" "$CONFIG"; chmod 0640 "$CONFIG"
  /usr/bin/python3 -E -s -B "$release/manage.py" unit --config "$CONFIG" > "$BACKUP/new.service"
  UNIT_CHANGED=1
  install -o root -g root -m 0644 "$BACKUP/new.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"

  echo '正在验证机器人初始化和长轮询就绪……'
  local ready=0 pid
  for _ in {1..180}; do
    pid="$(systemctl show "$SERVICE" -p MainPID --value)"
    if systemctl is-active --quiet "$SERVICE" \
      && /usr/bin/python3 -E -s -B "$release/manage.py" check-ready \
        --file /run/agy-telegram-remote/ready.json --pid "${pid:-0}" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 0.5
  done
  [[ "$ready" == 1 ]] || fail '服务没有通过应用级就绪检查；请查看 journalctl 日志。'
  COMMITTED=1
  echo
  echo '🎉 安装成功！服务已启动。'
  echo "配置：$CONFIG"
  echo "工作目录：$work"
  echo "回退备份：$BACKUP（root 私有，包含旧配置；请勿上传）"
  echo "查看日志：sudo journalctl -u $SERVICE -n 80 --no-pager"
  echo '请在 Telegram 发送 /start，再做一条只读任务。'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
