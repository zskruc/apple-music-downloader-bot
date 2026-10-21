# YouTube Music, video downloads, subtitles and persistent authentication

[中文](YOUTUBE.zh-CN.md) · [Operations](OPERATIONS.en.md) · [Authentication services and commands](../services/youtube/README.en.md)

This guide follows `bot/youtube_ingest.py`, `bot/nas_youtube_auth.py`, `bot/auth_auto_refresh.py` and the Compose templates in this repository. YouTube Music and YouTube video downloads share the bot and yt-dlp engine, with separate audio/video import paths. They do not require two separate downloader websites. Use content you are entitled to access, download and retain. Accounts, cookies, media and access rights are not supplied.

## 1. Links and routing

| Input | Result |
| --- | --- |
| `https://music.youtube.com/watch?v=VIDEO_ID` | Automatically imports audio into the YouTube Music folder |
| A regular YouTube / youtu.be link | Telegram offers audio/video buttons; Synology Chat needs an explicit command |
| `/ytmusic https://www.youtube.com/watch?v=VIDEO_ID` | Imports a YouTube or YouTube Music link as audio; `/ytaudio` is an alias |
| `/ytvideo https://www.youtube.com/watch?v=VIDEO_ID` | Imports video, including when given a YouTube Music link |
| `/status`, `/progress` | Shows the queue and current download progress |
| `/deletevideo` | Telegram video selection and confirmation; deletes selected media, sidecars, subtitles and matching history |

Send one YouTube URL per message. Do not mix Apple Music, YouTube and Bilibili URLs in one message. Single items and collections recognized by yt-dlp are supported. `YT_PLAYLIST_MAX_ITEMS=50` is the default safety limit: a detected collection exceeding the limit is rejected rather than silently truncated. Remove the `list=` parameter when you intend to download only one video.

There is no YouTube song-name search command. Plain song names use the main bot's Apple / LX / SQ search. Supply links for YouTube Music.

## 2. Audio quality, storage and libraries

Audio selection is `bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio`, followed by M4A extraction with `--audio-quality 0`. A compatible AAC track may be copied; another input codec may require re-encoding. **M4A is a container, not a lossless guarantee. A lossy YouTube source does not become ALAC, FLAC, Hi-Res or a studio master through this workflow.** `--audio-quality 0` does not establish losslessness.

The main Compose file uses these mounts. Set the host paths in private `.env` on host-local data storage outside Git, iCloud, Documents and Desktop.

| Purpose | Host variable/path | Inside bot |
| --- | --- | --- |
| Audio library | `YOUTUBE_MUSIC_PATH` | `/youtube-music` |
| Video library | `YOUTUBE_VIDEO_PATH` | `/youtube-video` |
| State, history, temporary downloads | `STATE_PATH/youtube` | `/state/youtube` |
| Job logs | `STATE_PATH/logs` | `/state/logs` |

Audio is organized by artist/album, with `[YT_VIDEO_ID]` in filenames. The workflow writes tags/artwork, optionally enriches metadata through Beets, attempts LRC lookup through lyric services/LRCLIB, and requests a Navidrome scan. `YT_BEETS_ENABLED` and `YT_LRC_ENABLED` control optional enrichment. Verify matches and lyric timing against the actual audio. LRC matching and video subtitles are separate features.

Configure `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` and map the same host music folder into Navidrome. Video uses `EMBY_URL`, `EMBY_USER`, `EMBY_PASSWORD`; with `EMBY_METADATA_SYNC_ENABLED`, set `EMBY_YOUTUBE_LIBRARY_PATH` to the path seen by Emby, not by the bot. A successful scan request does not establish playback. Match `YT_OUTPUT_UID/GID` to your media server's read permissions.

## 3. Video formats and playback compatibility

Video uses separate `bestvideo+bestaudio` streams. `YT_VIDEO_MAX_HEIGHT=0` leaves height unrestricted. The defaults are:

```text
YT_VIDEO_FORMAT_SORT=res,fps,hdr,quality,lang,tbr,vcodec,channels,acodec,asr
YT_VIDEO_MERGE_FORMAT=mkv
```

Resolution, frame rate and dynamic range take precedence over codec preferences. Among otherwise comparable formats, yt-dlp's [codec ordering](https://github.com/yt-dlp/yt-dlp#sorting-formats) may prefer AV1 / VP9. Available formats depend on the source, region, account and player client. 4K/8K, HDR and a particular codec are not guaranteed for every link. Set `YT_VIDEO_MAX_HEIGHT=1080` to cap height, then update the bot container's environment.

MKV accommodates the downloaded video/audio streams. Merging normally copies the video without improving source quality. Check actual AV1, VP9, HDR and MKV support on your TV, browser or Emby client. Create a separate, controlled transcode for wider compatibility and retain the original. This selector does not automatically fall back to a lower-quality combined format; a source lacking suitable independent streams can fail.

Video folders are organized by channel/collection/year. Each item contains the video, available `poster.jpg` / `folder.jpg`, NFO, `youtube.info.json` and processed external subtitles. Information JSON can contain descriptions, media URLs and account-visible content; sanitize it before sharing.

## 4. Caption selection, translation and bilingual ASS

After media download, the bot requests manual and automatic captions separately and converts them to SRT. With normal `.info.json` metadata, selection works as follows:

1. Prefer manual Chinese captions, then an available automatic/YouTube-translated Chinese track. The priority is `zh-Hans`, `zh-CN`, `zh`, `zh-Hant`, `zh-TW`, `zh-HK`, then another `zh-*` track.
2. Use the video's language metadata to select manual original-language captions, then automatic original-language captions. English is a fallback for English or unidentified sources, not an unconditional second language for Japanese/Korean/etc.
3. Chinese videos retain one default Chinese SRT. Non-Chinese videos with Chinese and original-language captions produce one bilingual ASS; Chinese alone produces a Chinese SRT. With no usable Chinese captions, the current organizer discards foreign-only tracks and imports the video without subtitles.

`YT_AUTOMATIC_SUBTITLE_LANGUAGES` is only a fallback when no readable media information JSON exists. Its default is `zh-Hans,zh-Hant,zh-CN,zh-TW,zh-HK,zh,en-orig,en,.*-orig`. Changing it does not replace the normal metadata-based selection above.

**Translation depends on tracks exposed by YouTube.** This video workflow does not run a translation model/API or Whisper to invent missing captions. The word-lyrics workers do not automatically transcribe/translate videos. Missing or disabled Chinese translation tracks, or failed caption requests, can leave the video without Chinese subtitles.

Manual and automatic caption requests each support authentication fallback. HTTP 429 triggers increasing waits. Defaults are 900 seconds per caption request, a 2-second subtitle interval, at most 4 rate-limit attempts per strategy, and waits of 60, 120 and 180 seconds. Controls are `YT_SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS`, `YT_SUBTITLE_SLEEP_SECONDS`, `YT_SUBTITLE_RATE_LIMIT_RETRIES`, `YT_SUBTITLE_RATE_LIMIT_RETRY_SECONDS`. Missing captions are logged without automatically failing a successful media download.

Bilingual ASS aligns Chinese and source-language cues, clips overlaps/drops invalid cues and logs quality statistics. Every line is not guaranteed to have both languages. Styles use a 1920×1080 reference canvas, with Chinese above the source language. Sidecars end in `.zh-CN.default.ass` or `.zh-TW.default.ass`; single-language output uses `.default.srt`. A player must load same-name external subtitles and support ASS styling. Processing retains one default track, not every raw downloaded track.

Font and layout settings are shown below. Defaults select Noto CJK / Liberation fonts suitable for Linux:

```dotenv
YT_BILINGUAL_CHINESE_FONT=Noto Sans CJK SC
YT_BILINGUAL_ENGLISH_FONT=Liberation Sans
YT_BILINGUAL_CHINESE_SIZE=68
YT_BILINGUAL_ENGLISH_SIZE=42
YT_BILINGUAL_MARGIN_V=64
YT_BILINGUAL_MARGIN_H=96
YT_BILINGUAL_SAFE_WIDTH=1728
YT_BILINGUAL_CUE_GAP_MS=80
```

The bot image installs Noto CJK, Liberation and fontconfig, and `.env.example` explicitly selects the fonts above. The actual player/Emby renderer still needs matching licensed fonts. ASS files do not embed fonts; this repository does not distribute PingFang or Arial. Generating sidecars does not require rendering them inside the bot. If Emby burns them in, Emby needs the fonts. Inspect long lines, portrait videos and unusual aspect ratios after changing settings.

### Optional burned-in copy

The bot produces external soft subtitles and **has no automatic burn-in setting**. Soft subtitles remain switchable; burn-in changes the picture and requires video re-encoding. You can create a separate copy in an FFmpeg environment built with libass. Run this example in a private work directory containing simple filenames and matching, licensed fonts in `fonts/`, avoiding filter-path escaping issues:

```sh
ffmpeg -n -i input.mkv -map 0:v:0 -map '0:a?' \
  -vf 'ass=filename=subtitles.ass:fontsdir=fonts' \
  -c:v libx264 -crf 18 -preset medium -c:a copy output-with-subtitles.mkv
```

`-n` prevents overwriting an existing output. This is an SDR example, not a general HDR/Dolby Vision preservation recipe; HDR needs a separately designed color/encoding workflow. Inspect the copy and retain the original video and subtitles. See the [official FFmpeg ASS / subtitles filter reference](https://ffmpeg.org/ffmpeg-filters.html#ass).

## 5. Proxies, PO tokens and dependencies

The bot image installs pinned yt-dlp, yt-dlp-ejs, the bgutil plugin, Deno and FFmpeg; see `requirements.txt` / `Dockerfile`. Compose's `youtube-pot` provides PO tokens at `http://youtube-pot:4416`. A PO token is separate from login cookies, does not grant account entitlement, and should not be hardcoded as a private token in the repository.

| Request path | Configuration and behavior |
| --- | --- |
| Standard yt-dlp metadata, size estimation, video subtitles, artwork | `YT_DOWNLOAD_PROXY_URL`; the template explicitly leaves it empty for direct access when no general proxy environment is configured |
| Large audio/video transfers | If `YT_LARGE_DOWNLOAD_PROXY_URL` is set, use it when size exceeds `YT_LARGE_DOWNLOAD_THRESHOLD_MIB=500` or is unknown; empty means use the normal route |
| Retryable network download failures | Try a different configured route while preserving partial files within this attempt; identical routes are not tried twice |
| Chromium login browser | Separate `YOUTUBE_BROWSER_ARGS`; add e.g. `--proxy-server=http://host.docker.internal:7890` if needed; it does not inherit the bot's download proxy |
| Bot → PO health check | Direct container-network `/ping`, explicitly bypassing system proxies |
| Explicitly enabled SABR strategy | Without an explicitly selected transfer proxy, prefer the large-transfer proxy, then the normal proxy; an explicit proxy takes precedence |
| Beets metadata and LRCLIB lyrics | The normal `YT_DOWNLOAD_PROXY_URL`; local `YT_LRCAPI_URL` requests use urllib's normal environment-proxy rules |
| Telegram, Apple, Bilibili | Their own settings; not implicitly controlled by the YouTube proxy |

Size estimation defaults to 300 seconds (`YT_DOWNLOAD_SIZE_PROBE_TIMEOUT_SECONDS`). Only configure routes reachable from the containers. Host `127.0.0.1` refers to the container itself from inside a container; the template supplies `host.docker.internal` for host services. A proxy URL containing credentials is private: remove it from public commands, screenshots and issue reports.

`YT_PLAYER_CLIENT=mweb` works with the PO provider. Normal probing can try cookie + PO, cookie + embedded, anonymous embedded and anonymous + PO strategies; the conditions below add `cookies-sabr`. Public content may work without cookies. Candidate-cookie checks force a cookie strategy and prohibit anonymous fallback, so public access alone cannot be mistaken for recovered login. Without the optional Docker socket, administrators must recover an unhealthy PO service through maintenance commands; the bot cannot start/restart it itself.

Review yt-dlp, EJS, Deno, the PO plugin/server and YouTube client behavior together when upgrading. Updating one dependency does not establish continued format/login compatibility. External requirements change; consult [yt-dlp's YouTube notes](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube) and the [PO provider upstream](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).

### Explicitly optional SABR tool

Normal images retain the pinned standard yt-dlp. **The SABR fork is not downloaded, installed or enabled by default.** To try the path corresponding to the specialized production tool, set private `.env`:

```dotenv
INSTALL_YT_SABR=true
YT_SABR_FALLBACK_ENABLED=true
YT_SABR_TOOL_PATH=/app/vendor/yt-dlp-sabr/yt-dlp
```

`INSTALL_YT_SABR` is a build argument forwarded by Compose; `YT_SABR_FALLBACK_ENABLED` is a runtime switch. Both default to `false`. Enabling runtime fallback does not install the executable, and installing it does not add it to download strategies. The fork runs through the same private-cookie snapshot runner rather than writing to persistent cookies directly.

Adding `cookies-sabr` also requires a complete cookie chain, `YT_PO_TOKEN_ENABLED=true`, a nonempty provider address, and an installed tool matching the pinned SHA-256. Normal ordering is `cookies-po-token` → `cookies-embedded` → `cookies-sabr` → `anonymous-embedded` → `anonymous-po-token`; the successfully probed strategy can be preferred for the subsequent transfer. Each media-download attempt invokes SABR at most once even across two ordinary strategy cycles. A new attempt after browser recovery has a new budget. Independent subtitle requests may also use SABR and do not share that media-attempt limit.

When enabled and verified, cookie export/rollback probes force `cookies-sabr` directly. Otherwise they force `cookies-po-token`, or `cookies-standard` when PO is disabled. All prohibit anonymous fallback and recursive refresh. A runtime integrity failure raises an explicit error rather than bypassing the hash check or executing a different tool.

The build installer is `services/youtube/install_sabr_tool.py`. Pinned metadata is below; see [SABR_SOURCES.md](../services/youtube/SABR_SOURCES.md) for provenance and licenses.

| Item | Pinned value |
| --- | --- |
| Tool version | `2026.08.19.233452` |
| Source commit | `6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6` |
| Executable SHA-256 | `8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d` |

The upstream `sabr` release tag can move. A changed asset is rejected on SHA-256 mismatch rather than silently accepted; deliberately updating the pin requires a new source/compatibility review. The fork's Unlicense and bundled third-party licenses remain separate from this project's MIT license. Enabled images retain licenses and provenance under `/usr/share/doc/yt-dlp-sabr/`.

For an existing deployment, build a candidate under a **new tag**, then use the existing single-service upgrade flow. Replace the example tag with an unused tag for each revision. The build command leaves the running bot alone; the subsequent `upgrade` performs the maintenance update:

```sh
docker build --build-arg INSTALL_YT_SABR=true \
  --tag local/apple-music-downloader-bot:sabr-reviewed .
# Pause submissions and wait for idle; retain actual deployment profile/socket options.
bash ops/stack.sh --profile auth upgrade bot local/apple-music-downloader-bot:sabr-reviewed /srv/private-backups/youtube-sabr-upgrade
bash ops/stack.sh probe
# Revert the image if acceptance fails:
bash ops/stack.sh --profile auth rollback /srv/private-backups/youtube-sabr-upgrade
```

`bash ops/stack.sh build bot` also forwards `.env` build arguments, but builds the tag selected by the current Compose/image override. Use it for a fresh deployment or after deliberately separating the build tag from the old image; do not overwrite the production tag with a candidate. `upgrade` reads runtime switches from `.env`. Review them before rolling back to an older image: image rollback does not restore `.env`.

This optional strategy uses a prerelease fork. It does not guarantee SABR downloads, login, 4K/8K or particular client formats, and does not invent missing subtitles. The same quality selection, bounded retries and cancellation apply. Installation/version output is not a real-download acceptance test. This addition supplies public provenance, installation and offline checks only; the optional executable was not downloaded/executed, and existing NAS services were not changed or asked to download new media.

## 6. Persistent cookies and Chromium profile

| Private data | Host path | Container path |
| --- | --- | --- |
| Complete browser state | `YOUTUBE_BROWSER_PATH` | Browser `/config`; bot read-only `/youtube-auth-browser` |
| Chromium Default profile | `YOUTUBE_BROWSER_PATH/.config/chromium/Default` | Bot `/youtube-auth-browser/.config/chromium/Default` |
| Active Netscape cookie file | `STATE_PATH/youtube/cookies.txt` | `/state/youtube/cookies.txt` |
| Previous cookie file | `STATE_PATH/youtube/cookies.txt.previous` | `/state/youtube/cookies.txt.previous` |
| Refresh cooldown state | `STATE_PATH/auth-refresh/youtube.json` | `/state/auth-refresh/youtube.json` |

Recreating bot/browser containers does not erase these host bind mounts. Keep profiles out of ephemeral container storage and do not clear their host data directories. `YT_AUTH_BROWSER_PROFILE_PATH` sets the profile path; check the actual layout before switching Chromium distributions.

The browser uses `--password-store=basic`; the exporter reads Chromium with `BASICTEXT`. Protect the full profile as login credentials. Export retains only `youtube.com` and subdomain cookies and requires SID, HSID, SSID, APISID, SAPISID, LOGIN_INFO, `__Secure-1PSID`, `__Secure-3PSID`. Present names establish structural completeness, not account validity or download entitlement.

A persistent profile supports refreshing an existing session; **cookies can still expire or rotate**. After login, return to `https://www.youtube.com/robots.txt`, close other YouTube tabs and stop the browser. Do not use it as an everyday playback browser. Upstream also documents one-time incognito export; this repository maintains a dedicated reusable profile, which cannot promise the same session lifetime. See [yt-dlp's cookie rotation guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

### First manual login and export

Run commands on the deployment host from this checkout after configuring `.env` and starting bot / PO services. Keep the same `COMPOSE_PROJECT_NAME` and deployment options. The helper automatically inherits active image overrides; also pass matching `--auth-refresh` / `--self-heal` / `--profile lx` / `--profile sq` options when those are deployed:

```sh
bash services/youtube/youtube-auth.sh start
bash services/youtube/youtube-auth.sh status
```

The HTTPS browser binds to host loopback, default `127.0.0.1:31011`. For remote access, establish your own SSH tunnel, e.g. `ssh -N -L 31011:127.0.0.1:31011 nas-user@nas.example.net`, then open `https://127.0.0.1:31011`. Adapt the example to your SSH/network settings. Keep the browser off the public internet; handle its self-signed certificate only after confirming the tunnel targets your server.

The account owner enters passwords, verification codes and 2FA manually in the dedicated Chromium UI. Do not send them to the bot, logs or GitHub. Confirm YouTube login, return to robots.txt and close other YouTube tabs. Replace the following placeholder with a permitted, suitable login-test URL:

```sh
bash services/youtube/youtube-auth.sh export 'https://www.youtube.com/watch?v=VIDEO_ID'
bash services/youtube/youtube-auth.sh status
```

The helper stops Chromium, confirms its state through Docker and runs the exporter inside the existing bot. The exporter writes a same-directory 0600 candidate, checks required cookie names and probes the supplied URL using that candidate and an explicit cookie strategy: available/enabled SABR, otherwise cookie + PO or standard-cookie authentication. Recursive refresh and anonymous fallback are disabled. On success, it keeps the old file as `.previous` and atomically activates the candidate. Failure preserves the active file and removes temporary candidates. Stopping Chromium matters because the browser can otherwise rotate cookies or write its SQLite session during export.

All yt-dlp invocations use 0600 cookie copies inside 0700 temporary directories. Cookie updates affect that copy, with cleanup on normal exit, `SystemExit` and `SIGTERM`. An older downloader cannot overwrite newly refreshed persistent cookies when it exits. `SIGKILL` or host power loss prevents guaranteed immediate cleanup; after stopping services, inspect/remove leftover `amdl-ytdlp-cookies-*` directories in container temporary storage, preserving persistent cookies/profiles. Persistent updates go through the authentication exporter.

This probe resolves metadata; it does not download the whole video or prove entitlement to every format/content. Next verify short authorized audio/video downloads. `status` reports existence, counts and missing names, without cookie values.

To return to the previous session:

```sh
bash services/youtube/youtube-auth.sh rollback 'https://www.youtube.com/watch?v=VIDEO_ID'
```

Rollback also stops the browser and probes its candidate. Only a successful probe exchanges active and `.previous` files. If both have expired, sign in manually again. Do not splice cookies with a text editor or run export, rollback and automatic refresh concurrently. The low-level `--skip-probe` option is for deliberate offline maintenance, not validity acceptance; the main helper always requires a test URL.

## 7. Manual recovery and optional automatic refresh

Default Compose disables automatic refresh and does not mount the Docker socket. Distinguish network failures, an unhealthy PO service, missing cookies and a rejected session. Recover through start → manual login/verification → export. A saved profile cannot bypass passwords, 2FA or account recovery.

For automatic refresh, review `compose.auth-refresh.yaml`, set private `.env` `ONLINE_AUTH_AUTO_REFRESH_ENABLED=true`, and explicitly apply the socket override to the bot, for example:

```sh
bash ops/stack.sh --auth-refresh --profile auth up bot
```

The Docker socket grants host-level Docker administration; a service-name allowlist is not an isolation boundary. `--auth-refresh` creates no watchdog and does not enable wrapper restart recovery. Existing `--self-heal` deployments can retain that overlay with the same `.env` switch. Reuse existing NAS supervision instead of installing duplicate loops. Create the browser container and complete its first manual login before relying on automatic refresh.

An authentication or unavailable-format error can trigger: confirm the browser is stopped → start the persistent browser → wait for its HTTPS port and default 50-second settling period → stop and confirm → export candidate → force a probe of the current URL → replace cookies → retry the job once at the same quality. A running manual-login browser is left alone. Password/2FA prompts require the account owner. SABR/format restrictions are not necessarily cookie failures; refresh does not guarantee restored high-resolution formats. Another failure ends this job so the queue can continue. This is error-triggered recovery, not a cookie polling or account keepalive scheduler.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ONLINE_AUTH_AUTO_REFRESH_SETTLE_SECONDS` | 12 | General site settling period |
| `YT_AUTH_AUTO_REFRESH_SETTLE_SECONDS` | 50 | YouTube uses the greater of this and the general period |
| `ONLINE_AUTH_BROWSER_READY_TIMEOUT_SECONDS` | 60 | Browser port readiness limit |
| `ONLINE_AUTH_EXPORT_TIMEOUT_SECONDS` | 240 | Automatic export/probe limit |
| `ONLINE_AUTH_SUCCESS_COOLDOWN_SECONDS` | 300 | Reuse a successful refresh for 5 minutes |
| `ONLINE_AUTH_FAILURE_COOLDOWN_SECONDS` | 1800 | Suppress automatic retries for 30 minutes after failure |
| `YT_AUTH_AUTO_REFRESH_DISABLED` | false | Disable recursive recovery in a probe/diagnostic |

Cooldown state persists under `/state/auth-refresh`. Use normal export after manual login rather than repeatedly forcing a failed automatic refresh. Pause submissions and avoid automatic recovery during manual maintenance.

## 8. Deduplication, retries, backups and upgrades

Repeated delivery of the same source/chat/message ID reuses its task card. A new message creates a new task, with media history handling completed-item deduplication. Audio and video have separate `/state/youtube/archive-audio.txt` and `archive-video.txt`; `downloads.sqlite3` stores output records. The same ID can intentionally exist as audio and video. Each attempt uses a temporary archive; an ID enters persistent history only after successful organization/registration. Video also checks existing item folders. Do not erase the entire archive/database to retry one job. Diagnose the specific record if history exists but its output is missing.

Network route switches retain partial files within the attempt; authentication/token strategy switches clear them. Exhausted retryable highest-quality strategies wait `YT_HIGH_QUALITY_RETRY_DELAY_SECONDS=60` before another cycle. `YT_HIGH_QUALITY_RETRY_MAX_CYCLES=2` limits each download attempt to two cycles by default (configurable from 1 to 10), then returns to optional browser recovery without lowering quality. `YT_DOWNLOAD_TIMEOUT_SECONDS=21600` limits **each downloader process to 6 hours, not the whole job**. Watch `/progress` and job logs; use the task card to stop when necessary. Retry waits and browser refresh support cancellation. Temporary directories are cleaned on completion/failure; cross-job or cross-container fragment resume is not guaranteed.

Pause external submissions and wait for jobs to finish. Include all matching deployment profile/socket options, such as `--auth-refresh` or `--self-heal`, when using the existing maintenance tool:

```sh
bash ops/stack.sh --profile auth backup /srv/private-backups/youtube-before-change
bash ops/stack.sh --profile auth upgrade bot local/apple-music-downloader-bot:reviewed-version /srv/private-backups/youtube-upgrade
bash ops/stack.sh probe
# If upgrade acceptance fails:
bash ops/stack.sh --profile auth rollback /srv/private-backups/youtube-upgrade
```

Use a new private destination outside the checkout and source data. The snapshot pauses writers and includes `.env`, state/cookies, full browser profiles, relevant private service data and image-switch records. It is **not encrypted** and excludes media libraries. Include `--profile auth` to place browser `/config` in the backup configuration; `--auth-refresh` also saves that overlay. Never publish these backups, cookies, profiles, databases or logs.

`upgrade` requires a pre-pulled/built candidate and retains the old image ID. Continue using `ops/stack.sh` afterward to preserve `STATE_PATH/ops/images.compose.json` overrides. `rollback` switches the service image only, preserving current data; it does not restore earlier cookies, databases or profiles. Restore data offline with writers stopped, recover state/profile from a consistent snapshot, verify UID/GID and mounts, then start services and validate. Keep a copy of pre-restore data. Never run two bot pollers or two Chromium instances writing the same profile.

## 9. Acceptance and diagnostic limits

Healthy containers, a working browser, required cookie names and successful metadata resolution are separate evidence layers. They do not replace real downloads. Deployment acceptance should cover:

- Short authorized audio/video jobs: inspect codec, duration, streams, output paths/permissions, and actual Navidrome / Emby playback.
- A small playlist and an over-limit playlist: verify counting, rejection, resubmission deduplication and retry of failed members without corrupting successful history.
- Manual Chinese, automatic/translated Chinese, non-English originals and no-caption sources: inspect selection, missing-caption logs, ASS timing/fonts, long lines and player rendering.
- Container recreation with persistent state/profile; expired-session feedback, manual relogin, failed candidate preserving the active cookies, rollback and refresh cooldowns.
- Separate network and PO-service failure exercises with scoped recovery, leaving unrelated NAS services alone.

Before sharing diagnostics, remove cookie values/files, full profiles, account IDs, tokens, proxy credentials, private URLs, chat IDs, expanded environments and backups. Normal logs hide cookie paths and filter common proxy-URL username/password fields, but arbitrary upstream output still needs review for sensitive details.

IPv6 access was unavailable during initial preparation. A subsequent [read-only live check on 2026-10-21](LIVE_CHECK_20261021.md) inspected running source, mounts, private-file permissions, dependencies and existing video/subtitle files. Production has not deployed this repository's new Cookie rollback features. The corresponding specialized SABR tool is now an explicit, disabled-by-default option described above. This check did not start the login browser, export cookies, request a new download, play media or exercise backup/restore, and the newly added optional executable was not downloaded/executed; those acceptance tests remain pending.
