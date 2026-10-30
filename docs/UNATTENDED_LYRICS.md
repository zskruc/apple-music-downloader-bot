# 逐词歌词运行与验收 / Unattended lyrics operations

## 中文

先按[运维指南](OPERATIONS.zh-CN.md)准备模型、持久 state 和媒体挂载，配置私有
`NAVIDROME_URL`、`NAVIDROME_USER`、`NAVIDROME_PASSWORD`，确认播放器支持
enhanced `getLyricsBySongId`。媒体检查和播放器回读默认启用；手动模板的自动
歌词入口仍默认关闭。先验收 `lyrics` profile，再启用 automation/completion。

把[未配置健康模板](lyrics-service-health.example.json)复制到私有 state 的
`word-lyrics/automation/service-health-config.json`，目录 0700、文件 0600。
模板空值不发起未配置的来源请求，也不声称业务健康。部署者自行填写：

- `source`：稳定官方酷狗记录的标题、艺人、毫秒时长和歌词编号。
- `additional_sources`：AMLL 固定搜索词、网易稳定歌词记录编号。AMLL 有效空
  结果只证明 API 可用；网易探针实际解析逐词内容。
- `player`：成功发布记录的播放器 ID、容器内媒体/旁档路径、旁档 SHA-256 和
  当前任务的 `source_fingerprint`，不能借用别的录音指纹。
- `rollout_started_at`：本次部署开始的 Unix 秒时间；`timezone`：报告时区。
  保留 `null` 时不计算新部署完成率。不要反复重置观察起点。

仓库不预置真实歌曲、个人路径或凭据。可用 `WORD_LYRICS_HEALTH_CONFIG` 改变
私有配置位置。维护命令须沿用实际部署的 env、Compose 叠加及 profile；以下是
手动模板示例：

```sh
docker compose --profile lyrics exec -T word-lyrics-resolver python -m word_lyrics_automation.service_health status
docker compose --profile lyrics exec -T word-lyrics-resolver python -m word_lyrics_automation.service_health status --liveness
docker compose --profile lyrics exec -T word-lyrics-resolver python -m word_lyrics_automation.service_health status --probe
```

第一条按业务状态返回退出码，第二条只按工作进程/队列存活返回退出码，第三条
执行到期探针。普通状态命令不发新探针。业务状态分别为 `ok`、`degraded`、
`needs_attention`、`unhealthy`；新鲜心跳不能代替来源、进展和播放器检查。
探针默认单次硬上限 20 秒、常规间隔 15 分钟，缓存和退避跨重启保存。

临时 DNS、连接、TLS EOF、429/5xx 有限退避；解析、账号/权限、响应语义、身份
及媒体故障独立分类。默认临时来源最多八次，无可信来源有有限初始/慢复查，
生命周期有 72 小时上限。任务及历史保留；新鲜的同依赖“失败→成功”探针证明
可恢复耗尽的临时任务。旧成功、心跳或无关来源不能重开任务，坏媒体和身份问题
不放宽校验。Compose 的 `worker_watchdog` 只恢复经重复确认、队列正常且进程
身份/启动代次匹配的失活 worker，并有冷却；业务降级不触发重启风暴。

真正修正输入后，可用有审计的显式重验入口，仍执行全部门禁：

```sh
docker compose --profile lyrics exec -T word-lyrics-resolver python -m word_lyrics_automation.queue_cli resume-attention --job-id 1 --reviewed --reason "reviewed replacement input"
```

编号须替换为实际任务。不要改库、放宽阈值或更换 revision 来清空错误证据。
完整解码绑定文件指纹；FLAC 另核对声明总样本及可用的 PCM MD5，能拦截
“ffmpeg 成功但样本数不足”。其他格式的干净解码不证明上游完整性。检查不改写
媒体。扫描接受后还须实际读回录音身份、大小/格式、正文、逐词毫秒、UTF-8
位置和声部；旧播放器视图重试读回，保留旁档，不重跑已完成的声学模型。

透明代理环境必须同时核对业务和 DNS 的实际 `DIRECT` 流量，空代理变量不足
以证明直连。权威策略仅存于 `ops/lyrics_network_policy.py`，镜像复制到
`/app/lyrics_network_policy.py`。通用 CLI 默认只读：

```sh
python ops/lyrics_network_policy.py --config /srv/mihomo/config.yaml --mode check
```

明确需要恢复时用 `--mode ensure`，并配置 `--mihomo-bin`、`--controller`、
`--runtime-config-path`、私有 `--state-dir`。它只恢复已声明约束，验证候选、
保留回滚、重载及清 DNS 缓存；不擅自删除不属于它管理的冲突配置。
可选 `lyrics_network_policy_runner.py` 固定要求 `mihomo` 容器、`/policy`
挂载、9090、`/root/.config/mihomo` 和 Docker socket，须逐项核对。
标准 Compose 不启用此适配器、不授予 socket、不新增主机 timer；socket 有
主机管理权限。优先使用可配置的通用 CLI，并沿用已有维护调度。

`service-telemetry.json` 每分钟最多刷新统计，保存有界日摘要/状态变化。
分母仅含部署后新建且未被替代的任务；零新任务时比例为 `null`。普通完成率、
无已记录人工事件的完成率、人工介入及历史失败分别报告，避免用历史成功充当
新部署自动率。维护应使用有审计入口，台账不能识别未记录的外部人工操作。

CI 安装 ffmpeg/ffprobe，运行真实解码及本地 HTTP 生命周期测试。发布仍须完成
镜像和配置检查，以及私有环境的账号、直连、播放器、订阅更新、重启/重建和
持续新任务验收。代码发布当天不能证明已经稳定运行数周；不承诺零失败或逐词
边界百分之百准确。早期验收记录保留原有范围，不能累加为全链路保证。

## English

Provision models and persistent state/media using the [operations guide](OPERATIONS.en.md).
Configure private Navidrome credentials and enhanced lyric support before enabling
the lyrics profile and automatic entries. Integrity and player read-back guards
default to enabled. Copy the [unconfigured template](lyrics-service-health.example.json)
into private state and select your own stable source/player records, exact paths,
digests and media fingerprint. Empty fields initiate no unconfigured source requests.
Set the real rollout Unix time and reporting timezone once; no observation window
means no completion-rate claim. No production records or credentials are shipped.

The commands above show cached business status, cached liveness and due probes.
A heartbeat does not prove source availability, job progress or current word cues.
Canaries run in bounded subprocesses with durable cache/backoff. Structured faults
separate transport, response, account/permission, identity and media problems.
Retries are finite. Fresh, provider-specific persisted failure-to-success evidence
can resume eligible exhausted transient work; stale or unrelated success cannot.
Invalid media or uncertain identity retain their gates. Explicit reviewed retries
use `queue_cli resume-attention --job-id ID --reviewed --reason REASON` with audit.

Full decoding is fingerprint-bound. FLAC adds declared-sample and available PCM
checksum checks; a clean decode of other formats establishes decodability, not
complete upstream media. Media is preserved. Completion requires actual player
read-back of recording identity, size/format, text, millisecond cues, UTF-8 offsets
and vocal groups. Stale views retry reads without repeating acoustic generation.
The Compose watchdog uses repeated stale-heartbeat, queue and process-generation
checks plus cooldown. Dependency outages and held inputs do not restart workers.

With transparent routing, verify music and DNS `DIRECT` behavior together after
updates/rebuilds. Prefer the portable policy CLI above: `check` is read-only;
explicit `ensure` accepts local validator/controller/runtime paths and private
rollback state. The optional runner has a fixed `mihomo`, `/policy`, port 9090,
`/root/.config/mihomo` and Docker socket contract. Standard Compose does not enable
it, grant a socket or install another timer. Socket access grants host administration.

Telemetry counts only new non-superseded jobs since rollout, with null rates when
there are none. It separates completion, completion without recorded manual events,
manual intervention, recovery and historical failures. Use audited maintenance
entries. CI includes decoder/local-HTTP failure lifecycles; private account,
routing, player, restart/rebuild/update and sustained real-job checks remain
necessary. Shipping code does not prove weeks of unattended operation, zero
failures or ground-truth per-word accuracy.
