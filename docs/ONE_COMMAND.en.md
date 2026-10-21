# One-command installation, private backup and migration

[中文](ONE_COMMAND.zh-CN.md) · [Home](../README.en.md)

This guide describes v0.2.0 `install.py`, `deploy/installer.py`, `deploy/manage.py` and `deploy/migration.py`. They manage a dedicated directory and explicitly recorded Compose projects. **Acceptance has not yet passed for real Docker / KVM installation, complete image-archive restore or authenticated downloads.** Synthetic tests and a read-only live comparison establish only their respective scope.

## 1. Host and suite

Use Linux amd64 / x86-64, Python 3.8+, Git, Docker Engine and the Compose plugin 2.20+. The online bootstrap example also uses curl. The installing account needs Docker daemon access and read/write access to a real `/dev/kvm`. Enable hardware virtualization and prepare any model-specific Synology KVM modules beforehand. The installer does not install host software or change the DSM kernel.

Default preflight requires 50 GiB free on the installation disk and 6 GiB available host RAM. VM defaults are 4 GiB RAM, 2 CPUs and a 32 GiB virtual disk. Allow additional space for media, Docker build caches, image archives and another complete backup. The two lyric models total about 1.65 GB. Choose a dedicated local path such as `/volume1/docker/amdl-suite`, outside the source checkout, shared storage roots, Documents, Desktop, cloud-synced folders and symlinks.

Resume first verifies actual container ownership, running state and the VM's current memory setting. An already running complete suite with no VM memory increase requires 0.5 GiB available headroom by default; a restored suite awaiting cutover with all non-writer infrastructure running requires 2 GiB. New profiles, a VM memory increase or missing infrastructure retain the full threshold. Private configuration may set `INSTALL_MIN_AVAILABLE_GIB`, `INSTALL_MIN_RESUME_AVAILABLE_GIB` and `INSTALL_MIN_RESTORE_AVAILABLE_GIB`; these control preflight only, without allocating or releasing memory.

Examples use Synology's `/volume1` data volume: installation at `/volume1/docker/amdl-suite`, restore at `/volume1/docker/amdl-suite-restored`, external private configuration at `/volume1/docker/amdl-private`, and backups at `/volume1/amdl-backups`. Avoid the small DSM system partition. On another Linux host or storage pool, consistently substitute dedicated local data-disk paths with sufficient space. Backups must remain outside the installation root and all backup sources.

The full suite prepares these services. Omitting `--with-media-servers` omits only the last row:

| Scope | Services and activation |
| --- | --- |
| Apple Music | `legacy-wrapper` KVM VM and `wrapper` adapter; subscription, upstream provisioning and login remain manual |
| Chat and queues | `bot`, persistent jobs and Apple → LX → SQ routing; polling starts only after activation requirements pass |
| LX / SQ | `lxserver`, `sqmusic-server`, `sq-mysql`, `sq-neteasecloudmusicapi`, isolated staging and generated local credentials |
| YouTube / Bilibili | Bot downloaders, `youtube-pot`, two on-demand login browsers and a checksum-pinned SABR tool |
| Lyrics | `word-lyrics-resolver`, `word-lyrics-nas-aligner`, pinned whisper.cpp and two verified GGML models |
| Recovery | `wrapper-watchdog`, `bot-watchdog`, activated with the bot; no host timer or cron is installed |
| Optional players | `navidrome`, `emby`, `lrcapi`, separate data directories and shared read-only media mounts |

The full installer enables unified music, LX/SQ, lyric automation/completion, automatic authentication, and SABR build/runtime configuration. The manual Compose template keeps its opt-in defaults. Authentication and watchdog Docker socket access grants host-level administration. Recorded ownership and service scope do not turn the socket into a security boundary.

## 2. One-command installation

Once the v0.2.0 tag and release are published, run on the target Linux host:

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/zskruc/apple-music-downloader-bot/v0.2.0/install.py | sudo python3 - --root /volume1/docker/amdl-suite --with-media-servers'
```

The bootstrap obtains the fixed release's `source.tar.gz` and SHA-256 file, verifies the archive and publishes `source/`. `pipefail` preserves curl download failures. These examples install with sudo and use sudo for maintenance to access mode-0700 private directories. If you install using a dedicated non-root account with Docker/KVM access, consistently use that account instead. Failed stages are recorded for `resume`. Infrastructure can be prepared without chat credentials; the bot stays stopped. You can first download and inspect the fixed-tag `install.py`. From an existing checkout, the local equivalent is `sudo python3 install.py install --root /volume1/docker/amdl-suite --with-media-servers`.

Installation obtains pinned images, an exact SQ source revision, models, an Ubuntu base disk and build dependencies on your machine. It does not push the locally fetched SQ source or built images. See [SOURCES](../deploy/SOURCES.md) for digests and component terms. SQ's audited upstream revision has no confirmed license; preparation does not grant redistribution rights.

### Private configuration and first activation

Copy the [example](../deploy/private-config.example.json) outside the source tree, for example to `/volume1/docker/amdl-private/private-config.json`. Use directory mode 0700 and file mode 0600, then edit it locally. The format is `{"env":{"UPPERCASE_KEY":"single-line value"}}`; booleans and integers are also accepted. The example's `START_BOT=false` prepares services before polling. Never commit real configuration, package it in a source release, or put secret values directly in commands.

```sh
sudo python3 /volume1/docker/amdl-suite/source/install.py resume --root /volume1/docker/amdl-suite --config /volume1/docker/amdl-private/private-config.json
```

Telegram requires your own `TELEGRAM_BOT_TOKEN` and a nonempty `TELEGRAM_ALLOWED_CHAT_IDS`. Synology Chat requires `SYNOLOGY_CHAT_ENABLED=true`, an outgoing token, a user allowlist and an incoming reply webhook. Any enabled but unsafe entry point blocks activation. Apple Music additionally requires a valid subscription and [wrapper / VM initialization](../vm/README.md). Complete passwords, 2FA and device authorization yourself.

**Only one poller may use a Telegram token.** The installer cannot detect that another project uses the same token. A fresh installation permits activation by default once valid credentials and wrapper health are present. When replacing a service, keep `START_BOT=false`, stop the old poller, then explicitly set it to true and `resume`. Use a separate test bot for acceptance. The installer refuses to overwrite existing private credentials; do not reapply an unfilled example to change an existing account.

Supply your own external proxy URLs as needed through `TELEGRAM_PROXY_URL`, `AMDL_DOWNLOAD_PROXY_URL`, `YT_DOWNLOAD_PROXY_URL`, `YT_LARGE_DOWNLOAD_PROXY_URL` and `BILI_DOWNLOAD_PROXY_URL`. Browser proxy arguments are separate. Host dependency downloads and Docker pulls use host/daemon networking; bot proxy variables do not configure them. No proxy subscription, Cloudflare account or private egress service is bundled.

## 3. Layout and persistence

| Path below the installation root | Contents |
| --- | --- |
| `source/` | Public code, without real `.env`, cookies, VM disks or media |
| `deployment.json` | Private project ownership, Compose files, services and mount scope |
| `config/private.env` | Generated private environment, mode 0600, reused by later commands |
| `config/installer.compose.json` | Ownership labels and mounts; `config/images.compose.json` records restored/upgraded images |
| `data/state/` | Bot databases, cookies, lyric queues; models in `word-lyrics/models/` |
| `data/vm/`, `private/wrapper-session/` | Persistent VM disks and wrapper sessions |
| `private/youtube-browser/`, `private/bilibili-browser/` | Persistent Chromium profiles containing private login state |
| `data/lx-music/`, `data/sq-mysql/`, `private/sq/` | LX data, SQ database and authentication; staging in `data/sq-staging/` |
| `media/` | `music`, `youtube-music`, `youtube-video`, `bilibili-music`, `bilibili-video` |
| `data/navidrome/`, `data/emby/`, `data/lrcapi/` | Optional player databases and configuration |
| `state/`, `cache/` | Installation checkpoints, maintenance state, private logs and resumable dependency caches |

Managed paths derive from `--root`; individual path overrides cannot silently point back into production. Existing personal libraries are not adopted automatically. For additional read-only mounts, use the [media example](../services/media/personal-libraries.compose.example.yaml), explicitly record the overlay and backup scope, and verify coverage first. Keep development and runtime directories outside cloud-synced document storage.

YouTube cookies live at host `data/state/youtube/cookies.txt`, container `/state/youtube/cookies.txt`, with `cookies.txt.previous` for rollback. The persistent profile is separate at `private/youtube-browser/`. Follow the [YouTube guide](YOUTUBE.en.md): open the loopback browser through an SSH tunnel, log in manually, then have the export helper stop Chromium, validate a candidate, atomically replace the cookie file and retain its predecessor. Failed validation does not publish the candidate. Downloads use private cookie copies. Recreating containers preserves these directories but does not establish account validity.

Browsers normally stay stopped. In a managed installation the YouTube helper automatically reads recorded authentication/recovery configuration. `start` opens the browser independently; `status`, `export` and `rollback` execute through the existing bot and require it to be running. During initial pending setup, complete Apple/chat activation before exporting:

```sh
sudo bash /volume1/docker/amdl-suite/source/services/youtube/youtube-auth.sh start
# After manual login, with the bot running, use export URL with an authorized public test URL.
sudo bash /volume1/docker/amdl-suite/source/services/youtube/youtube-auth.sh status
```

## 4. Pending setup and players

```sh
sudo python3 /volume1/docker/amdl-suite/source/install.py status --root /volume1/docker/amdl-suite
sudo python3 /volume1/docker/amdl-suite/source/install.py doctor --root /volume1/docker/amdl-suite
sudo python3 /volume1/docker/amdl-suite/source/install.py resume --root /volume1/docker/amdl-suite --config /volume1/docker/amdl-private/private-config.json
```

`resume` reuses completed stages and re-evaluates configuration, activation and service checks without resetting databases or accounts. `installed` means installation stages completed. `configured` means bot activation requirements passed. `infrastructure_ready` reflects only recorded container state/health. `ready` remains false and `readiness_scope` separates real downloads. `pending` may retain generic optional YouTube/Bilibili login and authorized LX-source reminders; these do not automatically diagnose an expired login.

Optional player ports are distinct and bind only to `127.0.0.1`; use SSH tunnels remotely:

| Service | Default host endpoint | Manual setup |
| --- | --- | --- |
| Navidrome | `http://127.0.0.1:14533` | Create the first administrator, scan and validate music under `/music` |
| Emby | `http://127.0.0.1:18096` | Complete the wizard, users, libraries and scans; audio `/music`, video `/video/YouTube`, `/video/Bilibili` |
| LrcAPI | `http://127.0.0.1:28883` | Configure lyric sources as needed; local `/src` serves a static page without testing external providers |

The installer does not fabricate Navidrome/Emby users or API keys. To let the bot request scans, put your created `NAVIDROME_USER` / `NAVIDROME_PASSWORD` or `EMBY_USER` / `EMBY_PASSWORD` in private configuration, then `resume`. Selected internal services supply their URLs when unspecified; existing nonempty `NAVIDROME_URL`, `EMBY_URL` and `YT_LRCAPI_URL` are preserved. To disable an integration, explicitly retain an empty URL in the private JSON used for each invocation; a previously empty environment value alone does not prevent a later internal default. See [media configuration and sources](../services/media/SOURCES.md).

Use the installed operations entry point. It automatically reads `deployment.json`, private environment, recorded profiles and image overrides:

```sh
sudo bash /volume1/docker/amdl-suite/source/ops/stack.sh status
sudo bash /volume1/docker/amdl-suite/source/ops/stack.sh probe
```

Do not create another `.env` in `source/` and start a second Compose stack. Single-service `upgrade` still requires an independently tagged image built/pulled first. Managed image overrides live at root `config/images.compose.json`; the manual template uses `STATE_PATH/ops/images.compose.json` instead.

## 5. Complete private backup

Pause chat, manual LX/SQ entry points and other submissions. Running/stopping bot jobs or leased lyric work block backup; queued jobs may be preserved. Backup briefly stops recorded services and verifies quiescence, stopping watchdogs and bot/workers before SQ/MySQL and wrapper/VM. Success or failure triggers an attempt to restart only services that were running. Originally stopped browsers or bots stay stopped.

```sh
sudo python3 /volume1/docker/amdl-suite/source/install.py backup --root /volume1/docker/amdl-suite --destination /volume1/amdl-backups/backup-20261021
```

**Media and Docker image archives are included by default.** Private configuration, persistent state, browser profiles, cookies, wrapper sessions, VM, LX/SQ data and player configuration are also covered. File digests and a mount-coverage ledger support restore validation. Choose a new destination outside all sources. Backup does not stop unrelated projects or automatically adopt independently deployed LX/SQ projects. Older multi-project migrations require explicit project/mount inventory; see `deploy.migration inventory` / `snapshot` help.

A complete physical backup must read database files owned by container users, so sudo is normally required. MySQL's fixed regenerable socket is recorded separately in `runtime_omissions`; unknown external links still refuse backup. Restored media ownership is mapped to the selected UID/GID in the new directory while retaining file modes.

Use `--without-media` or `--without-images` only deliberately. Omissions are recorded: copy missing media separately or obtain images again. For a final migration snapshot use `--leave-stopped`: success leaves the original services stopped; failure still attempts to restart the previously running services. The older manual `ops/stack.sh backup` is a different private upgrade snapshot which excludes media by default and has different job rules.

Backups are unencrypted and contain complete credentials, login state and media. Never upload them as releases, issues or CI artifacts. Logs can also contain private data. Check `status` and `state/maintenance.json` afterwards; resolve restart failures before accepting work. Queue data is preserved, and the original service can continue queued work after restart. A completed backup does not mean the old poller is stopped.

## 6. Restore and explicit cutover

Restore into a new dedicated root, preserving the original installation. Default ports overlap, and the old database must not consume jobs after the final checkpoint. Pause external submissions and host supervisors, then take a final `--leave-stopped` snapshot and verify it before restoring. An ordinary backup that resumed the original services is not a frozen cutover point. This example uses an existing public source entry point without copying private backups into source:

```sh
sudo python3 /volume1/docker/amdl-suite/source/install.py backup --root /volume1/docker/amdl-suite --destination /volume1/amdl-backups/cutover-20261021 --leave-stopped
sudo env PYTHONPATH=/volume1/docker/amdl-suite/source python3 -m deploy.migration verify /volume1/amdl-backups/cutover-20261021
sudo python3 /volume1/docker/amdl-suite/source/install.py restore --root /volume1/docker/amdl-suite-restored --bundle /volume1/amdl-backups/cutover-20261021 --config /volume1/docker/amdl-private/private-config.json --with-media-servers
```

Confirm snapshot success and use `status` to verify the old stack is stopped. Failure may have restarted it; resolve that before cutover. Pass `--with-media-servers` explicitly to restore optional players; their archived data alone does not select the services. Restore verifies contents and coverage, publishes data under the new root and remaps mounts. Included images are loaded, checked against image IDs and reused for archived services rather than built/pulled again. Missing dependencies may still need network access. The original task database and queued states are retained rather than clearing all jobs. After activation, existing recovery logic handles resumable work; inspect interrupted/failed jobs individually.

When older multi-project service names differ, use a top-level `restore_services` object beside `env` in the same private JSON, for example `{"env":{"START_BOT":false},"restore_services":{"legacy-vm":"legacy-wrapper","wrapper-adapter":"wrapper"}}`. Keys accept an old service name or `project-name/old-service` / `project-id/old-service` for disambiguation. These are naming examples; fill them from your reviewed bundle. The mapping persists for later `resume`. Unknown persistent services, unselected media services, ambiguous mappings, conflicting data at one mount target, different images merged into one service, or mappings matching no record are rejected before restored data is written. Renaming a service does not validate old image compatibility; independently review configuration, mount targets, image entry points and account state.

**Saved `START_BOT=true` is not cutover approval.** Keep `START_BOT=false` in the private configuration above. Without approval, the bot, both watchdogs, LX/SQ applications and both lyric workers are created but stopped; databases, NetEase API, wrapper, PO provider and players can run for inspection. An explicit `START_BOT=true` in the current restore invocation's JSON already counts as approval, so do not prematurely reuse a production configuration that permits activation. After checking the new root's configuration, accounts and mounts, create a separate mode-0600 `/volume1/docker/amdl-private/cutover.json` containing `{"env":{"START_BOT":true}}`. Explicitly approve cutover:

```sh
sudo python3 /volume1/docker/amdl-suite-restored/source/install.py resume --root /volume1/docker/amdl-suite-restored --config /volume1/docker/amdl-private/cutover.json
```

The current invocation's private config must explicitly supply true; editing the restored environment alone does not release the hold. Approval releases background task writers; the bot additionally needs chat allowlists and wrapper health. Keep the old instance stopped while validating a single poller, persisted jobs, real downloads and playback. To revert, stop the new instance first and assess the old state and newly written media. Parallel infrastructure inspection requires different values for every published port, and still must not run duplicate account task entry points.

### Offline source archive and image restore

On a connected machine, save `install.py` from the fixed v0.2.0 tag and `source.tar.gz`, `source.tar.gz.sha256` and `release.json` from that release. Check provenance and checksum, then transfer them securely. `install.py` is not a separate release asset. With a known expected SHA-256, the bootstrap can install public source and restore a private bundle offline:

```sh
sudo python3 /mnt/amdl-release/install.py restore --root /volume1/docker/amdl-suite-restored --archive /mnt/amdl-release/source.tar.gz --sha256 EXPECTED_64_HEX_SHA256 --bundle /volume1/amdl-backups/cutover-20261021 --config /volume1/docker/amdl-private/private-config.json --with-media-servers
```

Replace `EXPECTED_64_HEX_SHA256` with the verified digest. An offline source archive covers public code distribution only. Fresh apt/PyPI/Go/Git dependencies, SABR, models, Ubuntu and container images may still require network access. A fully initialized private snapshot plus the default image archive can recover its saved VM, models and service images. Missing components or unfinished initialization cannot be supplied by the source archive. Keep private backups out of public `source.tar.gz`.

## 7. Validation and limits

Run `python3 scripts/check_repo.py` and `python3 scripts/run_tests.py` from source for repository/document checks and synthetic tests. `install --dry-run` lists stages; `doctor --dry-run` lists planned checks. Neither tests Docker/KVM. Actual isolated infrastructure acceptance needs a disposable Linux amd64 KVM host and dedicated directory:

```sh
sudo python3 -m deploy.acceptance --root /volume1/docker/amdl-acceptance --report /volume1/docker/amdl-acceptance-report.json --include-images
```

This builds/starts and backs up/restores real dedicated test services, consuming time and storage. It uses no production credentials and submits no new media jobs. Publish only the sanitized report, never its private directories, logs or bundles. The harness exists; **real infrastructure acceptance is under way and has not yet passed**. Use your authorized accounts to complete the [operations acceptance checklist](OPERATIONS.en.md#3-acceptance-tests) for downloads, subtitles, scans, playback and a single poller.
