# 运维脚本 / Operations

所有命令在本项目目录使用 `bash ops/stack.sh`。脚本通过 Compose 项目标签和固定服务白名单限制操作；不会扫描或重启其他 NAS 项目，不安装 timer/cron，也不启用任何现有主机守护。

可选 `--profile lx` 与 `--profile sq` 会自动合并 `services/` 下的对应配置。LX/SQ 与主项目共用网络并纳入服务白名单；SQ 应用会在 MySQL 之前停止、在 MySQL 之后恢复。使用与部署相同的 profile 备份，私有快照会包含 LX 数据、SQ 数据库、鉴权文件和叠加配置；不包含公开音乐、SQ 暂存、日志和缓存。先暂停所有入口并确认后端手动队列为空，不能只检查 Bot 队列。完整初始化和验收见 [专项指南](../docs/MUSIC_SOURCES.zh-CN.md)。

```sh
bash ops/stack.sh check
bash ops/stack.sh build
bash ops/stack.sh up legacy-wrapper
# 完成 vm/README.md 的首次登录和 single 模式初始化后：
bash ops/stack.sh up
bash ops/stack.sh probe
bash ops/stack.sh --profile auth up youtube-auth-browser
bash ops/stack.sh --profile auth stop youtube-auth-browser
bash ops/stack.sh --profile lyrics up word-lyrics-resolver word-lyrics-nas-aligner
bash ops/stack.sh --self-heal up wrapper-watchdog bot-watchdog
bash ops/stack.sh backup /srv/private-backups/amdl-YYYYMMDD
bash ops/stack.sh upgrade bot your-registry/example:reviewed-release /srv/private-backups/amdl-upgrade-YYYYMMDD
bash ops/stack.sh rollback /srv/private-backups/amdl-upgrade-YYYYMMDD
```

`check` 只打印校验结果，避免泄露展开后的 `.env`；配置了 Telegram token 时要求非空 chat allowlist，启用 Synology Chat 时要求 outgoing token 和用户 allowlist。首次仅初始化 VM 时可以尚未填写 bot token。`probe` 显示容器状态、重启次数、任务状态计数及 bot 健康摘要，不做真实内容下载。

`backup` 是有短暂停机的私有离线快照。它先检查 bot 是否还有 queued/running/stopping 任务和歌词 leased 任务；有则拒绝。请先暂停外部提交并等任务结束，以免预检后又出现新任务。随后停止本栈原本运行的容器，备份 `.env`、Compose 文件、state、wrapper session、VM 与浏览器 profile，再恢复原先运行的容器。媒体库不纳入备份；文件权限是私有目录 0700/文件 0600，但未加密。目标目录必须是仓库外的新目录，不得放在备份源内部。即使备份失败也会尝试恢复服务，失败后应运行 `status` 检查。启用了额外 profile、`--auth-refresh` 或 self-heal 时，备份、升级和回滚都应传同样选项。

`upgrade` 只替换明确指定的一个现有服务，候选镜像必须已拉取或构建。先做完整私有快照并给旧 image ID 创建保留标签，写好回滚记录，保持所有写入者停止，再以 `--no-start --no-deps --no-build --pull never` 创建候选容器，最后恢复原先运行的服务。创建失败时先尝试恢复旧镜像再恢复服务；不会批量升级、清理镜像或修改数据结构。服务若原本停止，更新后继续停止。升级记录及 image 覆盖位于 `STATE_PATH/ops/images.compose.json`，之后应继续使用此脚本，直接 `docker compose up` 会忽略该覆盖。`rollback` 只将目标服务恢复为记录的旧镜像，保留当前数据；数据或 schema 恢复需要人工离线评估备份，不自动覆盖当前状态。使用任何 upgrade 前自行核对数据格式向后兼容性。

歌词 `lyrics` profile 需要模型文件；不随仓库分发或自动下载。默认主模型是 `ggml-large-v3-turbo-q5_0.bin`（SHA-1 `e050f7970618a659205450ad97eb95a18d69c9ee`），fallback 模型为 `ggml-large-v3-q5_0.bin`（代码要求 SHA-1 `e6e2ed78495d403bef4b7cff42ef4aaadcfea8de`）。模型来源及使用条款由部署者核对。Dockerfile 从固定 whisper.cpp 提交构建 CPU CLI；Mac/MLX backend 不属于该 Linux 镜像。开启自动歌词任务前先启用并验证 worker，再设置 `.env` 中的 automation/completion 开关。

登录浏览器只有 loopback HTTPS 端口，按需启用 `auth` profile。YouTube 使用 `bash services/youtube/youtube-auth.sh start` 登录，再用 `export URL` 停浏览器、验证候选 Cookie 并原子替换；`status` 只显示状态，`rollback URL` 验证并恢复上一份 Cookie。工具对现有 bot 执行导出，保留镜像覆盖；详细音视频、字幕及持久化步骤见 [YouTube 指南](../docs/YOUTUBE.zh-CN.md)。哔哩哔哩仍使用对应的 `nas_bilibili_auth.py`。默认不允许 bot 操作 Docker；自动认证需显式设置 `ONLINE_AUTH_AUTO_REFRESH_ENABLED=true` 并用 `--auth-refresh` 应用 `compose.auth-refresh.yaml`。这个选项只开放按需浏览器管理，不新增 watchdog；已有 `--self-heal` 部署也提供 socket。启停、备份、升级、回滚和认证工具均沿用部署时相同选项。

Docker socket 挂载具有主机管理权限，**不是权限隔离边界**；即便脚本和 watchdog 只使用固定名称，也应将宿主视为可信。NAS 已有巡检时避免再运行重复 watchdog；本仓库不自动替换或新增 NAS 守护。

## English

Use `bash ops/stack.sh` from this project. Commands are limited to the current Compose project and an explicit service allowlist. No host timer/cron is installed, and no unrelated NAS service is modified.

Optional `--profile lx` / `--profile sq` automatically merge their service overlays. The services share this project's network and maintenance allowlist. SQ stops before MySQL and resumes after it. Backups with the matching profiles include LX private data, the SQ database, credential files and overlay configuration; media, SQ staging, logs and caches are excluded. Pause submissions and verify manual backend queues are idle as well as bot jobs. See the [music source guide](../docs/MUSIC_SOURCES.en.md).

`check` validates without printing expanded secrets. Configured Telegram credentials require a chat allowlist; enabled Synology Chat requires an outgoing token and user allowlist. `probe` reads container status, restart counts, database task counts and the bot health summary. It does not verify account entitlement or a real download.

`backup DIRECTORY` takes a private offline snapshot with brief downtime and restores the originally running services afterward. It refuses active bot jobs and leased lyric work. Stop external submissions first to avoid a new job arriving after the preflight check. It includes configuration/secrets, state, wrapper sessions, the VM, and browser profiles, but excludes media libraries. Use a new destination outside the checkout and data sources. Backups have private permissions, are **not encrypted**, and must never be published. Check `status` after any failed backup.

`upgrade SERVICE IMAGE BACKUP_DIRECTORY` requires an existing service and an already available candidate image. It snapshots private data, retains the old image under a rollback tag, and writes recovery metadata while all writers remain stopped. It creates the selected candidate without starting it, pulling/building, or replacing dependencies, then resumes the previously running services. If creation fails, it attempts to recreate the old image before resuming. A stopped service stays stopped. Updates are saved in `STATE_PATH/ops/images.compose.json`; continue using this script afterward so the overrides remain effective. `rollback BACKUP_DIRECTORY` reverts the selected image only, preserving current data. Database/schema restoration requires separate offline review; the script never overwrites current data automatically. Supply the same `--profile` / `--auth-refresh` / `--self-heal` options used for deployment. With no service names, `stop` discovers all containers carrying this project's label and stops watchdogs first, including previously enabled profiles.

The `lyrics` profile requires operator-provided primary/fallback models matching the hashes listed above. Model files are neither distributed nor automatically downloaded. whisper.cpp is built from a fixed upstream commit for CPU execution; the optional Mac/MLX backend is outside this Linux image. Verify both workers before enabling automatic lyric jobs.

The `auth` profile starts a loopback-only HTTPS browser on demand. Use `bash services/youtube/youtube-auth.sh start`, followed by `export URL`, `status` or `rollback URL` for YouTube. The host helper confirms Chromium has stopped and executes export inside the existing bot, preserving image overrides. See the [YouTube guide](../docs/YOUTUBE.en.md) for media, subtitles and persistent sessions. Bilibili retains its separate `nas_bilibili_auth.py` helper. Automatic browser control requires `ONLINE_AUTH_AUTO_REFRESH_ENABLED=true` and the explicit `--auth-refresh` socket overlay; this does not enable watchdogs. Existing `--self-heal` deployments also provide socket access. Reuse the deployment options for maintenance and authentication commands. Socket access grants host-level Docker administration, irrespective of the application's service-name allowlist. Avoid duplicate watchdogs when existing NAS supervision already handles this stack.

Only static configuration, syntax and mocked maintenance tests were run during publication preparation. Image builds, KVM boot, wrapper provisioning/login, browser login, live backup/restore, container health and real downloads still require validation on the deployment host.
