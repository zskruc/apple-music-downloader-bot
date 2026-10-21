# Apple Music Downloader Bot

[中文](README.md) · English

A self-hosted Telegram / Synology Chat media ingestion service for a NAS. It accepts links and search requests, processes jobs through GAMDL, yt-dlp and a local wrapper service, and writes media, artwork and lyrics to configured folders.

This repository packages a customized NAS deployment into source code, portable templates and operations tools. **v0.2.0 adds one-command installation, complete private backups and migration restore. An isolated Linux amd64 / KVM run passed full installation, configuration resume, backup with images/media and restore into a new root; see the [evidence and scope](docs/DEPLOYMENT_ACCEPTANCE_20261021.md). Account login, real downloads, subtitle quality and playback still need operator validation.** Initial maintenance material was dated through 2026-10-19. A read-only IPv6 check on 2026-10-21 compared live source, mounts, dependencies and existing media; that [live check](docs/LIVE_CHECK_20261021.md) does not establish fresh-install acceptance.

## Features

- Apple Music text search, paginated selection, song / album / playlist jobs, ALAC and Atmos modes.
- LX Music and SQ Music adapters and optional backend deployments; unified single-song routing attempts the same recording in Apple → LX → SQ lossless order.
- Persistent task state, library indexing and deduplication, progress cards and failure reporting.
- YouTube / YouTube Music and Bilibili audio or video ingestion, with optional Navidrome / Emby integration.
- Manual/automatic YouTube captions, Chinese or Chinese-plus-original-language SRT/ASS sidecars; persistent browser sessions, candidate cookie validation, atomic updates and rollback.
- Optional SABR compatibility with a pinned upstream tool and preserved notices; separate build-install and runtime opt-ins.
- LRC / TTML lyrics, word-timed lyric matching, persistent queues, retries and notifications.
- A wrapper protocol adapter, KVM VM provisioning support, on-demand authentication browsers and service-scoped recovery.
- One-command preparation of the full suite, resumable installation, private backups including media and images by default, explicit restore cutover, and single-service upgrades and rollback.

Some features require network access and account authorization. The [one-command installer](docs/ONE_COMMAND.en.md) obtains pinned dependencies and model weights and builds services on the deploying machine. No media, Apple accounts, cookies, device credentials, wrapper sessions, VM disks or model weights are included in the repository.

## Architecture

Telegram / Synology Chat → bot → GAMDL → wrapper adapter → user-authorized wrapper / KVM VM. Optional LX and SQ backends supply supplementary lossless recordings. The bot also dispatches yt-dlp tasks and persistent lyric jobs. Finished files are written to media folders that your media servers can scan. See the diagram in the [Chinese README](README.md#架构).

## One-command deployment

Use Linux amd64 with readable/writable `/dev/kvm`, Docker Engine, Compose 2.20+, Python 3.8+, Git and curl. Defaults require 50 GiB free disk and 6 GiB available RAM. Select a dedicated local directory and an account allowed to operate Docker and KVM.

The example uses Synology's `/volume1` data volume to keep services and VM disks off the DSM system partition. On another Linux host or storage pool, substitute a dedicated local data-disk path with sufficient free space. Run the fixed v0.2.0 release:

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/zskruc/apple-music-downloader-bot/v0.2.0/install.py | sudo python3 - --root /volume1/docker/amdl-suite --with-media-servers'
```

This prepares the bot, wrapper / KVM VM, LX, SQ / MySQL / NetEase API, YouTube PO provider, two lyric workers, on-demand login browsers and recovery configuration. `--with-media-servers` adds Navidrome, Emby and LrcAPI with loopback-only management ports. Accounts, 2FA, authorized music sources and the Emby wizard remain manual. The bot stays stopped without a safe chat entry point and a healthy wrapper; status lists outstanding configuration. This example installs with sudo; use the same privileges for later maintenance.

The [complete guide](docs/ONE_COMMAND.en.md) covers private JSON configuration, directory layout, resume, persistent cookies, offline backups including media and images, source archives and restore cutover. The [operations guide](docs/OPERATIONS.en.md) retains manual Compose setup. The full suite enables lyrics, SABR and automatic authentication configuration, unlike the manual template's opt-in defaults. Its Docker socket configuration requires a trusted deployment host.

**Only one poller may use a Telegram bot token.** Back up and plan a cutover when replacing an existing service. Use a separate test bot for isolated acceptance testing.

## Usage

Send an Apple Music URL or a song name, or use `/search`, `/alac`, `/atmos`, `/both` and `/status`. YouTube and Bilibili links can be handled as music or video; `/ytmusic` and `/ytvideo` select the intended use. The source and the bot's help output define the supported behavior.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python scripts/check_repo.py
.venv/bin/python scripts/run_tests.py
```

These steps install test dependencies; service dependencies are in `requirements.txt` and the Dockerfile. Tests use synthetic fixtures and temporary directories; Python socket connections are restricted to loopback fixtures, with external addresses blocked. Separate [real deployment acceptance](docs/DEPLOYMENT_ACCEPTANCE_20261021.md) covers VM boot, configuration changes and full backup/restore. It does not test account login, content downloads, subtitle quality, playback or host power-loss recovery. See [SOURCE_SNAPSHOT.md](docs/SOURCE_SNAPSHOT.md) for provenance and validation history.

## Documentation and licensing

- [Operations guide](docs/OPERATIONS.en.md) · [中文运维手册](docs/OPERATIONS.zh-CN.md)
- [One-command installation, backup and restore](docs/ONE_COMMAND.en.md) · [中文指南](docs/ONE_COMMAND.zh-CN.md)
- [Pinned dependencies and licensing](deploy/SOURCES.md) · [Private configuration example](deploy/private-config.example.json)
- [LX / SQ and unified music](docs/MUSIC_SOURCES.en.md) · [中文配置指南](docs/MUSIC_SOURCES.zh-CN.md)
- [LX backend deployment](services/lx-music/README.md) · [SQ backend and task patch](services/sq-music/README.md)
- [YouTube Music, video, subtitles and cookies](docs/YOUTUBE.en.md) · [中文指南](docs/YOUTUBE.zh-CN.md)
- [Deployment and restore acceptance record](docs/DEPLOYMENT_ACCEPTANCE_20261021.md)
- [Source snapshot and verification scope](docs/SOURCE_SNAPSHOT.md)
- [Sensitive information handling](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

Original repository code is available under the [MIT License](LICENSE). Third-party components, the OpenCC-derived dictionary and separately obtained tools retain their respective licenses. Use the software only with content you are entitled to retain and follow the access rules of the services you use. This project is not affiliated with Apple, Google, Bilibili or Synology.
