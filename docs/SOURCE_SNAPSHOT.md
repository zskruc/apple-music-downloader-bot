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

The optional SQ / LX integrations require separately configured external
services. No such service, access token or endpoint is provided. Local macOS
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
  configuration variables and blocks Python socket connections.
- Python syntax, distributable file types, credential patterns and private
  deployment identifiers were reviewed before publication. Automated matching
  supplements manual review; it is not a guarantee that every possible secret
  format can be detected.
- Dependency resolution was checked against the pinned runtime requirements.
- Both Compose templates passed validation against the official Compose
  specification's JSON schema. All shell scripts passed syntax checks. The
  repository CI validates Compose configuration and runs the offline suites.

## Limits

The NAS domain was resolved through the required public IPv6 SSH route, but
both connection attempts returned `No route to host`. No alternative address
or overlay route was used. Consequently, this snapshot has **not** been
compared against the final live NAS source or active configuration.

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
因 NAS 公网 IPv6 不可达，尚未核对线上最终状态；
本机无 Docker，新的通用模板尚未完成容器构建、账号下载、VM 冷启动和恢复验收。
这些限制同样在中英文 README 首屏注明。
