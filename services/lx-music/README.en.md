# LX Music / LXServer

[简体中文](README.md)

This directory contains optional LXServer deployment, private configuration initialization, read-only checks, and upgrade/rollback instructions. Bot integration lives in [`bot/lx_fallback.py`](../../bot/lx_fallback.py) and [`bot/ingest_bot.py`](../../bot/ingest_bot.py): search, persistent download queues, source/quality checks, and artist/album organization.

## Upstream and version

- Project: [XCQ0607/lxserver](https://github.com/XCQ0607/lxserver).
- Source: [v2.1.2, commit `9006e61d73f15764b8eb1388efbd55d8d3271370`](https://github.com/XCQ0607/lxserver/tree/9006e61d73f15764b8eb1388efbd55d8d3271370).
- Default official image: `ghcr.io/xcq0607/lxserver@sha256:aa3db966049584585639ca6563e85ce5b7ebf33569dd1f5064c21bd7330382af`.
- Upstream license: Apache-2.0; [license copy](LICENSE.upstream.txt). Original helpers and Bot integration use this repository's MIT license.

The digest and runtime version come from the retained 2026-10-19 deployment verification. Upstream source references were checked on 2026-10-21. This publication did not reconnect to the NAS or validate a fresh deployment. Server source and image layers remain external. The retained upgrade records contain no additional LXServer source patch; our customizations are the Bot client, path mappings, and maintenance workflow. See [upstream.json](upstream.json).

## New installation

Run commands from the repository root. Set `LX_DATA_PATH`, `LX_FRONTEND_AUTH_PATH`, and `MUSIC_PATH` in the root `.env` to writable local host paths. These commands use the `/srv/apple-music-downloader` example from `.env.example`; adjust the arguments when changing those settings. State, credentials, and media belong outside the Git checkout.

```sh
python3 services/lx-music/initialize.py \
  --data-path /srv/apple-music-downloader/lx-music \
  --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth
bash ops/stack.sh --profile lx check
bash ops/stack.sh --profile lx up lxserver
python3 services/lx-music/check.py \
  --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth
```

The initializer creates a random password, private `data/config.js`, and a mode-0600 Bot authentication file. It never prints credentials and refuses an existing configuration, authentication file, or nonempty data directory. Admin and player initially share this generated password; synchronization users start empty. Add separate synchronization users in the admin interface if needed. The initialized configuration takes precedence over bundled default accounts.

The management port is bound to host `127.0.0.1:19527`; use an SSH tunnel to access `/admin`. Import a source you are authorized to use in the admin UI and confirm search works. Source scripts, private API endpoints/tokens, and media are not distributed here. Starting the server alone does not establish source availability.

After completing the root project's Bot credentials and Apple Music setup, start the stack with `bash ops/stack.sh --profile lx up`. Selecting this overlay enables LX fallback. To enable unified Telegram text search, also set `AMDL_UNIFIED_MUSIC_ENABLED=true` in `.env` and configure the relevant entry point using the root documentation. Lossless quality tiers are preferred; `AMDL_LX_FALLBACK_ALLOW_320` controls whether 320k is permitted.

Direct Compose usage requires the same overlays every time. After an operations-managed upgrade, continue using the operations helper so saved image overrides remain effective:

```sh
docker compose -f compose.yaml -f services/lx-music/compose.yaml --profile lx up -d lxserver
```

## Existing installation

Preserve the active `data/config.js`, `users.json`, user directories, playlists, `serverInfo.json`, and persistent download queue. Do not run initialization against production data. Quiesce writers, take a private backup, and map the existing state to `LX_DATA_PATH`. Upstream migrates a legacy root `config.js` only when the active data-directory configuration does not exist.

The host file selected by `LX_FRONTEND_AUTH_PATH` must contain the current `frontend.password` value as one nonempty UTF-8 line; a trailing newline is allowed. It is not JSON, a synchronization user's password, or the player password. Use mode 0600 and grant the Bot container user read access. The Bot sends it as `x-frontend-auth`, uses `x-user-name: default`, and writes downloads under the public `_open` user's music directory. Update this secret and restart the Bot whenever the admin password changes.

This template does not automatically import or alter an existing deployment. Review Compose project ownership, mount paths, and writers before moving a separately managed service. Never run two LXServer instances against the same writable state.

## Shared path contract

| Host path or setting | LXServer | Bot |
| --- | --- | --- |
| `${MUSIC_PATH}/LX Music` | `/server/music/_open` | `/music/LX Music` |
| `${LX_DATA_PATH}/data` | `/server/data` | Not mounted |
| `${LX_FRONTEND_AUTH_PATH}` | Its value matches `frontend.password` in `data/config.js` | `/run/secrets/lx-frontend-auth`, read-only |

The Bot uses `http://lxserver:9527`; the default project network also provides the `lxserver-test` alias. Its `AMDL_LX_FALLBACK_SERVER_MUSIC_ROOT=/server/music/_open` must exactly match returned server paths. Avoid adding a second `_open` level to the shared host directory. An existing external service using `/server/music` or another root requires matching changes to both mounts and Bot mapping. A mismatch can leave the queue completed while the Bot cannot find the file.

## Checks and maintenance

`check.py` performs authenticated GET requests for configuration and the persistent queue. It blocks redirects, ignores environment proxies, and never prints credentials or response bodies. Output contains readiness, task count, and busy-task count only. The container health check only establishes HTTP availability; integration validation also needs authenticated queue access, shared files, index consistency, and a working authorized source.

Pause new submissions and wait for both Bot and LX jobs to finish before upgrading. Historical `error`, `finished`, `exists`, and canceled tasks are allowed. Do not clear queue history to make an upgrade proceed.

```sh
python3 services/lx-music/check.py \
  --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth --require-idle
bash ops/stack.sh --profile lx backup /srv/private-backups/amdl-lx-before-upgrade
```

The root helper snapshots LX `/server/data` and the Bot authentication file. These contain private accounts, configuration, playlists, and queue state. Back up media separately. Existing deployments with additional root configuration, source scripts, or mounts need corresponding private backups. Treat raw server logs as private too.

Review candidate configuration/data compatibility. Validate a copy of private state in isolation, ensuring existing account, playlist, and queue fields survive, then pre-pull the reviewed image on the deployment host:

```sh
bash ops/stack.sh --profile lx upgrade lxserver REGISTRY/IMAGE@sha256:REVIEWED_DIGEST /srv/private-backups/amdl-lx-upgrade
bash ops/stack.sh --profile lx status
python3 services/lx-music/check.py --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth
```

Verify runtime version/digest, existing users and playlists, queue history, and unchanged mounts. Test an authorized song to confirm file/index organization. API checks do not replace a real download acceptance test. If necessary, `bash ops/stack.sh --profile lx rollback /srv/private-backups/amdl-lx-upgrade` restores only the image while retaining current data. Restore a data snapshot only after an offline review to avoid discarding newer changes. Preserve retained images, user directories, and media during recovery.

Configuration references: [v2.1.2 example](https://github.com/XCQ0607/lxserver/blob/9006e61d73f15764b8eb1388efbd55d8d3271370/config.js), [startup and persistence](https://github.com/XCQ0607/lxserver/blob/9006e61d73f15764b8eb1388efbd55d8d3271370/src/index.ts).
