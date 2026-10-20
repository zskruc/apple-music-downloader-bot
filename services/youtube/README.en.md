# YouTube authentication operations

[中文](README.md) · [Full English guide](../../docs/YOUTUBE.en.md)

This directory operates the existing Compose `youtube-auth-browser` and bot exporter. It adds no second bot, scheduler, or daemon. Keep the browser `/config` directory and `/state/youtube` cookies on persistent local NAS storage, with private backups. Never commit browser profiles, cookies, or refresh state.

Run from the deployed repository, preserving its `.env` and `COMPOSE_PROJECT_NAME`. The entry point reuses `ops/stackctl.py` and automatically includes the active image overrides saved at `state/ops/images.compose.json`:

```sh
bash services/youtube/youtube-auth.sh start
# Open the local/tunneled browser. Enter credentials and 2FA yourself.
bash services/youtube/youtube-auth.sh status
bash services/youtube/youtube-auth.sh export 'https://www.youtube.com/watch?v=PUBLIC_VIDEO_ID'
# Only when restoring the previous session is appropriate:
bash services/youtube/youtube-auth.sh rollback 'https://www.youtube.com/watch?v=PUBLIC_VIDEO_ID'
```

Preserve any applicable deployment options before the command, for example `bash services/youtube/youtube-auth.sh --auth-refresh --self-heal --profile lx --profile sq status`. Include only options you actually use; the `auth` profile is added automatically. The helper starts only the browser and executes exports inside the running bot. It does not recreate the bot or change its image.

The helper stops Chromium and verifies its stopped state with Docker inspect. The exporter creates a private candidate, filters to `youtube.com`, checks required session-cookie names, and probes the supplied link using that candidate without authentication fallback. It atomically publishes only after success. The former cookies become `cookies.txt.previous`; both files have mode `0600`. Rollback also probes before replacing and preserves the current file as the new `.previous`. This single rollback copy does not replace a backup policy. Stopping Chromium allows its session to reach disk and avoids exporting a rotating live session; leave it stopped afterwards.

`status` reports metadata only: existence, size, mode, age, and missing standard cookie names. It never prints cookie values and does not establish that downloads work. Browser state is `null` without an in-container Docker socket; the host helper independently verifies the stopped state. The bot service must already be running for status/export/rollback.

`bot/nas_youtube_auth.py` also accepts `--profile`, `--destination`, `--probe-url`, `--status`, and `--rollback`. `--browser-stopped` is an explicit host-side assertion only when no Docker socket is mounted; a mounted socket is always checked. `--skip-probe` is for deliberate offline maintenance and does not establish a valid session. The normal helper never skips the probe.

Automatic recovery defaults to off. Enabling it requires both `ONLINE_AUTH_AUTO_REFRESH_ENABLED` and an explicit Docker socket mount (privileged host control); follow the opt-in Compose configuration in the full guide. The existing `auth_auto_refresh.py` starts, waits for, and stops the same browser on authentication failure before probing a fresh export. It leaves an already running interactive browser alone. Default cooldowns are 300 seconds after success and 1,800 seconds after failure. Refresh-state files use `0600` and exclude cookies and raw extractor output. Recovery cannot complete a password/2FA challenge or guarantee Google will accept the session. Pause related tasks/disable automatic recovery while logging in, exporting, or rolling back.

An external macOS Cookie sync/launchd integration is optional and is neither installed nor enabled here. Do not run Mac sync, manual export, and NAS recovery as simultaneous writers. No personal SSH path, account, or private domain is supplied.
