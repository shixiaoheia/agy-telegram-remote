#!/usr/bin/env bash
# Debian 12 disposable environment test for dedicated service account creation.
# Must be run in a disposable Debian 12 container as root.
set -Eeuo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "Error: This test script must be run as root in a disposable Debian 12 container." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=install.sh
source "$ROOT_DIR/install.sh"

echo "=== Test 1: Fresh service account creation in Debian 12 ==="
if id "$APP_USER" >/dev/null 2>&1; then
  userdel -r "$APP_USER" 2>/dev/null || userdel "$APP_USER" 2>/dev/null || true
fi
if getent group "$APP_USER" >/dev/null 2>&1; then
  groupdel "$APP_USER" 2>/dev/null || true
fi

ensure_service_account

id "$APP_USER" >/dev/null 2>&1 || fail "Account was not created."
user_home="$(getent passwd "$APP_USER" | cut -d: -f6)"
[[ "$user_home" == "$APP_HOME" ]] || fail "Expected home $APP_HOME, got $user_home"
user_uid="$(id -u "$APP_USER")"
(( user_uid >= 100 )) || fail "UID $user_uid is less than 100"
(( user_uid != 0 )) || fail "UID cannot be 0"
primary_group="$(id -gn "$APP_USER")"
[[ "$primary_group" == "$APP_USER" ]] || fail "Expected primary group $APP_USER, got $primary_group"
all_groups="$(id -Gn "$APP_USER")"
[[ "$all_groups" == "$APP_USER" ]] || fail "Expected only $APP_USER group, got: $all_groups"
echo "PASS: New service account created with UID $user_uid, home $user_home, only own group: $all_groups."

echo "=== Test 2: Repeated runs preserve existing account UID ==="
initial_uid="$user_uid"
ensure_service_account
subsequent_uid="$(id -u "$APP_USER")"
[[ "$initial_uid" == "$subsequent_uid" ]] || fail "UID changed from $initial_uid to $subsequent_uid on repeat run"
echo "PASS: Repeated run preserved UID $initial_uid."

echo "=== Test 3: Reject account with supplementary group 'users' and show hint ==="
groupadd -f users
usermod -a -G users "$APP_USER"

set +e
output="$(ensure_service_account 2>&1)"
exit_code="$?"
set -e

if [[ "$exit_code" -eq 0 ]]; then
  fail "Expected failure when user has 'users' supplementary group, but succeeded"
fi
if [[ "$output" != *"实际组："* || "$output" != *"users"* ]]; then
  fail "Expected error output to display actual group list containing 'users', got: $output"
fi
if [[ "$output" != *"gpasswd -d $APP_USER users"* ]]; then
  fail "Expected hint for 'gpasswd -d $APP_USER users', got: $output"
fi
echo "PASS: Rejected account with 'users' group and displayed correct hint and group list."

echo "=== Test 4: Reject account with unknown supplementary group without users hint ==="
gpasswd -d "$APP_USER" users >/dev/null 2>&1 || true
groupadd -f testdummy
usermod -a -G testdummy "$APP_USER"

set +e
output="$(ensure_service_account 2>&1)"
exit_code="$?"
set -e

if [[ "$exit_code" -eq 0 ]]; then
  fail "Expected failure when user has unknown supplementary group, but succeeded"
fi
if [[ "$output" != *"实际组："* || "$output" != *"testdummy"* ]]; then
  fail "Expected error output to display actual group list containing 'testdummy', got: $output"
fi
if [[ "$output" == *"gpasswd -d $APP_USER users"* ]]; then
  fail "Unexpected 'users' hint displayed for unknown group: $output"
fi
echo "PASS: Rejected account with unknown group, displayed actual group list, and withheld users hint."

# Cleanup test account in disposable container
userdel -r "$APP_USER" 2>/dev/null || userdel "$APP_USER" 2>/dev/null || true
groupdel testdummy 2>/dev/null || true

echo "=== All Debian 12 service account tests passed! ==="
