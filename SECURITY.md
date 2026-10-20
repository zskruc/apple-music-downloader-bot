# Sensitive information / 敏感信息

Never commit real `.env` files, bot tokens, chat histories, Apple credentials,
media server passwords, cookies, browser profiles, wrapper sessions, private
keys, device credentials, VM disks, databases or production logs.

不要在公开 issue、PR 或 Actions 日志中粘贴上述资料。故障报告只包含版本、
错误类型、脱敏后的最小复现，以及必要的文件名和配置项名称。

The deployment can optionally mount the Docker socket for browser management
and watchdog recovery. This grants host-level control to those containers;
review `compose.auth-refresh.yaml` / `compose.self-heal.yaml` before enabling it. Keep account and browser
interfaces on loopback, and use an authenticated tunnel when remote access is
needed. Configure a Telegram chat allowlist before starting a public bot.

Backups contain secrets and require restricted permissions and separate secure
storage. Do not upload them with the public repository.

YouTube's persistent Chromium profile, active Cookie file and `.previous`
rollback copy are all login credentials. The exporter validates a private
candidate before replacing the active file. Each yt-dlp process uses its own
private Cookie snapshot, so a previously started process cannot overwrite a
refreshed session. Status commands exclude Cookie values; inspect all upstream
diagnostic output before sharing it. See the [YouTube guide](docs/YOUTUBE.en.md).

The LX / SQ overlays mount private authentication files read-only and keep
management ports on loopback. SQ staging must stay outside the published music
library and outside database/credential directories. Initialization helpers
are intended for a new private deployment; preserve matching existing secrets
and databases when migrating.

If a credential is exposed, revoke or rotate it at its issuer before cleaning
the repository history. Deleting the visible file does not invalidate a leaked
credential. Report a vulnerability privately through the maintainer's GitHub
profile contact methods when available; do not include live secrets in a
public issue.
