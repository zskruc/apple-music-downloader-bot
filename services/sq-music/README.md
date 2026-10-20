# SQ Music Plus 服务与运维

[English](README.en.md)

这是 Bot 的可选 SQ 后端，配合现有的 `bot/sq_music.py`、统一搜索和单曲无损回退使用。默认链路是 Apple → LX → SQ；明确缺曲或没有可用无损结果时才继续换源，Apple 登录和网络故障保留原有错误与重试。SQ 先写独立暂存，Bot 校验录音身份、任务归属、实际路径、FLAC 格式和完整解码后才发布到正式库的 `SQ Music` 子目录。

此目录提供原创精准补丁、固定上游版本引用、Compose 叠加配置、初始化工具和运维说明。**没有复制上游 Java 源码或镜像**。在审计的上游提交中没有找到许可证；本仓库的 MIT 许可只覆盖本项目原创内容，不授予 SQ 或其他上游的再分发权。构建前自行核对使用与许可条件。[版本与来源](SOURCES.md)。

## 已验证基线与边界

- 上游：[`59799517/simple_sq_music_plus`](https://github.com/59799517/simple_sq_music_plus)，后端 `3.1.33`，源码提交 `cf0f6ed136be0aa13845bbab028c27e404b89d5d`。
- 官方基础镜像：`ghcr.io/59799517/simple_sq_music_plus@sha256:4f3e499c07da526bea5648fc4e1b9c449e363356c5e8c1ef4d96c2d67b1e20f2`。
- 数据库保留当时的 MySQL `5.7` 基线，部署者必须固定已审计镜像摘要；本模板不提供主版本升级。
- NetEase API 保留上游的 `v4.32.0` 基线，示例提供当时校验的摘要。
- 2026-10-17 的本地留存记录验证过 SQ 网易单曲搜索、排队、完成轮询、FLAC 完整解码、隔离发布与重复复用。2026-10-20 留存巡检显示这套 SQ 服务仍在运行。此次公开准备未连接 NAS，也未重新执行真实下载或全新部署。
- Bot 默认只启用 `netease`。SQ 自带的其他来源没有得到相同验收，不应仅为增加候选数而启用。上游原生网易扩展保留在上游实现中，本仓库不提供外部音源脚本、会话、平台账号或媒体。

## 1. 构建本地补丁镜像

以下命令从本仓库根目录执行。开发检出与构建目录应放在本地工作盘；本机约定使用 `LocalWork`。

```sh
git clone --filter=blob:none https://github.com/59799517/simple_sq_music_plus.git services/sq-music/upstream
git -C services/sq-music/upstream checkout cf0f6ed136be0aa13845bbab028c27e404b89d5d
python3 services/sq-music/prepare_build.py services/sq-music/upstream
docker build -t apple-music-downloader-sq:3.1.33-bot services/sq-music
docker image inspect apple-music-downloader-sq:3.1.33-bot --format '{{.Id}}'
```

将得到的本机 image ID 写入根 `.env` 的 `SQ_PATCHED_IMAGE`；跨主机部署时需要自行核对再分发条件，并使用已审计镜像摘要。`upstream/` 与生成的 `patched/` 都被忽略，不要加入公开提交或发布附件。

补丁逐个校验两个原文件的 SHA-256，结构不同即拒绝。它添加 `${taskId}` 目录模板，在标签与歌词处理完成后记录真实 `downloadFile`，并添加普通登录认证保护下的 `GET /api/task/bot/{id}`。接口返回 `bridgeVersion=1`、任务与子任务摘要，只有最终 `success` 才返回实际路径。Bot 不接受“已入队”作为成功，也不接受 `supplement_success`、显示名、其他任务路径、软链接或非 FLAC。Dockerfile 只用固定镜像内已有 JDK 21 与依赖编译覆盖类，不增加运行依赖。升级上游时必须重新审计这些契约。

## 2. 准备配置与私有文件

将本目录 [.env.example](.env.example) 的变量补入**仓库根** `.env`，也可直接使用根示例已提供的 SQ 区块。保持密钥占位符，填写实际的 `SQ_PATCHED_IMAGE`、已审计的 MySQL 5.7 摘要，以及以下绝对路径：

| 变量 | 内容 | 容器用途 |
| --- | --- | --- |
| `SQ_STAGING_PATH` | 独立 SQ 暂存目录 | SQ `/music` 可写；Bot `/sq-staging` 只读 |
| `SQ_MYSQL_DATA_PATH` | 独立空数据库目录 | `sq-mysql:/var/lib/mysql` |
| `SQ_AUTH_PATH` | 私有 JSON 文件 | Bot `/run/secrets/sq-auth.json` 只读 |

暂存、数据库、凭据目录彼此分开，且暂存不应放入正式 `MUSIC_PATH`。同一份暂存必须同时映射给 SQ 与 Bot，不能把 Bot 的正式 `/music` 当作 SQ 的暂存。Bot 最终写入 `/music/SQ Music`，仍由主 Compose 的 `MUSIC_PATH` 提供。

```sh
python3 services/sq-music/bootstrap.py prepare
bash ops/stack.sh --profile sq check
```

`prepare` 创建所需目录、独立随机数据库密码/根密码/JWT 密钥和 `0600` 的 auth JSON，保留已有密钥；发现已有数据库却缺少匹配密钥时拒绝生成。它只操作文件，不启动服务、不连接 NAS、不输出凭据。auth 文件格式为两个非空字符串：`{"username":"YOUR_SQ_USERNAME","password":"YOUR_PRIVATE_PASSWORD"}`；该文件对应 SQ 的独立登录账号，不是平台 cookies，也不是 JWT。不要提交或打印 auth 文件与根 `.env`。

## 3. 初始化这套新服务

```sh
bash ops/stack.sh --profile sq up sq-mysql sq-neteasecloudmusicapi sqmusic-server
# 等后端完成首次建表，再配置这套新的独立数据库：
python3 services/sq-music/bootstrap.py configure --new-database
bash ops/stack.sh --profile sq stop sqmusic-server
bash ops/stack.sh --profile sq up sqmusic-server
# 先验证后端认证和完成接口，再在根 .env 启用 AMDL_UNIFIED_MUSIC_ENABLED=true，启动/更新 Bot：
bash ops/stack.sh --profile sq up bot
bash ops/stack.sh --profile sq probe
```

`configure` 先核对关键配置行，再执行 `bootstrap-config.sql`，并通过进程标准输入设置独立登录账号。它不打印 SQL 或密码，不停止服务，不替换已有媒体。此操作仅用于新建的独立 SQ 数据库；不要对共享或未知版本数据库执行。后端重启后才会重新读取缓存。

叠加配置和主栈共享默认 Docker 网络，服务地址固定为 `http://sqmusic-server:8099`。**HTTP 地址不可用带下划线的旧容器名**，Tomcat 会拒绝该 Host。后端宿主端口仅绑定 `127.0.0.1:18099`；数据库和 NetEase API 不发布宿主端口；MCP 关闭。默认内存上限约为后端 1100 MB、数据库 600 MB、API 300 MB，不设置部分 DSM 不支持的 CPU CFS 参数。

数据库配置限制下载并发为 1、重试为 0，并关闭上游自动跨源替换、降级、收藏同步和自动下载。默认使用已验收的原生网易扩展。必须用自己有权使用的来源和账号完成验收；来源声明 FLAC 不代表实际媒体已通过验证。

## 验收、备份、升级与恢复

首次上线先在独立暂存验收：匿名访问任务接口被拒绝；独立账号可登录；真实单曲搜索返回明确的 FLAC 枚举；提交后能轮询到最终成功；成功路径首层目录等于任务 ID；Bot 对同一宿主文件完整解码后原子发布；重复发布不重复下载。确认 Apple 网络/认证故障仍保留原错误，取消任务后不再发布，现场、重制、伴奏等录音不混用。`probe` 只提供健康摘要，不能替代这些真实验收。

```sh
bash ops/stack.sh --profile sq backup /srv/private-backups/amdl-sq-YYYYMMDD
bash ops/stack.sh --profile sq stop sqmusic-server sq-neteasecloudmusicapi sq-mysql
```

始终带上部署时相同的 `--profile sq`。先暂停 Bot 与任何外部 SQ 客户端提交，并确认双方队列为空。运维备份包含根 `.env`、SQ Compose、`SQ_AUTH_PATH` 和离线 `SQ_MYSQL_DATA_PATH`，默认不包含下载暂存；如需保留暂存，另行在 SQ 停止时复制到私有备份。不要将备份发布到 GitHub。保留固定基础镜像引用、补丁镜像 ID 和补丁版本。升级 SQ 前先隔离验证源码摘要、认证、最终任务路径与 FLAC 完整解码，不能只改 `latest` 或盲套补丁。MySQL 主版本升级需要独立的备份恢复演练。

关闭新的统一搜索入口时在根 `.env` 设置 `AMDL_UNIFIED_MUSIC_ENABLED=false`，用带 `sq` profile 的运维命令更新 Bot，当前已持久任务仍需完成或明确停止。`sq` profile 会将 Bot 的 SQ 开关设为开启；完全移除集成时，更新 Bot 的命令应去掉 `--profile sq`（保留其他仍需要的 profile），随后单独停 SQ 服务并保留数据库与暂存。回滚保留旧镜像与新数据，不用旧数据库覆盖新任务。取消/超时会停止 Bot 等待与正式发布，上游已开始的下载可能继续写入自己的暂存；清理必须按任务归属和保留期进行，不移动活跃文件。

本目录静态/隔离验证与 Bot 测试不等于新主机部署验收。可先运行 `python3 -m unittest discover -s tests/bot -p test_sq_music.py`；最终仍须在自己的部署主机验证账号、来源、真实完成路径和媒体解码。

纯离线辅助工具检查：`python3 -m unittest discover -s services/sq-music -p 'test_*.py' -v`。测试使用临时目录与模拟数据库调用，覆盖私密权限、凭据保留、旧数据库保护、未知源码/数据库拒绝及 SQL 标准输入传递。
