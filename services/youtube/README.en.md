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

## Optional SABR fork

Default images do not download/install the SABR fork, and its runtime strategy is disabled. To opt in, set private `.env` `INSTALL_YT_SABR=true` and `YT_SABR_FALLBACK_ENABLED=true`, with `YT_SABR_TOOL_PATH=/app/vendor/yt-dlp-sabr/yt-dlp`. The first installs at build time; the second enables runtime use. The authentication helper never installs/downloads the tool itself.

`install_sabr_tool.py` pins version `2026.08.19.233452`, source commit `6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6`, and executable SHA-256 `8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d`. A changed asset behind the mutable upstream `sabr` tag is rejected. See [SABR_SOURCES.md](SABR_SOURCES.md) for provenance and separate licenses. This prerelease tool does not guarantee downloads or 4K. Candidate probes and downloads still use private cookie copies. With the tool enabled/verified and PO enabled, candidate probes force `cookies-sabr` without anonymous fallback. Ordinary runtime strategies additionally require a complete cookie chain and nonempty provider address.

For production, first build a new image tag, e.g. `docker build --build-arg INSTALL_YT_SABR=true --tag local/apple-music-downloader-bot:sabr-reviewed .`, then follow the [full guide](../../docs/YOUTUBE.en.md#explicitly-optional-sabr-tool) for backup, single-service `upgrade` and acceptance. `bash ops/stack.sh build bot` builds the tag selected by the current image override; do not overwrite a production tag needed for rollback. This addition did not download/execute the optional tool, change the NAS or request new media downloads.

An external macOS Cookie sync/launchd integration is optional and is neither installed nor enabled here. Do not run Mac sync, manual export, and NAS recovery as simultaneous writers. No personal SSH path, account, or private domain is supplied.
