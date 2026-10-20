# Deployment and operations

This guide describes the portable templates in this repository. Keep deployment-specific configuration, image digests and acceptance records privately. The initial public snapshot has not passed clean-host installation or real-account acceptance testing. Use a separate directory rather than overwriting production.

## 1. Prerequisites

Use Linux x86-64, Docker Engine and the Compose plugin. The VM approach requires `/dev/kvm` and enabled hardware virtualization. Synology KVM modules depend on the NAS model and DSM kernel; verify the actual device instead of copying modules from another machine.

Create a private `.env` with your bot token, explicit chat allowlist, media paths and required proxies. An empty Telegram allowlist permits all chats in the original application logic. Synology Chat likewise needs a webhook token and an explicit user allowlist.

```sh
cp .env.example .env
chmod 600 .env
bash ops/stack.sh check
bash ops/stack.sh build
```

Never publish expanded Compose configuration, container environment values or wrapper account responses.

## 2. Initial start and authentication

Start the VM separately and follow the [VM guide](../vm/README.md) for upstream wrapper initialization and interactive authentication. Supply your own valid Apple Music subscription. Login, 2FA and account recovery take place in your private environment.

```sh
bash ops/stack.sh up legacy-wrapper
bash ops/stack.sh status
```

Allow time for the first VM boot and cloud-init. The management endpoint binds to host loopback; use an SSH tunnel remotely. Use manager mode for initialization, then follow the VM instructions to select the persistent-instance mode before starting dependent services.

Validate the account, M3U8 and actual processing channels, then start the default services:

```sh
bash ops/stack.sh up
bash ops/stack.sh probe
```

The default stack includes the bot, wrapper adapter, legacy-wrapper VM and PO token provider. Authentication browsers use the `auth` profile; lyric services use the `lyrics` profile. Acoustic alignment additionally needs a whisper.cpp executable and models matching the configured hashes. Model weights are not included. Review the corresponding environment variables and mounts.

LX Music and SQ Music use the `lx` / `sq` profiles. The maintenance helper merges their overlays and includes their services in this project's stop/start and backup scope. Follow the [LX / SQ guide](MUSIC_SOURCES.en.md) for initialization, unified routing, read-only authentication mounts and isolated SQ staging.

Run YouTube / Bilibili authentication browsers only when login or cookie refresh is needed, and stop them afterwards. Browser profiles and exported cookies are private data. Before enabling automatic browser management or recovery, review `compose.self-heal.yaml`: Docker socket access gives containers host-level control.

## 3. Acceptance tests

A responding health endpoint is supporting evidence. Before going live:

1. Use a separate test bot to validate search, song and album selection, progress, success and failure messages.
2. Validate the required ALAC / Atmos modes with your authorized account; inspect codec, duration, metadata and playback.
3. Resubmit a track and check deduplication and output folders, including distinct versions.
4. If needed, validate YouTube / Bilibili audio and video jobs and expired-login feedback.
5. Check LRC / TTML timing against the actual audio. Verify completion, cancellation, retries and notifications; file existence alone is insufficient.
6. Scan the target folders in Navidrome / Emby and verify actual entries and playback.
7. Restart the stack and verify persisted jobs, dependency recovery and a single Telegram poller.
8. In a maintenance window, check KVM, mounts and container recovery after a host restart. This alone does not establish power-loss recovery.

Use explicitly selected test content. Inspect state before taking recovery actions.

## 4. Routine monitoring and recovery

```sh
bash ops/stack.sh status
bash ops/stack.sh probe
```

Interpret probe results alongside actual queue state, completed files and media playback. Check free space, permissions, mounts and VM initialization. A single failed download does not establish a stack-wide outage.

Optional recovery configuration is in `compose.self-heal.yaml`. Review target names, failure thresholds, startup grace and cooldowns. Reuse an existing host monitor instead of installing duplicate restart loops. Recovery should target this stack's services, without restarting the NAS or unrelated home services.

## 5. Private backups

```sh
bash ops/stack.sh backup /absolute/private-backup-directory
```

Backups pause writers and restore the previously running services; schedule a maintenance window. Verify that the backup covers configuration, task / lyric databases, cookies / browser state, wrapper sessions and VM data. Back up media separately. External mounts, external databases and extra models are not automatically covered.

Backups contain credentials and sessions. Use restricted or encrypted storage and never upload them to public GitHub. Record the source commit, Compose revision, image IDs / digests, host version and backup time. Test an offline restore.

Manage the deployed stack through `bash ops/stack.sh`: it preserves image overrides. Running `docker compose up` directly can ignore the selected upgrade image.

## 6. Upgrades and rollback

Review upstream versions, GAMDL CLI / wrapper compatibility and yt-dlp / PO provider client-server compatibility. Build a new tag and validate it without a production token or writable production mounts before replacing a known-good image.

```sh
bash ops/stack.sh upgrade bot local/apple-music-downloader-bot:reviewed-version /absolute/private-upgrade-backup
bash ops/stack.sh probe
# If acceptance fails:
bash ops/stack.sh rollback /absolute/private-upgrade-backup
```

The upgrade helper switches the selected service's image. Database migrations, configuration changes, VM updates and models require separate review. Image rollback does not undo database migrations; restore a consistent private data backup if necessary. Stop the old instance before restoring shared account state or starting a replacement poller.

## 7. Troubleshooting

| Symptom | Checks |
|---|---|
| Telegram `409 Conflict` | Find other pollers using the token; keep one instance. Repeated restarts do not fix two pollers. |
| Telegram timeouts | Check API routing, DNS and download proxies separately. Browser connectivity is insufficient. |
| Wrapper ports respond but jobs fail | Verify account login, subscription, session, M3U8 and actual processing channels. Never publish `/me` responses. |
| VM fails to boot | Check `/dev/kvm`, hardware virtualization, disk space, permissions and cloud-init. |
| Files missing from media servers | Compare host/container mounts, scan roots, actual files and permissions. |
| YouTube authentication / PO errors | Check client/provider versions, routing and cookies; sign in on demand and stop the browser afterwards. |
| Lyric queue backlog | Distinguish source matching, reference text, model readiness, worker heartbeats and queue leases. Preserve failure evidence before changing state. |
| Healthy containers but no results | Inspect real jobs, completed files, playback, lyric timing and library scans. |

## 8. Migration

Back up the old environment, restore private state and matching images, then check UID/GID, media roots, mappings, model hashes, KVM and proxies. Stop the old poller before enabling the new bot. Retain the old deployment and rollback material until actual jobs pass. VM disks and private state do not belong in the open-source repository.
