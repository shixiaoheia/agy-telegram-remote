#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PYTHON="${PYTHON:-python3}"
"$PYTHON" --version
bash -n install.sh
bash -n scripts/verify.sh
bash -n scripts/test_account_debian12.sh
"$PYTHON" -B - <<'PY'
import ast
from pathlib import Path
files = list(Path(".").glob("*.py")) + list(Path("tests").glob("*.py"))
for path in files:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))
print(f"AST syntax check: {len(files)} Python files passed (Python 3.10 grammar)")
PY
"$PYTHON" -B -m unittest discover -s tests -v
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck --severity=warning install.sh scripts/verify.sh scripts/test_account_debian12.sh
else
  echo 'SKIP: ShellCheck is not installed (GitHub CI installs it).'
fi
