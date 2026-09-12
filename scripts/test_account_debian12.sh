#!/usr/bin/env bash
# Debian 12 disposable environment test for root mode installation verification.
# MUST only be run in an isolated disposable container with explicit confirmation.
set -Eeuo pipefail

CONFIRMED=0
for arg in "$@"; do
  if [[ "$arg" == "--confirm-isolated-environment" ]]; then
    CONFIRMED=1
  fi
done

if [[ "$CONFIRMED" -ne 1 || "${AGY_TEST_ISOLATED_CONTAINER:-0}" != "1" ]]; then
  echo "错误：此脚本专用于在一次性隔离容器中验证 Debian 12 环境。" >&2
  echo "为防止误操作生产宿主机，必须在隔离容器中执行，并同时满足：" >&2
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

# Source install.sh for functions
# shellcheck disable=SC1091
source "$ROOT_DIR/install.sh"

echo "=== Test 1: Verify Debian 12 OS support check ==="
supported_os "debian" "12" || fail "supported_os 未能识别 debian 12"
echo "PASS: supported_os 成功验证 debian 12"

echo "=== Test 2: Verify Root mode defaults ==="
[[ "$APP_USER" == "root" ]] || fail "APP_USER 预期为 root，实际为 $APP_USER"
[[ "$APP_HOME" == "/root" ]] || fail "APP_HOME 预期为 /root，实际为 $APP_HOME"
echo "PASS: Root 模式默认配置项符合预期"

echo "=== Test 3: Verify as_user root environment ==="
env_output="$(as_user env)"
echo "$env_output" | grep -q "^HOME=/root$" || fail "as_user HOME 不为 /root"
echo "$env_output" | grep -q "^USER=root$" || fail "as_user USER 不为 root"
echo "$env_output" | grep -q "^LOGNAME=root$" || fail "as_user LOGNAME 不为 root"
echo "PASS: as_user 环境变量符合 root 模式预期"

echo "=== Test 4: Verify install.sh --help execution ==="
help_output="$("$ROOT_DIR/install.sh" --help)"
echo "$help_output" | grep -q "Antigravity Telegram Remote 安装与管理脚本" || fail "帮助输出异常"
echo "$help_output" | grep -q "极简 Root 模式" || fail "帮助输出未包含 Root 模式说明"
echo "PASS: install.sh --help 正常执行"

echo "=== All Debian 12 Root mode environment tests passed successfully! ==="
