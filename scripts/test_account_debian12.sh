#!/usr/bin/env bash
# Debian 12 disposable environment test for dedicated service account creation.
# MUST only be run in an isolated disposable container with explicit confirmation.
set -Eeuo pipefail

CONFIRMED=0
for arg in "$@"; do
  if [[ "$arg" == "--confirm-isolated-environment" ]]; then
    CONFIRMED=1
  fi
done

if [[ "$CONFIRMED" -ne 1 || "${AGY_TEST_ISOLATED_CONTAINER:-0}" != "1" ]]; then
  echo "错误：此脚本会在当前系统上实际执行账户与组管理命令。" >&2
  echo "为防止误操作生产宿主机，必须在一次性隔离容器中执行，并同时满足：" >&2
  echo "  1. 传入参数: --confirm-isolated-environment" >&2
  echo "  2. 环境变量: AGY_TEST_ISOLATED_CONTAINER=1" >&2
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "错误：此测试必须以 root 身份在隔离容器中执行。" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# Generate a dedicated random suffix for this test run.
RAND_SUFFIX="$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c 8 || true)"
if [[ -z "$RAND_SUFFIX" || ${#RAND_SUFFIX} -lt 4 ]]; then
  RAND_SUFFIX="t${RANDOM}${BASHPID}"
fi

TEST_USER="test-agy-${RAND_SUFFIX}"
TEST_HOME="/home/${TEST_USER}"
EXTRA_GROUP_USERS="users"
EXTRA_GROUP_CUSTOM="test-agy-grp-${RAND_SUFFIX}"

# Strict safety bounds: Never touch real production resources
if [[ "$TEST_USER" == "agy-tg" || "$TEST_HOME" == "/home/agy-tg" ]]; then
  echo "安全错误：测试账户或目录绝不允许为生产路径。" >&2
  exit 1
fi

# If test account, group or home already exists, reject immediately instead of deleting.
if id "$TEST_USER" >/dev/null 2>&1; then
  echo "错误：测试账户 $TEST_USER 已存在，拒绝运行以防破坏已有资源。" >&2
  exit 1
fi
if getent group "$TEST_USER" >/dev/null 2>&1; then
  echo "错误：测试用户组 $TEST_USER 已存在，拒绝运行以防破坏已有资源。" >&2
  exit 1
fi
if [[ -e "$TEST_HOME" ]]; then
  echo "错误：测试目录 $TEST_HOME 已存在，拒绝运行以防破坏已有资源。" >&2
  exit 1
fi

# Resource tracking for strictly scoped cleanup
CREATED_USER=""
CREATED_GROUP=""
CREATED_EXTRA_GROUPS=()
CREATED_HOME=""

cleanup() {
  local ec=$?
  trap - EXIT INT TERM
  # Only clean up resources created during this run matching test prefix
  if [[ -n "$CREATED_USER" ]] && id "$CREATED_USER" >/dev/null 2>&1; then
    if [[ "$CREATED_USER" =~ ^test-agy- ]]; then
      userdel -r "$CREATED_USER" 2>/dev/null || userdel "$CREATED_USER" 2>/dev/null || true
    fi
  fi
  if [[ -n "$CREATED_GROUP" ]] && getent group "$CREATED_GROUP" >/dev/null 2>&1; then
    if [[ "$CREATED_GROUP" =~ ^test-agy- ]]; then
      groupdel "$CREATED_GROUP" 2>/dev/null || true
    fi
  fi
  for g in "${CREATED_EXTRA_GROUPS[@]}"; do
    if [[ "$g" =~ ^test-agy- ]] && getent group "$g" >/dev/null 2>&1; then
      groupdel "$g" 2>/dev/null || true
    fi
  done
  if [[ -n "$CREATED_HOME" && -d "$CREATED_HOME" ]]; then
    if [[ "$CREATED_HOME" =~ ^/home/test-agy- || "$CREATED_HOME" =~ ^/tmp/test-agy- ]]; then
      rm -rf -- "$CREATED_HOME"
    fi
  fi
  exit "$ec"
}
trap cleanup EXIT INT TERM

# Source install.sh for functions
# shellcheck source=install.sh
source "$ROOT_DIR/install.sh"

# Override APP_USER and APP_HOME with dedicated test resources
APP_USER="$TEST_USER"
APP_HOME="$TEST_HOME"

echo "=== Test 1: Fresh service account creation with dedicated test user ($APP_USER) ==="
ensure_service_account

# Record that the user was created
CREATED_USER="$APP_USER"
CREATED_GROUP="$APP_USER"
CREATED_HOME="$APP_HOME"

id "$APP_USER" >/dev/null 2>&1 || fail "测试账户 $APP_USER 未能成功创建。"
user_home="$(getent passwd "$APP_USER" | cut -d: -f6)"
[[ "$user_home" == "$APP_HOME" ]] || fail "Home 路径不符：预期 $APP_HOME，实际 $user_home"
user_uid="$(id -u "$APP_USER")"
(( user_uid >= 100 )) || fail "UID $user_uid 小于 100"
(( user_uid != 0 )) || fail "UID 绝不能为 0"
primary_group="$(id -gn "$APP_USER")"
[[ "$primary_group" == "$APP_USER" ]] || fail "主组不符：预期 $APP_USER，实际 $primary_group"
user_all_groups="$(id -Gn "$APP_USER")"
[[ "$user_all_groups" == "$APP_USER" ]] || fail "账户不应包含额外组，实际所属组为: $user_all_groups"
echo "PASS: 专有测试账户创建成功 (UID=$user_uid, Home=$user_home, 仅同名组=$user_all_groups)"

echo "=== Test 2: Repeated runs preserve existing account UID ==="
initial_uid="$user_uid"
ensure_service_account
subsequent_uid="$(id -u "$APP_USER")"
[[ "$initial_uid" == "$subsequent_uid" ]] || fail "重复运行修改了已有 UID：从 $initial_uid 变为 $subsequent_uid"
echo "PASS: 重复运行保持已有 UID 不变 ($initial_uid)"

echo "=== Test 3: Reject account with supplementary group 'users' and show hint ==="
if ! getent group "$EXTRA_GROUP_USERS" >/dev/null 2>&1; then
  groupadd "$EXTRA_GROUP_USERS"
fi
usermod -a -G "$EXTRA_GROUP_USERS" "$APP_USER"

set +e
output="$(ensure_service_account 2>&1)"
exit_code="$?"
set -e

if [[ "$exit_code" -eq 0 ]]; then
  fail "拥有 users 附加组时未按预期被拦截"
fi
if [[ "$output" != *"实际组："* || "$output" != *"$EXTRA_GROUP_USERS"* ]]; then
  fail "错误输出未打印包含 users 的实际组名单：$output"
fi
if [[ "$output" != *"gpasswd -d $APP_USER users"* ]]; then
  fail "错误输出未包含 gpasswd -d $APP_USER users 提示：$output"
fi
echo "PASS: 存在 users 附加组时成功拦截并正确输出实际组与 gpasswd 提示"

# Cleanly remove users group for next subtest without || true
gpasswd -d "$APP_USER" "$EXTRA_GROUP_USERS" >/dev/null

echo "=== Test 4: Reject account with unknown supplementary group without users hint ==="
groupadd "$EXTRA_GROUP_CUSTOM"
CREATED_EXTRA_GROUPS+=("$EXTRA_GROUP_CUSTOM")
usermod -a -G "$EXTRA_GROUP_CUSTOM" "$APP_USER"

set +e
output="$(ensure_service_account 2>&1)"
exit_code="$?"
set -e

if [[ "$exit_code" -eq 0 ]]; then
  fail "拥有未知附加组时未按预期被拦截"
fi
if [[ "$output" != *"实际组："* || "$output" != *"$EXTRA_GROUP_CUSTOM"* ]]; then
  fail "错误输出未打印实际组名单：$output"
fi
if [[ "$output" == *"gpasswd -d $APP_USER users"* ]]; then
  fail "未知附加组不应显示 users 移除提示：$output"
fi
echo "PASS: 存在未知附加组时成功拦截并打印实际组，未泄露 users 提示"

echo "=== All Debian 12 service account tests passed successfully! ==="
