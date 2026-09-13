#!/usr/bin/env bash
# Debian 12+ / Ubuntu 22.04+. Native Root mode on dedicated Linux VPS.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

APP=/opt/agy-telegram-remote
RELEASES=/opt/agy-telegram-remote-releases
APP_USER='root'
APP_HOME=/root
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
MODE=menu
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
=============================================
 Antigravity Telegram Remote 安装与管理脚本
=============================================
用法：
  bash install.sh                         显示管理菜单（安装、更新、彻底卸载、退出）
  bash install.sh --install               👑 极简 Root 模式安装/更新（直接以 root 运行）
  bash install.sh --root                  👑 极简 Root 模式安装/更新（快捷别名）
  bash install.sh --enable-auto-approve    更新时明确启用自动审批（跳过权限确认弹窗）
  bash install.sh --reauth                 重新进入 Google 账号授权流程
  bash install.sh --ref COMMIT_OR_BRANCH   安装/升级指定不可变 Git 提交 SHA 或分支（推荐生产使用 Commit SHA）
  bash install.sh --uninstall              安全卸载服务（仅停止并移除服务，保留数据与配置）
  bash install.sh --uninstall --purge      彻底清理（需输入 PURGE 二次确认，清除部署与配置）
  bash install.sh --help                   显示此帮助说明
EOF
}

# This menu only selects a mode. No credentials or system changes happen here.
choose_operation() {
  local choice rc
  echo '============================================='
  echo ' Antigravity Telegram Remote 管理菜单'
  echo '============================================='
  echo '  1) 安装'
  echo '  2) 更新'
  echo '  3) 彻底卸载（清除部署与配置）'
  echo '  0) 退出'
  echo
  while true; do
    printf '请输入选项 [0/1/2/3]： '
    if IFS= read -r choice; then
      case "$choice" in
        1) MODE=install; return 0 ;;
        # 安装和更新走同一套原子部署流程；更新会自动保留现有配置。
        2) MODE=install; return 0 ;;
        3) MODE=uninstall; PURGE=1; return 0 ;;
        0)
          MODE="exit"
          echo '已退出，未开始任何安装或卸载操作。'
          return 0
          ;;
        *) echo '请输入 1、2、3 或 0，然后回车。' ;;
      esac
    else
      rc=$?
      if [[ "$rc" -eq 1 ]]; then
        MODE="exit"
        echo
        echo '检测到输入结束，已退出，未开始安装或卸载。'
        return 0
      fi
      fail "无法读取菜单选项（read 返回 $rc）。"
    fi
  done
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
  if [[ -e "$path" && ! -d "$path" ]]; then
    fail "已有运行路径不是目录：$path"
  fi
  if [[ -L "$path" ]]; then
    fail "已有运行路径是符号链接：$path"
  fi
  if [[ -d "$path" ]]; then
    [[ "$(stat -c %u "$path")" == 0 ]] || fail "已有运行目录并非 root 所有：$path"
  else
    install -d -o root -g root -m 0700 "$path"
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
  env -i \
    HOME="$APP_HOME" USER="root" LOGNAME="root" \
    PATH="$APP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
    TERM="${TERM:-xterm-256color}" \
    HTTP_PROXY="${HTTP_PROXY-}" HTTPS_PROXY="${HTTPS_PROXY-}" ALL_PROXY="${ALL_PROXY-}" NO_PROXY="${NO_PROXY-}" \
    http_proxy="${http_proxy-}" https_proxy="${https_proxy-}" all_proxy="${all_proxy-}" no_proxy="${no_proxy-}" \
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
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    fail '服务未能停止；未移除服务文件。'
  fi
  rm -f -- "$UNIT"
  systemctl daemon-reload
  if [[ "$PURGE" == 0 ]]; then
    echo '服务已移除，所有程序和数据保留。'
    return
  fi
  echo "彻底清理将删除：$APP、$RELEASES、$CONFIG_DIR、$STATE_BASE、$BACKUPS"
  echo "注意：为保护系统安全，/root 个人目录与工作空间将完整保留。"
  read -r -p '确认永久删除上述数据请输入 PURGE： ' answer || exit 0
  [[ "$answer" == PURGE ]] || exit 0
  for directory in "$RELEASES" "$CONFIG_DIR" "$STATE_BASE" "$BACKUPS"; do
    [[ ! -L "$directory" ]] || fail "拒绝清理符号链接：$directory"
  done
  rm -rf -- "$APP" "$RELEASES" "$CONFIG_DIR" "$STATE_BASE" "$BACKUPS"
  echo '彻底清理完成；保留了 /root 工作区与 Google 登录凭据，未删除 BotFather 中的 Telegram Bot。'
}

main() {
  local original_args=("$@")
  local direct_install=0
  while (( $# )); do
    case "$1" in
      --help|-h) usage; return ;;
      --install|--root) MODE=install; direct_install=1 ;;
      --enable-auto-approve) ENABLE_AUTO=1; direct_install=1 ;;
      --reauth) REAUTH=1; direct_install=1 ;;
      --uninstall) MODE=uninstall ;;
      --purge) PURGE=1 ;;
      --ref)
        (( $# >= 2 )) || fail '--ref 缺少值。'
        REF="$2"; shift
        direct_install=1
        [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$REF" != *..* ]] || fail '非法 Git ref。'
        ;;
      *) fail "未知参数：$1" ;;
    esac
    shift
  done
  # Existing explicit install flags remain shortcuts; bare invocation shows the menu.
  if [[ "$MODE" == menu && "$direct_install" == 1 ]]; then
    MODE=install
  fi
  [[ "$MODE" == uninstall || "$PURGE" == 0 ]] || fail '--purge 必须与 --uninstall 一起使用。'
  [[ -t 0 && -t 1 ]] || fail '请在可交互 SSH 终端运行，不要把脚本通过管道传给 bash。'
  if [[ "$MODE" == menu ]]; then
    choose_operation
  fi
  [[ "$MODE" != exit ]] || return 0
  if [[ "$EUID" -ne 0 ]]; then
    command -v sudo >/dev/null || fail '请使用 root 或具备 sudo 权限的账户。'
    if [[ "$PURGE" == 1 ]]; then
      exec sudo /usr/bin/env SSH_CONNECTION="${SSH_CONNECTION-}" SSH_TTY="${SSH_TTY-}" \
        /bin/bash "$0" "${original_args[@]}" "--purge" "--$MODE"
    fi
    exec sudo /usr/bin/env SSH_CONNECTION="${SSH_CONNECTION-}" SSH_TTY="${SSH_TTY-}" \
      /bin/bash "$0" "${original_args[@]}" "--$MODE"
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
  echo '👑 运行模式：个人 VPS 极简 Root 模式（运行账户: root，工作目录: /root）'
  echo '⚡ 特性支持：默认开启自动审批（无头运行不挂起），仅供信任的白名单用户使用。'
  echo '📦 正在准备系统依赖与核心运行环境……'
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 git curl ca-certificates procps util-linux
  /usr/bin/python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail '需要 Python 3.10+。'
  user_dir "$APP_HOME"
  root_dir "$RELEASES"
  root_dir "$BACKUPS" 0700
  root_dir "$CONFIG_DIR" 0700 root

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
  chown root:root "$CANDIDATE"; chmod 0600 "$CANDIDATE"
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
  OLD_ACTIVE=0
  OLD_ENABLED=0
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    OLD_ACTIVE=1
  fi
  if [[ -f "$UNIT" ]] && systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
    OLD_ENABLED=1
  fi
  TRANSACTION=1
  if [[ "$OLD_ACTIVE" == 1 ]]; then
    echo '正在停止旧服务；未完成任务不会自动重跑。'
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
      fail '旧服务没有停止，不能继续部署。'
    fi
  fi
  if pgrep -f '/opt/agy-telegram-remote/bot.py' >/dev/null 2>&1; then
    fail '检测到旧版本 bot.py 进程仍在运行，请先手动结束该进程。'
  fi

  local installed_now=0
  if [[ ! -x "$agy" ]]; then
    [[ "$agy" == "$APP_HOME/.local/bin/agy" ]] || fail '自定义 AGY_PATH 不存在，请先人工安装。'
    echo '正在安装 Google 官方 agy……'
    local official_installer
    official_installer="$(mktemp "$RELEASES/.agy-installer-XXXXXXXX")"
    curl --proto '=https' --tlsv1.2 -fsSL https://antigravity.google/cli/install.sh \
      -o "$official_installer"
    chmod 0644 "$official_installer"
    if [[ ! -s "$official_installer" ]] || ! head -n 1 "$official_installer" | grep -q '^#!/bin/bash'; then
      rm -f -- "$official_installer"
      fail '官方 agy 安装器校验失败：下载内容为空或缺少合法的 bash 头部。'
    fi
    if ! grep -q 'DOWNLOAD_BASE_URL=' "$official_installer" || ! grep -q 'Antigravity CLI' "$official_installer"; then
      rm -f -- "$official_installer"
      fail '官方 agy 安装器校验失败：未包含官方受信任的结构特征。'
    fi
    local expected_hash="${AGY_INSTALLER_SHA256:-}"
    if [[ -n "$expected_hash" ]]; then
      local actual_hash
      actual_hash="$(sha256sum "$official_installer" | cut -d' ' -f1)"
      if [[ "$actual_hash" != "$expected_hash" ]]; then
        rm -f -- "$official_installer"
        fail "官方 agy 安装器哈希校验失败：实际哈希 $actual_hash 与预期 $expected_hash 不符。"
      fi
    fi
    as_user /bin/bash "$official_installer"
    rm -f -- "$official_installer"
    [[ -x "$agy" ]] || fail '官方安装器未在预期位置生成 agy。'
    installed_now=1
  fi

  echo
  echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
  echo ' 步骤 3/3：Google 账号授权'
  echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
  echo ''
  echo '📌 授权说明：'
  echo '  · 已有有效的 Google 授权将自动复用，无需重新登录。'
  echo '  · 首次授权或凭证失效时，终端会直接输出 Google 授权登录网址。'
  echo '  · 复制该链接并在浏览器打开，登录你的 Google 账号并获取授权码。'
  echo '  · 将授权码粘贴回下方终端按回车，系统将自动完成认证并直接继续！'
  echo ''

  # 自动预接受 agy 首次使用服务条款，避免条款弹窗打断
  local _onboarding_dir="$APP_HOME/.gemini/antigravity-cli/cache"
  if [[ ! -f "$_onboarding_dir/onboarding.json" ]]; then
    mkdir -p "$_onboarding_dir"
    cat > "$_onboarding_dir/onboarding.json" <<'_ONBOARDING_EOF'
{
  "consumerOnboardingComplete": true,
  "enterpriseOnboardingComplete": true,
  "onboardingComplete": true
}
_ONBOARDING_EOF
  fi

  local smoke_rc=10
  if [[ "$installed_now" == 0 && "$REAUTH" == 0 ]]; then
    if as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" smoke --config "$CANDIDATE" --allow-root; then
      smoke_rc=0
    else
      smoke_rc=$?
    fi
  fi
  if [[ "$smoke_rc" -ne 0 || "$REAUTH" == 1 ]]; then
    local auth_rc=0
    local _token_file="$APP_HOME/.gemini/antigravity-cli/antigravity-oauth-token"
    # 若已有凭证失效或主动重授，移开旧凭据以确保直接输出全新授权链接
    if [[ -f "$_token_file" ]]; then
      mv -f -- "$_token_file" "$_token_file.bak.$$" 2>/dev/null || rm -f -- "$_token_file"
    fi
    as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" auth-login --config "$CANDIDATE" || auth_rc=$?
    if [[ "$auth_rc" -ne 0 ]]; then
      [[ ! -f "$_token_file.bak.$$" ]] || mv -f -- "$_token_file.bak.$$" "$_token_file" 2>/dev/null || true
      fail 'Google 账号授权未完成或失败。'
    fi
    rm -f -- "$_token_file.bak.$$" 2>/dev/null || true
    if as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" smoke --config "$CANDIDATE" --allow-root; then
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
  echo "$commit" > "$release/.commit"
  echo "$commit" > "$CONFIG_DIR/current_commit" 2>/dev/null || true
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
  chown root:root "$CONFIG"; chmod 0600 "$CONFIG"
  /usr/bin/python3 -E -s -B "$release/manage.py" unit --config "$CONFIG" --user root > "$BACKUP/new.service"
  UNIT_CHANGED=1
  install -o root -g root -m 0644 "$BACKUP/new.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"

  echo '正在验证机器人初始化和长轮询就绪……'
  local ready=0 pid
  for _ in {1..180}; do
    pid="$(systemctl show "$SERVICE" -p MainPID --value 2>/dev/null || true)"
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null \
      && /usr/bin/python3 -E -s -B "$release/manage.py" check-ready \
        --file /run/agy-telegram-remote/ready.json --pid "${pid:-0}" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 0.5
  done
  [[ "$ready" == 1 ]] || fail '服务没有通过应用级就绪检查；请查看 journalctl 日志。'
  COMMITTED=1
  echo '============================================='
  echo ' 🎉 安装成功！后台守护服务已启动就绪'
  echo '============================================='
  echo "• ⚙️ 配置文件：$CONFIG"
  echo "• 📁 核心工作目录：$work"
  echo "• 👤 运行系统账户：$APP_USER"
  echo "• 🔖 部署版本 Commit：$commit"
  echo "• 💾 升级回退备份：$BACKUP (root 私有，包含旧配置，请勿上传)"
  echo "• 📋 查看服务状态：sudo systemctl status $SERVICE --no-pager"
  echo "• 📜 查看实时日志：sudo journalctl -u $SERVICE -f"
  echo '━━━━━━━━━━━━━━━━━━━━'
  echo '👉 部署完成！请打开 Telegram 向你的机器人私聊发送 /start 开始体验。'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
