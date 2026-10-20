# Apple Music Downloader Bot

[中文](README.md) · English

A self-hosted Telegram / Synology Chat media ingestion service for a NAS. It accepts links and search requests, processes jobs through GAMDL, yt-dlp and a local wrapper service, and writes media, artwork and lyrics to configured folders.

This repository packages a customized NAS deployment into source code, portable deployment templates and an operations guide. **The initial snapshot was assembled from local maintenance material dated through 2026-10-19. The NAS public IPv6 connection was unavailable during preparation on 2026-10-21, so the final live files could not be compared. A clean container build and real-account acceptance test are also pending.** Treat this as a source snapshot requiring integration validation.

## Features

- Apple Music text search, paginated selection, song / album / playlist jobs, ALAC and Atmos modes.
- Persistent task state, library indexing and deduplication, progress cards and failure reporting.
- YouTube / YouTube Music and Bilibili audio or video ingestion, with optional Navidrome / Emby integration.
- LRC / TTML lyrics, word-timed lyric matching, persistent queues, retries and notifications.
- A wrapper protocol adapter, KVM VM provisioning support, on-demand authentication browsers and service-scoped recovery.
- Documented backups, upgrades, rollbacks and troubleshooting.

Some features require separate services, model files or account authorization. See the configuration and [operations guide](docs/OPERATIONS.en.md). No media, Apple accounts, cookies, device credentials, wrapper sessions, VM disks or model weights are included.

## Architecture

Telegram / Synology Chat → bot → GAMDL → wrapper adapter → user-authorized wrapper / KVM VM. The bot also dispatches yt-dlp tasks and persistent lyric jobs. Finished files are written to media folders that your media servers can scan. See the diagram in the [Chinese README](README.md#架构).

## Before deployment

Use a Linux x86-64 NAS with Docker Compose and sufficient storage. The bundled VM approach requires `/dev/kvm`. Read the [operations guide](docs/OPERATIONS.en.md), review `compose.yaml` and `.env.example`, then supply your own storage paths, credentials and network configuration.

```sh
cp .env.example .env
chmod 600 .env
# Set the bot token, allowed chat IDs, storage paths and other settings.
docker compose config --quiet
docker compose build
```

Do not paste the full output of `docker compose config` into a public issue: expanded configuration can contain credentials. Apple Music requires your own valid subscription, initialized wrapper and account login. Running containers alone do not establish that downloads work. Follow the operations guide for the first start.

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

These steps install test dependencies; service dependencies are in `requirements.txt` and the Dockerfile. Tests use synthetic fixtures and temporary directories with Python socket connections blocked. Offline tests do not replace authenticated downloads, VM cold starts, media scanning or power-loss recovery checks. See [SOURCE_SNAPSHOT.md](docs/SOURCE_SNAPSHOT.md) for provenance and validation limits.

## Documentation and licensing

- [Operations guide](docs/OPERATIONS.en.md) · [中文运维手册](docs/OPERATIONS.zh-CN.md)
- [Source snapshot and verification scope](docs/SOURCE_SNAPSHOT.md)
- [Sensitive information handling](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

Original repository code is available under the [MIT License](LICENSE). Third-party components, the OpenCC-derived dictionary and separately obtained tools retain their respective licenses. Use the software only with content you are entitled to retain and follow the access rules of the services you use. This project is not affiliated with Apple, Google, Bilibili or Synology.
