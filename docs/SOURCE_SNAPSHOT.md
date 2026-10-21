# Source snapshot / 源码快照

Prepared on **2026-10-21, Asia/Shanghai**.

## Provenance

This is a curated source snapshot assembled from local maintenance checkouts,
not an export of the NAS filesystem or its Git history. The per-file source
path, original SHA-256 and published SHA-256 are recorded in
[SOURCE_MANIFEST.tsv](SOURCE_MANIFEST.tsv).

| Component | Selected local material |
|---|---|
| Bot orchestration and word-lyric pipeline | r80 snapshot, 2026-10-18 |
| YouTube, authentication, task cards and download history | Production source captured during maintenance on 2026-10-19 |
| Shared library, adapters and support modules | r58 maintenance source, supplemented by later captured modules |
| Unified music routing and optional SQ integration | 2026-10-17 integration snapshot |
| Tests | Existing bot, lyric, lifecycle and integration regression suites |
| Deployment and maintenance templates | Newly adapted for portable paths and an independent checkout |

Unexecuted deployment candidates were excluded. Public source differences
include configurable host names, accounts and storage paths; synthetic
network fixtures; portable test imports; and omission of the old optional
AppleMusicDecrypt runner and vendor tree. Use `AMDL_BACKEND=gamdl`.

The optional SQ / LX integrations include backend deployment overlays,
private initialization helpers and bilingual operations guides in `services/`.
SQ includes an original exact-version transformation recipe; upstream Java
sources and binaries are not republished. No access token, personal source
script or platform session is provided. Local macOS
alignment support is included as source, but its MLX environment is separate
from the Linux container and is not configured by the default deployment.

## Included and excluded material

Included: original Python services, relevant regression tests, Docker and
Compose templates, VM provisioning scripts, operations helpers, bilingual
documentation and third-party notices.

Excluded: `.env`, cookies, browser profiles, private keys, account sessions,
device credentials, media, VM disks, model weights, databases, production logs,
historical backups and development caches. Development dependencies and test
outputs remain local and are ignored by Git.

OpenCC-derived dictionary material retains Apache-2.0 attribution. Independent
upstream services and dependencies keep their own licenses. In particular,
wrapper-manager is AGPL-3.0; see [third-party notices](../THIRD_PARTY_NOTICES.md).

## Verification

- The application regression suites passed **456 tests** under Python 3.12.
- The maintenance helpers passed **12 mocked contract tests**, including
  profile selection, scoped stopping, private backup, upgrade and rollback.
- The test runner isolates state in temporary directories, clears production
  configuration variables and restricts Python socket connections to loopback
  test fixtures, blocking external addresses.
- Python syntax, distributable file types, credential patterns and private
  deployment identifiers were reviewed before publication. Automated matching
  supplements manual review; it is not a guarantee that every possible secret
  format can be detected.
- Dependency resolution was checked against the pinned runtime requirements.
- Both Compose templates passed validation against the official Compose
  specification's JSON schema. All shell scripts passed syntax checks. The
  repository CI validates Compose configuration and runs the offline suites.

## Limits

During initial preparation, the required domain/IPv6 SSH route returned
`No route to host`; no alternative address or overlay route was used.
Connectivity later recovered and a read-only live comparison was completed
on 2026-10-21. See the [live check record](LIVE_CHECK_20261021.md) for verified
source/configuration scope, portable differences and remaining acceptance limits.

The preparation host has no Docker runtime. A clean image build, KVM guest
cold start, account login, actual media downloads, media-server scanning,
model execution, backup/restore and host reboot recovery remain unverified for
these portable templates. CI source checks do not establish those outcomes.

Before production use, perform the acceptance steps in the operations guide
with a separate test bot and private state. Keep the known-good deployment and
its images until the replacement has passed those checks.

## 中文说明

本次开源从本地维护资料白名单整理，没有同步整个 NAS，也没有上传历史目录。
主流程与歌词取 r80；YouTube、认证和任务卡等取 10 月 19 日保存的生产源码；
逐文件来源及脱敏前后哈希见同目录清单。未部署候选没有当成生产基线。

456 项应用回归与 12 项运维模拟测试已通过，Compose schema 与脚本语法检查通过。
首次整理时 NAS IPv6 不可达；后续已完成[只读线上核验](LIVE_CHECK_20261021.md)。
本机无 Docker，新的通用模板尚未完成容器构建、账号下载、VM 冷启动和恢复验收。
这些限制同样在中英文 README 首屏注明。

## LX / SQ supplement, 2026-10-21

The initial publication included the adapters but omitted backend deployment
and complete credential/storage examples. This supplement adds LXServer
2.1.2 deployment, SQ 3.1.33 task-bridge preparation, MySQL/NetEase API
dependencies, private bootstrap helpers, optional Compose profiles and
configuration tests. Unified routing now skips disabled providers entirely.

The service directories record their fixed upstream revisions and local
maintenance evidence. LXServer's Apache-2.0 license is retained. No license
grant was found in the locally audited SQ commit; its upstream source is not
included or relicensed. New public files contain recipes and synthetic tests,
not authenticated runtime data. Live backend builds and downloads remain
unverified during this publication work.

The supplemented repository passed **500 isolated tests** under Python 3.12,
including provider-disable boundaries, shared-volume/network contracts,
credential initialization, exact-patch rejection and maintenance ordering.
The four Compose files passed the official schema check. Python socket access
in the test runner is limited to local loopback fixtures; external addresses
are blocked. SQ database/root/JWT secrets from the shared `.env` are explicitly
removed from bot and lyric-worker environments.

## YouTube supplement, 2026-10-21

This supplement completes the YouTube Music/video deployment configuration,
manual and automatic caption processing documentation, bilingual ASS styling,
persistent Chromium sessions and validated Cookie export/rollback. Chinese and
English guides are in [YOUTUBE.zh-CN.md](YOUTUBE.zh-CN.md) and
[YOUTUBE.en.md](YOUTUBE.en.md); the host authentication entry point is in
`services/youtube/`. The explicit `--auth-refresh` overlay enables Docker access
without selecting watchdog services; automatic refresh remains off by default.

General recovery and cancellation behavior was adapted from local
2026-10-19 YouTube maintenance material with a recorded production-start
receipt. The port retains private per-process Cookie snapshots, bounded
highest-quality retries and cancellable browser refresh. It excludes the
separate missing SABR extraction tool and the video-client-priority candidate
whose record says it was never deployed. New portable changes include open
font defaults, blank proxy/LRCAPI defaults, candidate validation and rollback,
private-session backup coverage, and the Bot's cancellation callback.

The final supplemented repository passed **552 isolated tests** under Python
3.12, including Cookie update/rollback failure boundaries, browser ownership,
cancel propagation, subtitle selection, retry exhaustion and persistent mount
contracts. Shell syntax and source/credential checks passed. CI also validates
the base, authentication, watchdog and optional music Compose combinations.
The manifest's published hashes reflect this final source; original snapshot
paths/hashes remain as provenance for the initial curated files.

本轮已补齐 YouTube Music、视频下载、中文字幕/双语外置字幕和 Cookie 持久化的
中英文文档、配置与认证工具。Cookie 导出先校验后替换，保留上一份回滚；
下载使用私有副本，避免旧任务覆盖新会话。552 项隔离测试通过。
未上传真实 Cookie、浏览器 profile、私有配置或媒体。后续直连核验见
[线上记录](LIVE_CHECK_20261021.md)；新的真实登录、下载、播放、容器构建及
备份恢复仍须在部署主机验收。

## Live verification and SABR completion, 2026-10-21

Direct domain/IPv6 access recovered. The read-only check compared all 57
published Python source files against the running bot, reviewed differences,
confirmed persistent state/browser mounts and inspected existing video/subtitle
files. It did not change the NAS or exercise new authenticated downloads.
The [live check record](LIVE_CHECK_20261021.md) distinguishes these observations
from playback, Cookie validity, image-build and recovery acceptance.

The check identified one omitted deployed behavior: unfinished online-media
tasks retain their original IDs for service restart while explicit user stops
remain stopped. This is now included with regression coverage.

The deployed SABR tool was also present and its bytes matched the public
upstream release asset. Its fixed source revision, digest, download recipe and
unmodified third-party notices are now recorded in
[SABR_SOURCES.md](../services/youtube/SABR_SOURCES.md). The original adapter and
private-Cookie runner support that tool as a separate opt-in. Default builds do
not download it, and default runtime settings do not select it. The vendor
executable itself is not committed. No unexecuted video-priority candidate or
production-specific URL rule was added.

The completed repository passed **573 isolated tests** under Python 3.12.
New coverage includes restart preservation, SABR selection and integrity
rejection, Cookie isolation under the optional engine, safe candidate-probe
failure and atomic installer failure boundaries. CI additionally checks
required image-source inclusion with an offline scratch build. The public
Cookie recovery enhancements and optional installer were not deployed to NAS
as part of this check.


## One-command deployment and restore acceptance, 2026-10-21

This is an appended record. Earlier sections retain the test counts and limits
of their publication stages; they are not rewritten as claims about this run.

The v0.2.0 full-suite installer adds staged resume, dedicated public source and
private runtime directories, pinned dependencies, optional Navidrome/Emby/LrcAPI,
complete private media/image backups, explicit service mapping and restore
cutover protection. The runtime baseline at `1d4df6e` passed **733 isolated
tests** and the distributable-source privacy check; this number is separate
from real-container acceptance and must not be presented as account testing.

The complete `--include-images` infrastructure run
[39558116855](https://github.com/zskruc/apple-music-downloader-bot/actions/runs/39558116855)
at [1d4df6e718851e811225563f9f5134eee46f2f5f](https://github.com/zskruc/apple-music-downloader-bot/commit/1d4df6e718851e811225563f9f5134eee46f2f5f)
passed with a real Linux amd64 KVM guest. It covered fresh builds, guest/manager
readiness, VM CPU configuration resume with disk preservation, an offline
private snapshot including images/media, removal of the original containers,
new-root restore, queued task preservation, held/released task writers and
reading restored private media as the selected runtime UID/GID. See the
[bilingual evidence and boundaries](DEPLOYMENT_ACCEPTANCE_20261021.md).

No production login, content download, subtitle-quality or playback acceptance
is inferred. The latest direct NAS SSH attempt timed out; this run did not
export or migrate the current NAS. The earlier same-day read-only live check
remains valid within its recorded scope. Restored paths may differ from fresh
examples; generated private environment and deployment descriptors are the
source of truth. Source archives remain public-code-only, while complete
migration bundles contain credentials and must remain private.

v0.2.0 的真实隔离基础设施验收已通过上述固定运行：安装、VM 配置续装、含镜像
和媒体的私有快照、新根恢复、队列切换保护及实际运行 UID/GID 读取均在范围内。
账号登录、真实下载、字幕质量与播放未测试；当前 NAS 的完整生产导出与迁移也
没有完成。原有来源、历史测试数量和当时的验收限制保留不变，具体证据以新验收
记录为准，恢复目录以生成的私有 env 与部署描述为准。
