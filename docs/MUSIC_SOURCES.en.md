# LX Music, SQ Music and unified routing

The repository includes `bot/lx_fallback.py`, `bot/sq_music.py`, `bot/unified_music.py` and their bot integration. Backend templates are documented in the [LX Music](../services/lx-music/README.md) and [SQ Music](../services/sq-music/README.md) directories. They join the main Compose project and its default network.

## Behavior

With `AMDL_UNIFIED_MUSIC_ENABLED=true`, text search queries enabled sources and groups candidates representing the same recording. Selection attempts lossless downloads in **Apple Music → LX → SQ** order. Single-song ALAC URLs can use this flow. This does not imply automatic three-source completion for albums, playlists or Atmos jobs.

Apple authentication, networking and ordinary download failures retain the existing retry behavior. Supplementary sources are attempted only when the configured missing-media criteria are met and no uncertain new files require inspection. Matching checks artist, version, album and duration rather than title alone.

Unified routing requires actual lossless audio. The separate legacy LX mode can permit verified 320k MP3 through `AMDL_LX_FALLBACK_ALLOW_320`; unified lossless routing still rejects it. Disabled providers are skipped without loading their credentials or contacting them.

## Opt-in configuration

The unified, LX and SQ switches in `.env.example` all default to false. Selecting `--profile lx` or `--profile sq` loads the corresponding service overlay, enables its bot adapter and provides its required mounts. Unified routing remains controlled by `AMDL_UNIFIED_MUSIC_ENABLED` in `.env`.

You may select either backend independently. The root `.env.example` lists configuration options; never commit the completed file.

## Backend setup

Follow each service README first, including SQ source verification and the local patched-image build. LX uses a pinned upstream image. SQ needs MySQL, a NetEase API service and the companion task-status patch expected by the client. An accepted download response is insufficient: the client requires an authenticated task endpoint, terminal status and the actual completed file path.

No personal source scripts, platform cookies, VIP sessions, private API endpoints or working music-source configuration are included. Configure authorized sources and platform login privately.

Complete the main wrapper / Apple account initialization before bringing up the bot. Use a separate test bot when an existing production poller is running.

```sh
# Initialize private state as described in each service README, then:
bash ops/stack.sh --profile lx --profile sq check
bash ops/stack.sh --profile lx --profile sq up lxserver sq-mysql sq-neteasecloudmusicapi sqmusic-server
# Finish SQ database/account bootstrap and verify the backends before starting the bot.
bash ops/stack.sh --profile lx --profile sq up bot
```

For LX alone, omit `--profile sq` and operate on `lxserver`. For SQ alone, omit `--profile lx`. The maintenance helper merges the selected service overlays automatically.

## Credentials and storage

| Resource | Backend | Bot |
|---|---|---|
| LX library | `${MUSIC_PATH}/LX Music` mounted at `/server/music/_open` | Same host folder visible at `/music/LX Music` |
| LX frontend authentication | Private frontend password in `data/config.js` | `LX_FRONTEND_AUTH_PATH` mounted read-only at `/run/secrets/lx-frontend-auth` |
| SQ staging | `SQ_STAGING_PATH` mounted at `/music` | Same folder mounted read-only at `/sq-staging` |
| SQ published library | Published after bot verification | `${MUSIC_PATH}/SQ Music`, visible at `/music/SQ Music` |
| SQ account | Dedicated initialized account | `SQ_AUTH_PATH` mounted read-only at `/run/secrets/sq-auth.json` |

The SQ credential file is a JSON object containing `username` and `password` strings. The LX credential file is a single UTF-8 line, not JSON. Actual credential files are excluded. Host paths and in-container paths are different settings.

Keep SQ staging outside the public media library. The bot uses authenticated task results, actual FLAC verification, duration checks and path boundaries before publishing files. It never infers a completed filename from a display name. Authentication bind mounts require existing files and do not create missing paths as directories.

## Validation and maintenance

Management ports bind to loopback. Use a trusted tunnel for remote access. Validate search and authentication, then selected test downloads, codec, paths, deduplication, cancellation and failure feedback. Include missing sources, incorrect credentials and unavailable backends in acceptance testing.

```sh
bash ops/stack.sh --profile lx --profile sq status
bash ops/stack.sh --profile lx --profile sq probe
bash ops/stack.sh --profile lx --profile sq backup /absolute/private-backup
```

Before backup, pause submissions in the bot and both backend web interfaces and wait for downloads to finish. The helper checks bot and lyric work; this does not establish that manually submitted backend queues are idle. Offline backup stops writers and applications before the SQ database. It covers LX configuration data, SQ database files and both credential files. Logs, caches, staged downloads and published media are outside the default private snapshot; back up media separately.

Use the same profiles for startup, backup, upgrades and rollback. Select reviewed images or digests, and do not automatically change the MySQL major version. Image rollback does not reverse schema migrations. Reuse existing host supervision instead of adding duplicate restart loops.

This update has offline code/configuration validation. The NAS public IPv6 route remains unavailable; live-source comparison, backend cold starts and real downloads have not been verified.
