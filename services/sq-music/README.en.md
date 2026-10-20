# SQ Music Plus service and operations

[中文](README.md)

This optional backend supplies the existing `bot/sq_music.py` adapter, unified search, and single-track lossless fallback. The order is Apple → LX → SQ after a confirmed missing/unavailable recording. Apple authentication and network failures keep their original retry/error handling. SQ writes to isolated staging. The bot verifies recording identity, task ownership, the actual path, FLAC format, and full decoding before publishing under the library's `SQ Music` directory.

This directory contains original transformation scripts, version references, a Compose overlay, initialization helpers, and operations documentation. **Upstream Java sources and images are not redistributed.** No license was found in the audited SQ revision. This repository's MIT license covers its original work and does not grant redistribution rights to SQ or other dependencies. Review upstream terms before obtaining/building them. See [sources and versions](SOURCES.md).

## Audited baseline

- Upstream: [`59799517/simple_sq_music_plus`](https://github.com/59799517/simple_sq_music_plus), backend `3.1.33`, commit `cf0f6ed136be0aa13845bbab028c27e404b89d5d`.
- Official base: `ghcr.io/59799517/simple_sq_music_plus@sha256:4f3e499c07da526bea5648fc4e1b9c449e363356c5e8c1ef4d96c2d67b1e20f2`.
- Database: retain the audited MySQL `5.7` baseline and supply a pinned image digest. No major-version migration is provided.
- NetEase API: upstream `v4.32.0` baseline; the example contains the digest verified during deployment.
- Retained local records from 2026-10-17 cover real NetEase search, enqueue, completion polling, full FLAC decoding, isolated publishing, and duplicate reuse. The 2026-10-20 local fleet snapshot still shows the same SQ deployment running. Publication preparation did not connect to the NAS, rerun downloads, or prove a fresh installation.
- Only `netease` is enabled for the bot by default. Other SQ providers do not have equivalent validation. SQ's native NetEase extension remains upstream; no external source scripts, sessions, platform accounts, or media are included here.

## 1. Build the bridge locally

Run from this repository's root on a local work disk. For this workstation, use `LocalWork`.

```sh
git clone --filter=blob:none https://github.com/59799517/simple_sq_music_plus.git services/sq-music/upstream
git -C services/sq-music/upstream checkout cf0f6ed136be0aa13845bbab028c27e404b89d5d
python3 services/sq-music/prepare_build.py services/sq-music/upstream
docker build -t apple-music-downloader-sq:3.1.33-bot services/sq-music
docker image inspect apple-music-downloader-sq:3.1.33-bot --format '{{.Id}}'
```

Put the resulting local image ID in the root `.env` as `SQ_PATCHED_IMAGE`. For deployment elsewhere, review redistribution rights and use an audited image digest. The ignored `upstream/` and generated `patched/` directories must not be committed or attached to public releases.

The transformation checks the SHA-256 of both original files and refuses unknown source structures. It adds a `${taskId}` directory variable, records the actual `downloadFile` after tags/lyrics finish, and adds `GET /api/task/bot/{id}` under ordinary login authentication. The endpoint returns `bridgeVersion=1`, task/child summaries, and the actual path only for final `success`. Enqueue acceptance, `supplement_success`, display names, another task's path, symlinks, and non-FLAC results are rejected by the bot. Docker compiles the overrides using the fixed image's existing JDK 21/dependencies. Re-audit these contracts before upgrading upstream.

## 2. Prepare configuration and private files

Merge [.env.example](.env.example) into the **root** `.env`, or use the SQ block already present in the root example. Set `SQ_PATCHED_IMAGE`, an audited MySQL 5.7 digest, and these absolute paths; leave the secret placeholders for the helper:

| Variable | Host content | Container mapping |
| --- | --- | --- |
| `SQ_STAGING_PATH` | Isolated SQ staging | SQ `/music` writable; bot `/sq-staging` read-only |
| `SQ_MYSQL_DATA_PATH` | Separate empty database directory | `sq-mysql:/var/lib/mysql` |
| `SQ_AUTH_PATH` | Private JSON file | Bot `/run/secrets/sq-auth.json` read-only |

Keep staging, database, and secret directories separate. Staging must also be outside the final `MUSIC_PATH`. Both containers must see the same staging directory. The bot publishes to `/music/SQ Music` through the main Compose library mount.

```sh
python3 services/sq-music/bootstrap.py prepare
bash ops/stack.sh --profile sq check
```

`prepare` creates directories, independent random database/root/JWT secrets, and a mode-`0600` auth file. Existing credentials are preserved. If database files exist without matching secrets, generation is refused. It does not start services, contact the NAS, or print credentials. Auth JSON contains two nonempty strings: `{"username":"YOUR_SQ_USERNAME","password":"YOUR_PRIVATE_PASSWORD"}`. This is the independent SQ login, not platform cookies or a JWT. Never commit/print this file or the root `.env`.

## 3. Initialize a new isolated deployment

```sh
bash ops/stack.sh --profile sq up sq-mysql sq-neteasecloudmusicapi sqmusic-server
# Wait for the backend to create its initial schema, then configure this new DB:
python3 services/sq-music/bootstrap.py configure --new-database
bash ops/stack.sh --profile sq stop sqmusic-server
bash ops/stack.sh --profile sq up sqmusic-server
# Verify backend authentication/completion, then set AMDL_UNIFIED_MUSIC_ENABLED=true in root .env:
bash ops/stack.sh --profile sq up bot
bash ops/stack.sh --profile sq probe
```

`configure` checks required configuration rows, applies `bootstrap-config.sql`, and sets the independent login through process standard input. SQL and passwords are not printed. It does not stop services or replace media. Use it only for a newly initialized, isolated SQ database. Restart the backend to refresh its configuration cache.

The overlay joins the main project's default Docker network. Use `http://sqmusic-server:8099`: **Tomcat rejects HTTP Host names containing underscores**, including the old container name. The backend host port binds only to `127.0.0.1:18099`. Database/API ports are not published, and MCP is disabled. Memory limits are approximately 1100/600/300 MB for backend/database/API; CPU CFS settings unsupported by some DSM hosts are omitted.

The bootstrap limits concurrency to one and automatic retries to zero. Upstream cross-provider substitution, quality downgrade, collection synchronization, and automatic downloading are disabled. The validated native NetEase extension is retained. Validate a source/account you are entitled to use; a provider's FLAC declaration is not proof that its file passes media validation.

## Validation and operations

Before enabling the bot, verify in isolated staging: anonymous task access is denied; the independent login works; real search declares FLAC; enqueue progresses to final success; the first path component equals the task ID; full decoding succeeds before atomic publication; and reuse avoids duplicate downloads. Confirm that Apple authentication/network failures retain their error, cancellation prevents publication, and different recordings/versions are not merged. `probe` provides health summaries and does not replace these checks.

```sh
bash ops/stack.sh --profile sq backup /srv/private-backups/amdl-sq-YYYYMMDD
bash ops/stack.sh --profile sq stop sqmusic-server sq-neteasecloudmusicapi sq-mysql
```

Always supply the same `--profile sq` used for deployment. Pause submissions from the bot and any external SQ clients, then verify both queues are empty. The operations backup includes the root `.env`, SQ Compose, `SQ_AUTH_PATH`, and offline `SQ_MYSQL_DATA_PATH`. Download staging is excluded by default; copy it separately into a private backup while SQ is stopped if needed. Never publish backups. Retain the exact base reference, bridge image ID, and patch version. Upgrade only after isolated source-hash, authentication, completion-path, and full FLAC checks; do not switch to `latest` or apply the patch blindly. Database major upgrades require a separate restore rehearsal.

To close new unified search entry points, set `AMDL_UNIFIED_MUSIC_ENABLED=false` in the root `.env` and update the bot with the SQ profile. Finish or explicitly stop existing persisted tasks. The `sq` profile enables the bot's SQ switch. To remove the integration entirely, update the bot without `--profile sq` while retaining any other needed profiles, then stop the SQ services independently and preserve their data. Rollback retains older images and current data; do not overwrite new tasks with an old database. Cancellation/timeout stops bot polling/publication, but an upstream download already in progress may finish into its own staging directory. Cleanup must respect task ownership and retention and must not move active files.

Static/isolated checks are not deployment acceptance. Run `python3 -m unittest discover -s tests/bot -p test_sq_music.py` as an initial check; verify accounts, sources, actual completed paths, and media decoding on your deployment host.

Offline helper checks: `python3 -m unittest discover -s services/sq-music -p 'test_*.py' -v`. Tests use temporary directories and mocked database calls to cover private permissions, credential preservation, existing-database protection, unknown source/schema refusal, and SQL delivery through standard input.
