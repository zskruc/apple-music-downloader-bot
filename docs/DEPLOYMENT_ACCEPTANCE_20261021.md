# Deployment acceptance / 部署验收 · 2026-10-21

## 中文

在隔离的 Linux amd64 Docker / KVM 环境完成一次完整基础设施验收。验收使用新的专用安装根、私有合成状态和媒体标记，不使用生产账号，不启动 Bot 轮询器，也不提交新的媒体下载任务。

| 证据 | 固定记录 |
| --- | --- |
| 完整运行 | [39558116855](https://github.com/zskruc/apple-music-downloader-bot/actions/runs/39558116855) |
| 验收源码 | [1d4df6e718851e811225563f9f5134eee46f2f5f](https://github.com/zskruc/apple-music-downloader-bot/commit/1d4df6e718851e811225563f9f5134eee46f2f5f) |
| 完成时间 | 2026-10-21 07:08:13 UTC（北京时间 15:08:13） |
| 脱敏报告 | [仓库内永久副本](deployment-acceptance-20261021.json)，与验收 artifact 的 `report.json` 字节一致 |
| 报告 SHA-256 | `2a3d5f25f844ae079e4a0273d58195e7bf67343089efc358109bb1745e828504` |
| 完整模式 | `kind=real-docker-kvm-acceptance`、`passed=true`、`stage=completed`、`images_in_bundle=true` |

报告的 20 项检查全部为 true，未记录最终 failure 或 cleanup attention，且 `private_artifacts_uploaded=false`。报告保留的 `last_readiness_failure` 是启动等待中的暂态 health 重试，容器状态快照中也可能保留 `health=starting`；这不表示所有观察时刻的容器都已 healthy。最终 `passed=true`、`stage=completed` 和全部检查才是本次结果。相同源码的[源码 CI 39558088207](https://github.com/zskruc/apple-music-downloader-bot/actions/runs/39558088207)另通过 733 项隔离测试与隐私检查。

已验证的范围：

- 从公开源码准备完整套件，实际构建/启动 Docker 服务，检查核心与可选媒体服务的本地 HTTP 接口。
- 启动真实 KVM Ubuntu guest，通过 SSH 和 manager 协议检查；执行配置 `resume`，将 VM CPU 从 2 调整到 1，核对 guest 内生效且原磁盘标记保留。
- 创建包含私有状态、媒体标记与 Docker 镜像归档的离线快照；删除原安装登记的容器后，恢复到全新的根目录。
- 保留原合成任务 ID 和 queued 状态、LX/SQ 鉴权摘要、媒体与 VM guest 标记；恢复挂载不再指向旧根。
- 切换批准前保持 Bot、watchdog、LX/SQ 应用和歌词 worker 停止；明确批准后释放后台队列服务，Bot 因未配置入口继续停止。
- 在恢复后的运行容器中，以所选媒体 UID/GID 实际读取私有媒体标记，验证读取权限，而非仅由安装器 root 检查文件存在。

验收从源码工作树调用安装入口，不验证 tag / release URL 可用性，也未通过网络隔离证明全程脱网恢复。

本记录不证明 Apple 登录、订阅或解密、YouTube/哔哩哔哩 Cookie 有效期、LX/SQ 授权内容源、真实音乐/视频下载和最高格式、字幕可用性/双语时间轴/渲染、Telegram/Synology 消息投递、Navidrome/Emby 账号向导及真实扫描播放、Whisper 对齐质量、宿主重启或断电恢复。SABR 安装与服务 health 不构成真实 SABR 下载验证。

生产部署仍需 Linux amd64、可读写 `/dev/kvm`、Docker Engine、Compose 2.20+、Python 3.8+ 和足够的本地数据盘空间。CI 环境的通过不能代替具体群晖机型的 KVM 模块、DSM 权限和存储池验证。新装目录示例见[一命令指南](ONE_COMMAND.zh-CN.md)；恢复后路径可能不同，以该根的 `config/private.env` 与 `deployment.json` 为准。

本次是隔离环境的安装/备份/恢复验收，没有从用户当前 NAS 导出完整私有生产数据。最近一次 NAS SSH 尝试超时，未回退其他地址或网络；同日较早完成的[IPv6 只读核对](LIVE_CHECK_20261021.md)仍是独立的历史证据，不能扩展成生产迁移成功记录。

验收报告经过脱敏，只有该报告允许作为公开 artifact。私有配置、profile、Cookie、VM 磁盘、模型、数据库、媒体、完整日志和备份均不可随报告发布。仅公开报告也不能替代每个部署者自己的授权账号验收。

## English

A complete infrastructure acceptance run finished in an isolated Linux amd64 Docker / KVM environment. It used dedicated fresh roots and synthetic private state/media markers, with no production accounts, no bot polling and no new media download jobs. The fixed run, source commit and [persistent sanitized report](deployment-acceptance-20261021.json) are listed above. All 20 checks are true, `passed=true`, `stage=completed`, and no final failure/cleanup attention is recorded. The retained `last_readiness_failure` describes a transient startup health retry; container-state snapshots may also retain `health=starting`. This is not a claim that every observed container was always healthy. The same source passed 733 isolated tests and privacy checks in [source CI 39558088207](https://github.com/zskruc/apple-music-downloader-bot/actions/runs/39558088207).

The run exercised real service builds/startup and local core/media HTTP endpoints; a real Ubuntu KVM guest with SSH and manager checks; configuration resume changing guest CPUs from 2 to 1 while preserving its disk marker; a private snapshot including media and Docker images; removal of the original owned containers; and restore into a new root. It verified queued task identity/state, LX/SQ credential digests, media/guest markers and relocated mounts. Restored task writers stayed stopped before approval; approval released background queue services while the unconfigured bot stayed stopped. A container process running as the selected media UID/GID read the restored private media marker.

The harness invokes the installer from a source checkout. It does not verify tag/release URL availability or establish a fully network-isolated restore.

These checks do not test Apple login, subscription/decryption, YouTube/Bilibili cookie lifetime, authorized LX/SQ providers, real downloads or highest formats, subtitle availability/timing/rendering, Telegram/Synology delivery, Navidrome/Emby account setup or real library scans/playback, Whisper alignment quality, host reboot or power-loss recovery. SABR installation and container health do not establish a real SABR download.

Deployment still requires Linux amd64, writable `/dev/kvm`, Docker Engine, Compose 2.20+, Python 3.8+ and sufficient local data-disk space. Passing on CI does not validate every Synology model, DSM KVM module, permission scheme or storage pool. Follow the [one-command guide](ONE_COMMAND.en.md). Restored paths may differ from fresh defaults; the restored root's `config/private.env` and `deployment.json` are authoritative.

This was an isolated install/backup/restore test, not an export of the user's current NAS production data. The latest NAS SSH attempt timed out without an alternate-address/network fallback. The earlier same-day [IPv6 read-only comparison](LIVE_CHECK_20261021.md) remains separate historical evidence, not proof of a completed production migration.

Only the sanitized report may be published. Private configuration, profiles, cookies, VM disks, models, databases, media, full logs and backups must remain private. Each deployment still needs authorized account and business acceptance.
