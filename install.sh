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
RUNTIME_DIR=/run/agy-telegram-remote
INPUT_DIR="$APP_HOME/.agy-telegram-inputs"
AGY_MANAGED_MARKER=$CONFIG_DIR/managed-agy-path
DEPLOY_LOCK_FILE="${DEPLOY_LOCK_FILE:-/run/lock/agy-telegram-remote-deploy.lock}"

REF=main
ENABLE_AUTO=0
HOST_ACCESS=
WRITE_PATHS=
WRITE_PATHS_SET=0
REAUTH=0
RECONFIGURE=0
MODE=menu
PURGE=0
NEW_PROXY=0
TRANSACTION=0
COMMITTED=0
SWITCHED=0
CONFIG_CHANGED=0
UNIT_CHANGED=0
COMMIT_CHANGED=0
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
  bash install.sh                         显示安装、更新与卸载菜单
  bash install.sh --menu                  显示管理菜单（安装、更新、彻底卸载、退出）
  bash install.sh --reconfigure           安装/更新并重新询问 Token 与白名单
  bash install.sh --install               👑 极简 Root 模式安装/更新（直接以 root 运行）
  bash install.sh --root                  👑 极简 Root 模式安装/更新（快捷别名）
  bash install.sh --enable-auto-approve    更新时明确启用自动审批（跳过权限确认弹窗）
  bash install.sh --full-host-access       启用完整主机 Root 权限与工具自动审批
  bash install.sh --restricted-host-access 恢复 systemd 目录和能力限制
  bash install.sh --write-paths /etc/nginx,/opt/my-app  明确开放指定已有目录的写入权限
  bash install.sh --write-paths ''         清空额外可写目录（普通更新保留原设置）
  bash install.sh --reauth                 重新进入 Google 账号授权流程
  bash install.sh --ref COMMIT_OR_BRANCH   安装/升级指定不可变 Git 提交 SHA 或分支（推荐生产使用 Commit SHA）
  bash install.sh --uninstall              安全卸载服务（仅停止并移除服务，保留数据与配置）
  bash install.sh --uninstall --purge      彻底清理本项目组件（需输入 y 确认）
  bash install.sh --proxy                  添加或更换本项目代理节点
  bash install.sh --help                   显示此帮助说明
EOF
}

# This menu only selects a mode. No credentials or system changes happen here.
choose_operation() {
  local choice rc
  echo '============================================='
  echo ' Antigravity Telegram Remote 管理菜单'
  echo '============================================='
  echo '地区提示：Antigravity 仅在 Google 支持的国家或地区提供。'
  echo '例如美国、日本、新加坡在官方名单中；香港目前未列入。'
  echo '完整名单及账号所属地区说明：https://antigravity.google/docs/faq'
  echo '网络出口和账号资格都可能影响使用，代理不能保证开通资格。'
  echo
  if [[ -f "$CONFIG" || -L "$APP" || -f "$APP/.env" ]]; then
    echo '检测到已有安装；更新将保留现有配置。'
    echo '  1) 安装或修复（沿用现有配置）'
    echo '  2) 更新到最新版本（推荐，保留现有配置）'
  else
    echo '未检测到已有安装；首次使用请选择安装。'
    echo '  1) 安装（推荐，首次配置）'
    echo '  2) 更新（使用同一部署流程）'
  fi
  echo '  3) 彻底卸载（查看清理范围并确认）'
  echo '  4) 添加或更换代理节点'
  echo '  0) 退出'
  echo
  while true; do
    printf '请输入选项 [0/1/2/3/4]： '
    if IFS= read -r choice; then
      case "$choice" in
        1) MODE=install; return 0 ;;
        # 安装和更新走同一套原子部署流程；更新会自动保留现有配置。
        2) MODE=install; return 0 ;;
        3) MODE=uninstall; PURGE=1; return 0 ;;
        4) MODE=proxy; return 0 ;;
        0)
          MODE="exit"
          echo '已退出，未开始任何安装或卸载操作。'
          return 0
          ;;
        *) echo '请输入 1、2、3、4 或 0，然后回车。' ;;
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

choose_initial_proxy() {
  local answer
  echo '如当前网络需要代理，可输入自己的节点链接。节点仅供本项目使用。'
  echo '首次下载项目和代理核心仍需要能访问 GitHub。'
  while true; do
    if ! IFS= read -r -p '现在配置代理节点？[y/n]： ' answer; then echo; return 1; fi
    case "$answer" in
      y|Y) NEW_PROXY=1; return 0 ;;
      n|N) NEW_PROXY=0; return 0 ;;
      *) echo '请输入 y 或 n。' ;;
    esac
  done
}

load_managed_proxy() {
  local env_file="$CONFIG_DIR/proxy.env" port
  [[ -e "$env_file" || -L "$env_file" ]] || return 0
  [[ -f "$env_file" && ! -L "$env_file" && "$(stat -c %u "$env_file")" == 0 ]] \
    || fail '代理环境文件不安全。'
  port="$(/usr/bin/python3 -I - "$env_file" <<'PY'
import os, re, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as f:
    st = os.fstat(f.fileno())
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077 or st.st_size > 4096:
        sys.exit(1)
    text = f.read(4097).decode('ascii')
m = re.fullmatch(r'HTTP_PROXY=http://127\.0\.0\.1:([0-9]{1,5})\nHTTPS_PROXY=http://127\.0\.0\.1:\1\nhttp_proxy=http://127\.0\.0\.1:\1\nhttps_proxy=http://127\.0\.0\.1:\1\nNO_PROXY=localhost,127\.0\.0\.1,::1\nno_proxy=localhost,127\.0\.0\.1,::1\nALL_PROXY=\nall_proxy=\n', text)
if not m or not 1 <= int(m.group(1)) <= 65535:
    sys.exit(1)
print(m.group(1))
PY
)" || fail '代理环境文件无效，请检查后重试。'
  if ! systemctl is-active --quiet agy-telegram-proxy.service; then
    systemctl start agy-telegram-proxy.service || fail '本项目代理服务未能启动，请先检查或更换节点。'
  fi
  export HTTP_PROXY="http://127.0.0.1:$port" HTTPS_PROXY="http://127.0.0.1:$port"
  export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
  export NO_PROXY='localhost,127.0.0.1,::1' no_proxy='localhost,127.0.0.1,::1'
  export ALL_PROXY='' all_proxy=''
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

report_service_state() {
  local state
  if ! state="$(systemctl show "$SERVICE" -p ActiveState --value 2>/dev/null)"; then
    state='unknown'
  fi
  case "$state" in
    active) echo '当前服务状态：运行中。' >&2 ;;
    inactive) echo '当前服务状态：未运行。' >&2 ;;
    failed) echo '当前服务状态：启动失败，请查看服务日志。' >&2 ;;
    *) echo '当前服务状态：无法确认，请检查 systemctl 状态。' >&2 ;;
  esac
  echo "查看状态：systemctl status $SERVICE --no-pager；查看日志：journalctl -u $SERVICE -n 100 --no-pager" >&2
}

rollback() {
  echo '部署未通过验证，正在恢复旧配置、旧服务和旧程序入口……' >&2
  systemctl stop "$SERVICE" >/dev/null 2>&1 || true
  if [[ "$COMMIT_CHANGED" == 1 ]]; then
    if [[ -f "$BACKUP/current_commit" ]]; then
      cp -p -- "$BACKUP/current_commit" "$CONFIG_DIR/current_commit"
    else
      rm -f -- "$CONFIG_DIR/current_commit"
    fi
  fi
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
  report_service_state
}

write_commit_record() {
  local commit="$1" marker="$CONFIG_DIR/current_commit" temporary
  [[ ! -L "$marker" && ( ! -e "$marker" || -f "$marker" ) ]] \
    || fail '版本记录文件类型异常。'
  temporary="$(mktemp "$CONFIG_DIR/.current-commit-XXXXXXXX")" || return 1
  if ! printf '%s\n' "$commit" > "$temporary" || ! chmod 0600 "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  COMMIT_CHANGED=1
  if ! mv -f -- "$temporary" "$marker"; then
    rm -f -- "$temporary"
    return 1
  fi
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  set +e
  if [[ "$TRANSACTION" == 1 && "$COMMITTED" == 0 ]]; then
    rollback
    (( rc != 0 )) || rc=1
  elif (( rc != 0 )) && [[ "$COMMITTED" == 0 ]]; then
    echo '尚未进入服务切换阶段；安装器未停止旧服务。' >&2
    report_service_state
  fi
  if [[ -n "$CANDIDATE" && -f "$CANDIDATE" ]]; then
    rm -f -- "$CANDIDATE"
  fi
  exit "$rc"
}

find_input_dir() {
  # Parse configuration as data; never execute an old checkout or source its env file.
  local source_config=''
  [[ ! -L "$CONFIG" ]] || fail '配置文件是符号链接，无法安全确认附件目录。'
  if [[ -f "$CONFIG" ]]; then
    source_config="$CONFIG"
  elif [[ ! -L "$APP" && -f "$APP/.env" ]]; then
    source_config="$APP/.env"
  fi
  /usr/bin/python3 -I - "$source_config" "$APP_HOME" <<'PY'
import os, re, shlex, stat, sys
from pathlib import Path
try:
    config, home_arg = sys.argv[1:]
    home = Path(home_arg)
    work = home
    if config:
        fd = os.open(config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise ValueError('unsafe config')
            content = stream.read(65537).decode('utf-8-sig')
        found = []
        for line in content.splitlines():
            line = line.strip()
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, sep, value = line.partition('=')
            if sep and key.strip() == 'AGY_WORKSPACE':
                value = value.strip()
                if value.startswith(("'", '"')):
                    parts = shlex.split(value, comments=True, posix=True)
                    if len(parts) != 1:
                        raise ValueError('bad workspace')
                    value = parts[0]
                else:
                    value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
                found.append(value)
        if len(found) > 1:
            raise ValueError('duplicate workspace')
        if found:
            work = Path(found[0])
    if not re.fullmatch(r'/[A-Za-z0-9._/-]+', str(work)) or '..' in work.parts:
        raise ValueError('bad workspace')
    if work != home and home not in work.parents:
        raise ValueError('workspace outside home')
    current = home
    for part in ((), *[(p,) for p in work.relative_to(home).parts], ('.agy-telegram-inputs',)):
        if part:
            current = current / part[0]
        if os.path.lexists(current):
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError('unsafe directory')
    print(current)
except (OSError, UnicodeError, ValueError):
    print('无法安全识别附件目录，请检查配置和路径后重试。', file=sys.stderr)
    sys.exit(1)
PY
}

confirm_service_stop() {
  local pid children answer
  pid="$(systemctl show "$SERVICE" -p MainPID --value 2>/dev/null || true)"
  children=''
  if [[ ! "$pid" =~ ^[1-9][0-9]*$ || ! -r "/proc/$pid/task/$pid/children" ]]; then
    echo '无法确认服务子进程状态，可能有任务正在运行。'
  else
    IFS= read -r children < "/proc/$pid/task/$pid/children" || true
    if [[ -n "$children" ]]; then
      echo '检测到服务子进程，可能有任务或模型检测正在运行。'
    else
      echo '未检测到服务子进程；仍可能有正在准备或新开始的任务。'
    fi
  fi
  echo '更新将停止服务，中断的任务不会自动重跑，已产生的修改不会撤销。'
  while true; do
    if ! IFS= read -r -p '继续更新？[y/n]： ' answer; then echo; return 1; fi
    case "$answer" in
      y|Y) return 0 ;;
      n|N) return 1 ;;
      *) echo '请输入 y 或 n。' ;;
    esac
  done
}

# A previous installation may not contain the proxy helper, and updating it can
# itself require a proxy. Fetch a fresh root-owned helper before changing the
# existing service; do not execute Python from the old installation.
run_proxy_setup() (
  local proxy_stage commit
  command -v git >/dev/null || fail '配置节点需要 git；请先安装 git 并确保能访问 GitHub。'
  root_dir "$RELEASES"
  proxy_stage="$(mktemp -d "$RELEASES/.proxy-stage-XXXXXXXX")"
  trap 'rm -rf -- "$proxy_stage"' EXIT
  git -c core.hooksPath=/dev/null init -q "$proxy_stage"
  git -c core.hooksPath=/dev/null -c protocol.file.allow=never -C "$proxy_stage" \
    fetch -q --depth=1 "$REPO" "$REF"
  git -c core.hooksPath=/dev/null -C "$proxy_stage" checkout -q --detach FETCH_HEAD
  commit="$(git -C "$proxy_stage" rev-parse HEAD)"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail '无法确认代理管理程序版本。'
  [[ -z "$(find "$proxy_stage" -path "$proxy_stage/.git" -prune -o -type l -print -quit)" ]] \
    || fail '代理管理程序含符号链接，已停止。'
  rm -rf -- "$proxy_stage/.git"
  find "$proxy_stage" -type d -exec chmod 0755 {} +
  find "$proxy_stage" -type f -exec chmod 0644 {} +
  validate_release_files "$proxy_stage"
  [[ -f "$proxy_stage/proxy_manager.py" && -f "$proxy_stage/proxy_node.py" ]] \
    || fail '所选版本不支持代理节点。'
  echo "代理管理程序版本：$commit"
  execute_proxy_helper "$proxy_stage"
)

execute_proxy_helper() {
  /usr/bin/python3 -E -s -B "$1/proxy_manager.py"
}

needs_auth() {
  [[ "$1" -eq 10 || "$2" == 1 || "$3" == 1 ]]
}

validate_release_files() {
  local stage="$1" required
  for required in bot.py settings.py state_store.py telegram_api.py telegram_formatting.py agy_runner.py manage.py; do
    [[ -f "$stage/$required" ]] || fail "所选提交缺少 $required；请先上传完整新版。"
  done
  # Older releases embedded command handlers in bot.py and legitimately lack
  # this module. Require it only when the selected release imports it.
  if grep -Eq '^[[:space:]]*(from[[:space:]]+command_handlers[[:space:]]+import|import[[:space:]]+command_handlers([[:space:]]|$))' "$stage/bot.py"; then
    [[ -f "$stage/command_handlers.py" ]] || fail '所选提交缺少 command_handlers.py。'
  fi
}

uninstall() {
  local answer managed_agy='' directory configured_input='' managed_proxy=0 managed_engine=0 managed_drop=0
  local proxy_unit=/etc/systemd/system/agy-telegram-proxy.service
  local proxy_drop=/etc/systemd/system/agy-telegram-remote.service.d/90-agy-managed-proxy.conf
  local proxy_engine=/opt/agy-telegram-remote-proxy
  [[ ! -L "$proxy_unit" && ! -L "$(dirname -- "$proxy_drop")" && ! -L "$proxy_drop" && ! -L "$proxy_engine" ]] \
    || fail '代理相关路径是符号链接，请人工检查。'
  if [[ -f "$proxy_unit" ]] && head -n 1 "$proxy_unit" | grep -Fxq '# Managed by agy-telegram-remote; do not edit.'; then
    [[ "$(stat -c %u "$proxy_unit")" == 0 ]] || fail '代理服务文件不受 root 管理。'
    managed_proxy=1
  fi
  if [[ -f "$proxy_drop" ]] && head -n 1 "$proxy_drop" | grep -Fxq '# Managed by agy-telegram-remote; do not edit.'; then
    [[ "$(stat -c %u "$proxy_drop")" == 0 ]] || fail '代理服务配置不受 root 管理。'
    managed_drop=1
  fi
  if [[ "$managed_proxy" == 1 && -e "$proxy_drop" && "$managed_drop" == 0 ]]; then
    fail '代理服务配置并非本项目管理，请人工检查。'
  fi
  if [[ -d "$proxy_engine" ]]; then
    [[ ! -L "$proxy_engine/.agy-managed" ]] || fail '代理核心标记是符号链接。'
    if [[ -f "$proxy_engine/.agy-managed" && "$(stat -c %u "$proxy_engine")" == 0 \
          && "$(stat -c %u "$proxy_engine/.agy-managed")" == 0 ]] \
          && [[ "$(cat "$proxy_engine/.agy-managed")" == 'agy-telegram-proxy-v1' ]]; then
      local engine_perms marker_perms
      engine_perms="$(stat -c %a "$proxy_engine")"
      marker_perms="$(stat -c %a "$proxy_engine/.agy-managed")"
      (( (8#$engine_perms & 0022) == 0 && (8#$marker_perms & 0077) == 0 )) \
        || fail '代理核心目录或标记权限不安全。'
      managed_engine=1
    fi
    [[ "$managed_proxy" == 0 || "$managed_engine" == 1 ]] || fail '代理核心目录并非本项目管理。'
  fi
  if [[ "$PURGE" == 1 ]]; then
    configured_input="$(find_input_dir)" || fail '未能安全确认附件清理范围；未停止服务。'
    if [[ -f "$AGY_MANAGED_MARKER" && ! -L "$AGY_MANAGED_MARKER" ]]; then
      IFS= read -r managed_agy < "$AGY_MANAGED_MARKER" || true
      [[ "$managed_agy" == "$APP_HOME/.local/bin/agy" ]] || managed_agy=''
    fi
    echo '将删除服务、程序、配置、任务记录、备份、运行文件和 Bot 临时附件。'
    [[ "$managed_proxy" == 0 ]] || echo '还将删除本项目管理的代理服务、节点凭据及代理核心。'
    if [[ -n "$managed_agy" ]]; then
      echo '还将删除本安装器安装的 agy 和对应的 Google 登录资料。'
    else
      echo '将保留独立安装或来源不明的 agy 与 Google 登录资料。'
    fi
    echo '保留工作目录及用户文件、systemd 自定义配置、系统日志和 BotFather 中的 Bot。'
    echo "将清理附件目录：$configured_input"
    [[ "$configured_input" == "$INPUT_DIR" ]] || echo "也将清理默认附件目录：$INPUT_DIR"
  else
    echo '将移除 systemd 服务；程序、配置、工作目录及 Google 登录会保留。'
  fi
  while true; do
    if ! IFS= read -r -p '确认继续？[y/n]： ' answer; then
      echo; echo '已取消，没有卸载任何内容。'; return 0
    fi
    case "$answer" in
      y|Y) break ;;
      n|N) echo '已取消，没有卸载任何内容。'; return 0 ;;
      *) echo '请输入 y 或 n。' ;;
    esac
  done
  [[ ! -L "$UNIT" ]] || fail "服务文件是链接，请人工检查。"
  if [[ "$PURGE" == 1 ]]; then
    for directory in "$RELEASES" "$CONFIG_DIR" "$STATE_BASE" "$BACKUPS" "$RUNTIME_DIR" "$INPUT_DIR" "$configured_input"; do
      [[ ! -L "$directory" ]] || fail "拒绝清理符号链接：$directory"
    done
    if [[ -n "$managed_agy" ]]; then
      for directory in "$APP_HOME/.local" "$APP_HOME/.local/bin" "$managed_agy" "$APP_HOME/.gemini" "$APP_HOME/.gemini/antigravity-cli"; do
        [[ ! -L "$directory" ]] || fail "拒绝清理符号链接：$directory"
      done
    fi
  fi
  systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    fail '服务未能停止；未移除服务文件。'
  fi
  if [[ "$managed_proxy" == 1 ]]; then
    systemctl disable --now agy-telegram-proxy.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet agy-telegram-proxy.service 2>/dev/null; then
      fail '代理服务未能停止，请人工检查。'
    fi
    if [[ "$PURGE" == 1 ]]; then
      rm -f -- "$proxy_unit"
    fi
  fi
  if [[ "$PURGE" == 1 ]]; then
    [[ "$managed_drop" == 0 ]] || rm -f -- "$proxy_drop"
    [[ "$managed_engine" == 0 ]] || rm -rf -- "$proxy_engine"
  fi
  rm -f -- "$UNIT"
  systemctl daemon-reload
  if [[ "$PURGE" == 0 ]]; then
    echo '服务已移除，所有程序和数据保留。'
    return
  fi
  rm -rf -- "$APP" "$RELEASES" "$CONFIG_DIR" "$STATE_BASE" "$BACKUPS" "$RUNTIME_DIR" "$INPUT_DIR" "$configured_input"
  if [[ -n "$managed_agy" ]]; then
    rm -f -- "$managed_agy"
    rm -rf -- "$APP_HOME/.gemini/antigravity-cli"
  fi
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  echo '清理完成。为保证操作互斥，保留不含凭据的部署锁文件。'
}

main() {
  local original_args=("$@")
  local direct_install=0
  while (( $# )); do
    case "$1" in
      --help|-h) usage; return ;;
      --menu) : ;;
      --reconfigure) RECONFIGURE=1; direct_install=1 ;;
      --install|--root) MODE=install; direct_install=1 ;;
      --enable-auto-approve) ENABLE_AUTO=1; direct_install=1 ;;
      --full-host-access) HOST_ACCESS=full; ENABLE_AUTO=1; direct_install=1 ;;
      --restricted-host-access) HOST_ACCESS=restricted; direct_install=1 ;;
      --write-paths)
        (( $# >= 2 )) || fail '--write-paths 缺少值。'
        WRITE_PATHS="$2"; WRITE_PATHS_SET=1; shift
        direct_install=1
        ;;
      --reauth) REAUTH=1; direct_install=1 ;;
      --uninstall) MODE=uninstall ;;
      --proxy) MODE=proxy ;;
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
  # Explicit modes bypass the menu; bare invocation always displays it.
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
  if [[ "$MODE" == proxy ]]; then
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null && ! confirm_service_stop; then
      echo '已取消更换节点，原服务保持运行。'
      return 0
    fi
    run_proxy_setup
    return
  fi

  load_managed_proxy
  if [[ ! -e "$CONFIG" && ! -L "$APP" && ! -f "$APP/.env" && ! -f "$CONFIG_DIR/proxy.env" ]]; then
    choose_initial_proxy || fail '已取消安装。'
  fi

  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  echo '============================================='
  echo ' Antigravity Telegram Remote 极简一键安装向导'
  echo '============================================='
  echo '地区提示：美国、日本、新加坡等在 Antigravity 官方支持名单内；香港目前未列入。'
  echo '请查看 https://antigravity.google/docs/faq 了解完整名单及账号资格。'
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
  [[ ! -L "$CONFIG_DIR/current_commit" && ( ! -e "$CONFIG_DIR/current_commit" || -f "$CONFIG_DIR/current_commit" ) ]] \
    || fail '版本记录文件类型异常。'
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
  validate_release_files "$STAGE"
  echo "采用提交：$commit"

  if [[ "$NEW_PROXY" == 1 ]]; then
    [[ -f "$STAGE/proxy_manager.py" && -f "$STAGE/proxy_node.py" ]] \
      || fail '所选版本不支持代理节点。'
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
      fail '检测到同名服务正在运行，请先检查已有安装，再通过菜单更换节点。'
    fi
    /usr/bin/python3 -E -s -B "$STAGE/proxy_manager.py" || fail '节点配置未完成，安装已停止。'
    load_managed_proxy
  fi

  CANDIDATE="$(mktemp "$CONFIG_DIR/.candidate-XXXXXXXX")"
  chown root:root "$CANDIDATE"; chmod 0600 "$CANDIDATE"
  local prepare=(prepare-config --output "$CANDIDATE" --home "$APP_HOME")
  if [[ -f "$CONFIG" ]]; then
    prepare+=(--old "$CONFIG")
  elif [[ -f "$APP/.env" ]]; then
    prepare+=(--old "$APP/.env")
  fi
  [[ "$RECONFIGURE" == 0 ]] || prepare+=(--reconfigure)
  [[ "$ENABLE_AUTO" == 0 ]] || prepare+=(--enable-auto)
  [[ -z "$HOST_ACCESS" ]] || prepare+=(--host-access "$HOST_ACCESS")
  [[ "$WRITE_PATHS_SET" == 0 ]] || prepare+=(--write-paths "$WRITE_PATHS")
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

  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    if ! confirm_service_stop; then
      echo '已取消更新，旧服务保持运行。'
      rm -rf -- "$STAGE"
      STAGE=
      return 0
    fi
  fi
  BACKUP="$(mktemp -d "$BACKUPS/deploy-XXXXXXXX")"
  [[ ! -f "$CONFIG" ]] || cp -p -- "$CONFIG" "$BACKUP/config.env"
  [[ ! -f "$UNIT" ]] || cp -p -- "$UNIT" "$BACKUP/service"
  [[ ! -f "$CONFIG_DIR/current_commit" ]] || cp -p -- "$CONFIG_DIR/current_commit" "$BACKUP/current_commit"
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
  if needs_auth "$smoke_rc" "$REAUTH" "$installed_now"; then
    local auth_rc=0
    local _token_file="$APP_HOME/.gemini/antigravity-cli/antigravity-oauth-token"
    # 若已有凭证失效或主动重授，移开旧凭据以确保直接输出全新授权链接
    if [[ -f "$_token_file" ]]; then
      mv -f -- "$_token_file" "$_token_file.bak.$$" || fail '无法暂存旧授权资料，已停止并保留原文件。'
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
  write_commit_record "$commit"
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
  if [[ "$installed_now" == 1 ]]; then
    printf '%s\n' "$agy" > "$AGY_MANAGED_MARKER"
    chown root:root "$AGY_MANAGED_MARKER"; chmod 0600 "$AGY_MANAGED_MARKER"
  fi
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

