# Repository instructions for coding agents

This is an authorized personal Telegram-to-agy bridge. Keep it simple: three installer
stages, one shared workspace, no web dashboard/database, and no automatic task reruns.

## Verify
Run `bash scripts/verify.sh` from the repository root. Runtime Python uses only the
standard library. Do not execute `install.sh` against the host just to run tests.
Never request, print, commit, or fabricate real Bot Tokens or Google credentials.

## Preserve
- Keep the shared JSON Runner for production and smoke tests.
- Preserve old settings on upgrades; only explicit opt-in changes old permission mode.
- Code and privileged installer helpers must not execute user-writable old venv code.
- Save results before sending. `/last` is retrieval only.
- Do not weaken whitelist, private-chat checks, output limits, process cleanup,
  symlink checks, or the documented security boundaries to make a test pass.
- Explicit administrator opt-in through AGY_HOST_ACCESS=full disables systemd
  host restrictions as documented in SECURITY.md; preserve this choice on upgrades.
- Keep unrelated repository content, attribution, community and promotion links.
- Do not claim a live Google/Telegram/systemd deployment was tested when it was not.
- Do not force-push or bypass branch protection. Report branch/PR/merge states accurately.

See SECURITY.md and docs/TESTING.md before changing behavior.
