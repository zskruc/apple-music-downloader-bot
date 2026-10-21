# 一命令部署、私有备份与迁移

[English](ONE_COMMAND.en.md) · [首页](../README.md)

本指南对应 v0.2.0 的 `install.py`、`deploy/installer.py`、`deploy/manage.py` 和 `deploy/migration.py`。它管理一个专用目录和登记的 Compose 项目。**真实 Docker / KVM 安装、完整镜像归档恢复及账号下载验收尚未运行。** 合成测试和只读线上核对只能验证各自范围，不能作为部署成功的承诺。

## 1. 主机与安装范围

需要 Linux amd64 / x86-64、Python 3.8+、Git、Docker Engine、Compose 插件 2.20+；在线引导示例另需 curl。安装账号须能访问 Docker daemon，并读写真实的 `/dev/kvm`；BIOS 虚拟化与 Synology KVM 模块由部署者事先准备。安装器不会安装宿主软件或修改 DSM 内核。

默认预检要求安装盘至少 50 GiB 空闲、宿主至少 6 GiB 可用内存，VM 默认为 4 GiB 内存、2 CPU、32 GiB 虚拟磁盘。媒体、Docker 构建缓存、镜像归档和第二份完整备份另计空间。两个歌词模型约 1.65 GB。选用 `/srv/apple-music-downloader` 这类专用本地目录；不要选现有源码仓库、共享目录根、Documents、Desktop、iCloud 或符号链接。

完整安装准备以下服务；不传 `--with-media-servers` 时只省略最后一行：

| 范围 | 服务与状态 |
| --- | --- |
| Apple Music | `legacy-wrapper` KVM VM、`wrapper` 协议适配器；订阅、上游初始化与登录另行完成 |
| 聊天与队列 | `bot`、持久任务状态、统一 Apple → LX → SQ 入口；满足启动条件后才开启轮询 |
| LX / SQ | `lxserver`、`sqmusic-server`、`sq-mysql`、`sq-neteasecloudmusicapi`，隔离暂存与生成的本地凭据 |
| YouTube / 哔哩哔哩 | Bot 内下载器、`youtube-pot`、两个按需启动的认证浏览器；固定校验值的 SABR 工具 |
| 歌词 | `word-lyrics-resolver`、`word-lyrics-nas-aligner`、固定 whisper.cpp 与两个经校验的 GGML 模型 |
| 恢复 | `wrapper-watchdog`、`bot-watchdog`；仅 Bot 满足启动条件时一起激活，不安装宿主 timer / cron |
| 可选播放器 | `navidrome`、`emby`、`lrcapi`，独立数据目录和只读公共媒体挂载 |

完整套件会启用统一音乐、LX/SQ、歌词 automation/completion、自动认证和 SABR 构建/运行配置。手动 Compose 模板仍保留可选功能默认关闭的方式。自动认证及 watchdog 的 Docker socket 具有宿主级管理能力；安装器登记项目归属和服务范围，但 socket 本身不是权限隔离边界。

## 2. 一命令安装

v0.2.0 tag 和 release 发布后，在目标 Linux 主机运行：

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/zskruc/apple-music-downloader-bot/v0.2.0/install.py | sudo python3 - --root /srv/apple-music-downloader --with-media-servers'
```

引导器取固定版本的 `source.tar.gz` 和 SHA-256 清单，校验后放入 `source/`。`pipefail` 会保留 curl 下载失败的退出状态。示例通过 sudo 安装，后续维护同样使用 sudo，以便读取权限 0700 的安装根与私有配置；如果用具备 Docker/KVM 权限的专用非 root 账号安装，应始终沿用那个账号。网络及依赖准备失败会记录阶段，可用后文 `resume` 重试。没有填写聊天凭据时，它可以准备基础设施，Bot 保持停止。使用前可先下载并检查固定 tag 的 `install.py`；已经有源码时也可从源码目录运行 `sudo python3 install.py install --root /srv/apple-music-downloader --with-media-servers`。

安装会在用户机器上获取固定镜像、SQ 指定提交、模型、Ubuntu 基础盘和构建依赖。不会上传本地 SQ 源码或构建镜像。来源、摘要和各自许可见 [SOURCES](../deploy/SOURCES.md)；SQ 上游缺少可确认的许可证，自动化不授予重新分发权。

### 私有配置与第一次启用 Bot

把[示例](../deploy/private-config.example.json)复制到源码外的私有位置，如 `/srv/amdl-private/private-config.json`，目录权限设为 0700、文件设为 0600，用编辑器填写。文件格式为 `{"env": {"大写配置名": "单行值"}}`，也接受布尔值和整数。示例的 `START_BOT=false` 用于先准备服务。不要把真实配置提交 GitHub、放进 source release 或作为命令行参数内容。

```sh
sudo python3 /srv/apple-music-downloader/source/install.py resume --root /srv/apple-music-downloader --config /srv/amdl-private/private-config.json
```

Telegram 需要自己的 `TELEGRAM_BOT_TOKEN` 和非空 `TELEGRAM_ALLOWED_CHAT_IDS`。Synology Chat 需要 `SYNOLOGY_CHAT_ENABLED=true`、outgoing token、允许用户列表和 incoming reply webhook。任何已启用但不安全的入口都会阻止 Bot 激活。Apple Music 另需有效订阅和 [wrapper / VM 初始化](../vm/README.md)；登录、密码、2FA 和设备授权由你完成。

**同一个 Telegram token 只能有一个轮询器。** 安装器不检查其他项目是否正在使用相同 token。新安装填写有效凭据后默认允许激活；已有服务迁移时务必保持 `START_BOT=false`，停止旧轮询器后才改成 true 并运行 `resume`。推荐用独立测试 Bot 验收。安装器拒绝直接覆盖已经写入的私有凭据；不要反复应用仍含空值的示例文件来更换现有账号。

代理使用自己的外部 URL，按实际需要填写 `TELEGRAM_PROXY_URL`、`AMDL_DOWNLOAD_PROXY_URL`、`YT_DOWNLOAD_PROXY_URL`、`YT_LARGE_DOWNLOAD_PROXY_URL`、`BILI_DOWNLOAD_PROXY_URL`。浏览器代理另由对应 browser args 配置。宿主下载依赖与 Docker 拉取镜像使用宿主/daemon 网络配置，Bot 的代理变量不会替代它们。仓库不捆绑代理订阅、Cloudflare 账号或私人出口服务。

## 3. 文件布局与持久化

| 安装根下的位置 | 内容 |
| --- | --- |
| `source/` | 公开源码；不存放真实 `.env`、Cookie、VM 盘或媒体 |
| `deployment.json` | 私有部署描述：项目名、归属、Compose 文件、服务及挂载范围 |
| `config/private.env` | 生成的私有环境配置，权限 0600；后续命令沿用 |
| `config/installer.compose.json` | 安装归属标签和持久挂载；`config/images.compose.json` 保存镜像恢复/升级覆盖 |
| `data/state/` | Bot 数据库、Cookie、歌词队列；模型在 `word-lyrics/models/` |
| `data/vm/`、`private/wrapper-session/` | 持久 VM 磁盘与 wrapper 会话 |
| `private/youtube-browser/`、`private/bilibili-browser/` | 持久 Chromium profile，包含登录私密数据 |
| `data/lx-music/`、`data/sq-mysql/`、`private/sq/` | LX 数据、SQ 数据库与鉴权；SQ 暂存在 `data/sq-staging/` |
| `media/` | `music`、`youtube-music`、`youtube-video`、`bilibili-music`、`bilibili-video` |
| `data/navidrome/`、`data/emby/`、`data/lrcapi/` | 可选播放器数据库和配置 |
| `state/`、`cache/` | 安装检查点、维护状态、私有日志及可重试依赖缓存 |

安装器从 `--root` 派生托管路径，不接受将单个托管数据路径偷偷指回生产目录。个人既有媒体库不会自动导入；需要额外只读挂载时，参考[媒体配置示例](../services/media/personal-libraries.compose.example.yaml)并把叠加文件登记到部署描述和备份范围，先验证覆盖清单。不要将整个开发或数据目录同步到 iCloud。

YouTube Cookie 位于宿主 `data/state/youtube/cookies.txt`（容器 `/state/youtube/cookies.txt`），上一份为 `cookies.txt.previous`；浏览器 profile 独立保存在 `private/youtube-browser/`。登录时按 [YouTube 指南](YOUTUBE.zh-CN.md)启动浏览器，通过回环端口/SSH 隧道手动完成登录，再由导出工具停浏览器、验证候选、原子替换并保留备份。失败不会发布未经验证的候选；运行下载时使用私有 Cookie 副本。profile 与 Cookie 只在各自持久目录保留，重建容器不会代替重新验证账号。

浏览器默认不常驻。完整套件已登记认证与自愈配置，安装后的 YouTube helper 会自动读取部署描述。`start` 可单独开浏览器；`status`、`export`、`rollback` 通过现有 Bot 执行，需要 Bot 已运行。首次尚处于 pending 时，先完成 Apple / 聊天入口配置与激活，再执行导出：

```sh
sudo bash /srv/apple-music-downloader/source/services/youtube/youtube-auth.sh start
# 完成手动登录且 Bot 已运行后，用有权访问的公开测试 URL 运行 export URL。
sudo bash /srv/apple-music-downloader/source/services/youtube/youtube-auth.sh status
```

## 4. 待配置状态与播放器初始化

```sh
sudo python3 /srv/apple-music-downloader/source/install.py status --root /srv/apple-music-downloader
sudo python3 /srv/apple-music-downloader/source/install.py doctor --root /srv/apple-music-downloader
sudo python3 /srv/apple-music-downloader/source/install.py resume --root /srv/apple-music-downloader --config /srv/amdl-private/private-config.json
```

`resume` 复用已完成阶段，重新评估配置、激活与服务检查，不重置数据库和账号。`status` 的 `installed` 表示安装阶段完成，`configured` 表示 Bot 激活条件满足，`infrastructure_ready` 仅反映登记服务的运行/health 状态。`ready` 保持 false，`readiness_scope` 明确真实下载另验；`pending` 可能保留按需 YouTube/哔哩哔哩登录和 LX 授权源的通用提醒，不能拿它自动推断登录已失效。

可选播放器端口互不重叠，均只绑定 `127.0.0.1`，远程使用 SSH 隧道：

| 服务 | 默认宿主地址 | 需要手动完成 |
| --- | --- | --- |
| Navidrome | `http://127.0.0.1:14533` | 创建首个管理员、扫描并验证音乐；媒体根 `/music` |
| Emby | `http://127.0.0.1:18096` | 初始化向导、用户、媒体库与扫描；音频 `/music`，视频 `/video/YouTube`、`/video/Bilibili` |
| LrcAPI | `http://127.0.0.1:28883` | 按需配置歌词源；本地 `/src` 可检查静态页面，不代表外部歌词源可用 |

安装器不伪造 Navidrome/Emby 账号和 API key。需要 Bot 发起扫描时，将自己创建的 `NAVIDROME_USER` / `NAVIDROME_PASSWORD` 或 `EMBY_USER` / `EMBY_PASSWORD` 写入私有配置，再 `resume`。未指定 URL 时，选装服务会接入内部服务地址；已有非空 `NAVIDROME_URL`、`EMBY_URL`、`YT_LRCAPI_URL` 会保留。要禁用某个接入，在每次使用的私有 JSON 中明确保留该 URL 的空值；不要仅凭一次写入 env 空值推断后续不会设内部地址。详情见[媒体服务配置与来源](../services/media/SOURCES.md)。

日常使用安装内的运维入口，它自动读取 `deployment.json`、私有 env、已登记 profiles 和镜像覆盖，无须重新拼 Compose 参数：

```sh
sudo bash /srv/apple-music-downloader/source/ops/stack.sh status
sudo bash /srv/apple-music-downloader/source/ops/stack.sh probe
```

不要直接在 `source/` 新建 `.env` 再启动第二套 Compose。单服务 `upgrade` 仍要求先构建/拉取独立新 tag，再用安装内运维入口；managed 覆盖保存在安装根 `config/images.compose.json`。普通手动模板的 `STATE_PATH/ops/images.compose.json` 是另一种布局。

## 5. 完整私有备份

先暂停聊天、LX/SQ 手动入口和外部提交。运行中的 Bot 任务、stopping 任务或歌词 leased 工作会阻止备份；尚未执行的 queued 任务允许保留。备份短暂停止登记项目的服务并检查数据静止，顺序包括先停 watchdog、Bot/worker，再停 SQ/MySQL 和 wrapper/VM。成功或失败都会尝试只恢复原本运行的服务，原本停止的浏览器或 Bot 保持停止。

```sh
sudo python3 /srv/apple-music-downloader/source/install.py backup --root /srv/apple-music-downloader --destination /srv/amdl-backups/backup-20261021
```

**默认包含媒体和 Docker 镜像归档。** 还包括私有配置、持久状态、浏览器 profile、Cookie、wrapper 会话、VM、LX/SQ 数据及播放器配置；文件摘要与挂载覆盖清单用于恢复核对。目标必须是源目录外一个不存在的新目录。备份不会停止无关项目，也不自动接管独立 LX/SQ 项目；旧多项目迁移必须先明确登记项目与所有持久挂载，参见 `deploy.migration inventory` / `snapshot` 的帮助。

确实要省略时才加 `--without-media` 或 `--without-images`。省略会写入覆盖记录；媒体须另行复制，镜像须重新拉取/构建。迁移最终快照另用 `--leave-stopped`：成功后保留原服务停止，失败仍尝试恢复原本运行的服务。不要把它与旧手动流程的 `ops/stack.sh backup` 混为一谈：后者是升级用私有快照，默认不含媒体，规则也不同。

备份未加密，含完整凭据、登录态和媒体，禁止作为 release、issue 或 CI artifact 上传。日志也可能含私密内容。备份后检查 `status` 与 `state/maintenance.json`；恢复服务失败需处理后再接收新任务。队列字节会保留，原服务恢复后也可能继续执行 queued 任务，因此“备份完成”不等于旧 Bot 已停止。

## 6. 恢复与明确切换

恢复使用新的专用根，不覆盖原安装。默认端口与旧实例相同，旧库也不能在最终快照后继续消耗队列。先暂停所有外部提交及会拉起旧服务的主机守护，再做带 `--leave-stopped` 的最终快照，验证后恢复；不要拿已恢复运行的日常备份点直接当最终切换点。下例使用已有公开源码入口，不需要把私有备份复制进源码：

```sh
sudo python3 /srv/apple-music-downloader/source/install.py backup --root /srv/apple-music-downloader --destination /srv/amdl-backups/cutover-20261021 --leave-stopped
sudo env PYTHONPATH=/srv/apple-music-downloader/source python3 -m deploy.migration verify /srv/amdl-backups/cutover-20261021
sudo python3 /srv/apple-music-downloader/source/install.py restore --root /srv/apple-music-downloader-restored --bundle /srv/amdl-backups/cutover-20261021 --config /srv/amdl-private/private-config.json --with-media-servers
```

确认最终备份成功并用 `status` 检查旧栈停止；失败时它可能已经自动恢复，先处理故障再切换。恢复若需保留选装播放器，显式传 `--with-media-servers`；仅有其归档数据不会自动选装服务。恢复先校验内容与覆盖记录，把数据放到新根并重新映射挂载；包含镜像时会 `docker load`、核对 image ID，并沿用归档服务镜像，避免重新构建/拉取这些服务。未包含的依赖仍需网络。恢复原始任务数据库，保留 queued 状态，不全量清队列；Bot 真正启动后由已有恢复逻辑处理可续任务，失败/中断任务仍需按状态检查。

**备份中的 `START_BOT=true` 不算切换批准。** 上例私有配置应保持 `START_BOT=false`。恢复尚未批准时，Bot、两个 watchdog、LX/SQ 应用和两个歌词 worker 都创建但保持停止；数据库、网易 API、wrapper、PO provider 和播放器可以运行以供检查。若当前 restore 调用的 JSON 已明确传入 `START_BOT=true`，就会当作批准，因此不要提前复用允许启动的生产配置。人工完成新根的配置、账号和挂载检查后，另外创建私有 `/srv/amdl-private/cutover.json`，内容为 `{"env":{"START_BOT":true}}`，文件权限 0600，再明确批准切换：

```sh
sudo python3 /srv/apple-music-downloader-restored/source/install.py resume --root /srv/apple-music-downloader-restored --config /srv/amdl-private/cutover.json
```

恢复后的切换必须在这次调用的私有配置中显式传 true；只修改恢复出的 env 不会解除保护。批准会释放后台任务写入者，Bot 另须满足聊天 allowlist 和 wrapper health 条件。旧实例保持停止，使用新实例验证唯一轮询、持久任务、真实下载和播放；需要撤回时先停新实例，再人工评估旧状态与迁移后新增媒体，不能同时运行两份。若要并行检查新旧基础设施，必须先在私有配置中为所有发布端口分配不同值，仍不得同时运行相同账号的任务入口。

### 离线源归档与镜像恢复

在联网机器从固定 v0.2.0 tag 保存 `install.py`，从同版本 release 保存 `source.tar.gz`、`source.tar.gz.sha256`、`release.json`，核对来源与摘要，再安全转移到目标主机。`install.py` 不是单独的 release asset。已知预期 SHA-256 后可离线安装源码并恢复私有备份：

```sh
sudo python3 /mnt/amdl-release/install.py restore --root /srv/apple-music-downloader-restored --archive /mnt/amdl-release/source.tar.gz --sha256 EXPECTED_64_HEX_SHA256 --bundle /srv/amdl-backups/cutover-20261021 --config /srv/amdl-private/private-config.json --with-media-servers
```

`EXPECTED_64_HEX_SHA256` 必须替换为核对过的真实摘要。离线 source archive 只解决公开源码分发；新装的 apt/PyPI/Go/Git、SABR、模型、Ubuntu 与容器镜像仍可能需要联网。完整初始化后的私有快照加默认镜像归档可恢复保存的 VM、模型和服务镜像；缺项或尚未完成的初始化不能靠源码归档补齐。勿把私有备份混入公开 `source.tar.gz`。

## 7. 验证方式与范围

从源码运行 `python3 scripts/check_repo.py` 与 `python3 scripts/run_tests.py` 验证文档/安全约束和合成用例。`install --dry-run` 只列阶段；`doctor --dry-run` 只列检查计划，都不证明 Docker/KVM 工作。真正的隔离基础设施验收应在空闲的 Linux amd64 KVM 主机用独立目录运行：

```sh
sudo python3 -m deploy.acceptance --root /srv/amdl-acceptance --report /srv/amdl-acceptance-report.json --include-images
```

该测试会真实构建/启动和备份/恢复专用测试服务，耗时、耗磁盘；不使用生产凭据，不发起新媒体任务。只可公开经过脱敏的 report，不能上传其私有目录、日志或备份。当前仓库已提供测试入口，**本轮尚未执行真实验收，不能声称已通过**。最后仍需按[运维验收清单](OPERATIONS.zh-CN.md#3-验收清单)使用自己的授权账号验证下载、字幕、扫描、播放和唯一轮询。
