# Antigravity Telegram Remote

[中文](README.md) | English

Control the Google Antigravity CLI (`agy`) in a private Telegram chat on your VPS. The bot runs tasks in a shared workspace and sends the results back to Telegram.

> This is a tool for you and people you fully trust. It can execute tasks on your server. Keep the bot token and whitelist private.

**Current release: [v2026.11.13.2](https://github.com/shixiaoheia/agy-telegram-remote/releases/tag/v2026.11.13.2)** · [Changelog](CHANGELOG.md)

This release adds account-based dynamic model discovery and owner-only `/update` from Telegram. Existing installations need one VPS update with the new installer before remote updates are available.

## Requirements

- A Debian 12+ or Ubuntu 22.04+ VPS with root access and systemd.
- A Telegram bot token from [BotFather](https://t.me/BotFather) and your numeric Telegram user ID.
- A Google account eligible to use Antigravity. Server location, network exit, account eligibility, model availability, and quota can all affect access. Check the [official FAQ](https://antigravity.google/docs/faq/).

## Installation

Connect to the server over SSH and run:

```bash
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/main/install.sh -o install.sh && bash install.sh
```

The installer is in Chinese. Select `1` to install. It asks whether to configure a proxy, then requests the bot token and allowed Telegram ID, and guides you through Google authorization when needed. You can choose whether the token is hidden during input. After installation, send `/start` in a private chat with the bot.

The same command opens the management menu. Select `2` to update while preserving existing settings, `3` to review and confirm complete removal, `4` to add or replace a proxy node, `5` to diagnose service status, or `0` to exit. Review the removal scope carefully: the workspace and independently installed agy credentials are preserved.

## Proxy nodes

The project supports selected share-link formats for VLESS, VMess, AnyTLS, Trojan, Hysteria2, and Shadowsocks. Unsupported options are rejected. Node links contain credentials: enter them only in the hidden terminal prompt, and do not publish them.

The managed proxy listens on loopback for this project; it does not change system-wide routing. The installer checks HTTPS connectivity and, if bot configuration already exists, checks the Telegram identity through the candidate node. Connectivity does not establish Google account eligibility or model quota. The proxy core is downloaded from the official release and checked against a pinned SHA-256 for x86-64 or ARM64; initial downloads still require GitHub access.

Replacing a node restarts the bot and may interrupt a task. Interrupted tasks are not automatically rerun. If switching fails, the manager attempts to restore the previous configuration. If a switch is interrupted, reopen menu `4` to recover; recovery is not performed automatically at boot.

## Using the bot

Send a text task, or attach screenshots, logs, or code files. Individual attachments are limited to 10 MB, with at most 10 files and 30 MB per group. The workspace is shared: allow only trusted users.

| Command | Purpose |
| --- | --- |
| `/start`, `/help` | Welcome and command list |
| `/language` | Select Chinese or English for your interface |
| `/model`, `/model refresh` | Select from the signed-in account catalog or refresh it without probing models |
| `/mode` | Select execution mode |
| `/status` | Check the current task |
| `/cancel` | Cancel the current task |
| `/queue` | View waiting tasks; use `/queue add <task>` to queue text |
| `/queue cancel`, `/queue resume` | Cancel or resume your waiting tasks |
| `/steer <correction>` | Stop and restart the current text task with a correction |
| `/history`, `/last` | Retrieve saved results without rerunning them |
| `/new`, `/reset` | Start a new conversation |
| `/usage` | View usage statistics |
| `/restart` | Restart the service when idle (owner only) |

Up to three text tasks can wait in the queue; attachments cannot be queued. Saved waiting tasks are paused after a restart until their owners confirm. A crash can still lose a queued task. Messages received while the service is offline may be discarded at startup and are not automatically executed.

Language preference is saved per authorized user. Your task text, file names, model replies, and externally supplied error text remain in their original language. Historical records created before this update may contain older Chinese labels. The installer remains in Chinese.

Completed task results are sent as new messages, while the original progress card closes with the final status. Notifications follow your Telegram and device settings, including when you leave the chat. Long results remain paginated; saved results can be retrieved without rerunning the task.

## Troubleshooting

Run the installer and choose `5`, or run `bash install.sh --diagnose`, to inspect bot and managed proxy status. Telegram 409 can indicate another instance is polling the same bot. Check other devices before restarting; do not repeatedly launch competing instances.

```bash
systemctl status agy-telegram-remote --no-pager
journalctl -u agy-telegram-remote -n 80 --no-pager
```

Network connectivity, authorization, location, quota, and model availability are separate checks. Model checks consume quota. Task cancellation does not undo changes that have already happened.

## Security and testing

The bot and agy run as root and new installations enable automatic tool approval by default. The systemd restrictions are not a complete sandbox; an authorized user or a prompt-injected task can access sensitive files within the service's permissions. Read [SECURITY.md](SECURITY.md) before use. Never publish tokens, node links, Google authorization codes, or private keys.

Updates preserve settings and keep private backups under `/var/backups/agy-telegram-remote`. Review and manage old backups deliberately.

On a supported Linux system, run the offline checks with:

```bash
bash scripts/verify.sh
```

The project uses the Python standard library at runtime. Offline tests do not prove live Telegram or Google access. See [testing notes](docs/TESTING.md) and the [changelog](CHANGELOG.md) for detailed history.

## Credits

Thanks to [whypuss/agy-telegram-bot](https://github.com/whypuss/agy-telegram-bot), [Google agy documentation](https://antigravity.google/docs/cli/install/), and the [Telegram Bot API](https://core.telegram.org/bots/api).

Join the [Telegram community](https://t.me/xiaoheidemimi). The project also lists a [BandwagonHost referral link](https://bandwagonhost.com/aff.php?aff=80815) in the Chinese README.

### Dynamic model menu

`/model` reads `agy models` using the server's signed-in account. The bot ships no fixed model catalog or version blacklist. New models and reasoning variants appear automatically when the CLI returns them. Menus refresh on opening with a 60-second cache; `/model refresh` or **Refresh now** bypasses it. Restarting the service triggers a fresh lookup on the first open.

Listing sends no chat prompts. Visibility does not guarantee quota, regional eligibility, or a successful request. Failed lookups preserve a clearly marked cached snapshot; without a snapshot, no candidate models are invented. Busy tasks defer lookup until the menu is reopened while idle. Failed automatic lookups have a 15-second cooldown. An upstream CLI interface/format change can still require a software update.

The optional **Check current model** / `/model check` consumes quota and remains separate from catalog refresh. Existing selections, permissions, history and running tasks are preserved. Stale buttons cannot select models missing from the refreshed catalog. Large catalogs are paginated. Legacy version aliases are retained for old settings; family shortcuts follow the current catalog.

### Update from Telegram

After installing a version with this feature on the VPS once, the owner can send `/update` in private chat. It checks the fixed repository's `main` branch, pins its current commit, runs offline tests and a Telegram identity check before stopping the old bot, then deploys and verifies initialization and polling readiness. Up-to-date installations are not restarted. An independent systemd updater survives bot shutdown, reuses the installer's deployment lock and rollback, and sends the final result to the owner. `/update status` retrieves the durable result if notification fails.

Active tasks, attachments, model checks, waiting queues and unrecovered results block updates. Duplicate requests do not create concurrent deployments, and the new bot waits for readiness verification before accepting work. Only merged `main` code is used; arbitrary sources/arguments and unmerged PRs are not accepted. This is an explicit command, not a scheduled or push-triggered update.

Configuration, proxy, Google login, permissions, history and model choices are retained. No model prompts, Google CLI upgrade, OS package upgrade or reauthentication runs during a bot update. Changes to dependencies or login can still require VPS maintenance. Failure retains or restores the previous bot; unconfirmed recovery is reported explicitly.

To bootstrap this release on an existing VPS with its saved configuration:

```bash
curl -fsSL https://raw.githubusercontent.com/shixiaoheia/agy-telegram-remote/v2026.11.13.2/install.sh -o install.sh && bash install.sh --ref v2026.11.13.2
```

The explicit `--ref` bypasses the menu. Verify `/status` and `/model` afterward; future updates merged into `main` can be installed through `/update`.
