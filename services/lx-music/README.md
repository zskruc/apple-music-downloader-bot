# LX Music / LXServer

[English](README.en.md)

本目录提供 LXServer 后端的可选部署、私有配置初始化、只读验收以及升级/回滚说明。Bot 的 LX 搜索、持久化下载队列、源和音质校验、按歌手/专辑归档代码在 [`bot/lx_fallback.py`](../../bot/lx_fallback.py)，相关用户流程在 [`bot/ingest_bot.py`](../../bot/ingest_bot.py)。

## 来源与版本

- 上游：[XCQ0607/lxserver](https://github.com/XCQ0607/lxserver)。
- 固定源码版本：[v2.1.2，提交 `9006e61d73f15764b8eb1388efbd55d8d3271370`](https://github.com/XCQ0607/lxserver/tree/9006e61d73f15764b8eb1388efbd55d8d3271370)。
- 默认官方镜像：`ghcr.io/xcq0607/lxserver@sha256:aa3db966049584585639ca6563e85ce5b7ebf33569dd1f5064c21bd7330382af`。
- 上游许可：Apache-2.0；保留[许可证副本](LICENSE.upstream.txt)。本目录原创辅助脚本及 Bot 集成沿用本仓库 MIT 许可证。

镜像摘要和版本来自 2026-10-19 保留的部署验收记录，上游标签和配置格式于 2026-10-21 核对。没有重新连接 NAS 或完成本模板的全新部署验收。本次不分发 LXServer 源码或镜像层；已留存的升级材料没有额外的 LXServer 源码补丁，我们的定制位于 Bot 客户端、路径映射及运维流程。[机器可读来源记录](upstream.json)。

## 新安装

在项目根目录执行。先将根 `.env` 的 `LX_DATA_PATH`、`LX_FRONTEND_AUTH_PATH` 和 `MUSIC_PATH` 配置为宿主本地可写目录。下面使用 `.env.example` 的 `/srv/apple-music-downloader` 示例路径；若已修改，命令参数必须一起修改。服务数据、凭据和媒体均位于 Git 工作树外。

```sh
python3 services/lx-music/initialize.py \
  --data-path /srv/apple-music-downloader/lx-music \
  --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth
bash ops/stack.sh --profile lx check
bash ops/stack.sh --profile lx up lxserver
python3 services/lx-music/check.py \
  --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth
```

初始化脚本生成随机密码、私有 `data/config.js` 和权限为 0600 的 Bot 鉴权文件。它不输出密码，并拒绝覆盖已有配置、鉴权文件或非空数据目录。管理控制台和播放器在首次初始化时使用同一随机密码，LX 同步用户初始为空；需要同步客户端时在管理界面单独添加用户。配置在数据目录持久化，镜像内的默认账户不会被复制进新配置。

默认仅在宿主 `127.0.0.1:19527` 提供管理入口；可通过 SSH 隧道访问 `/admin`。在管理界面导入你有权使用的音源并确认搜索可用。仓库不附带音源脚本、私人 API 地址、token 或媒体；创建容器本身不保证上游音源可用。

完成根项目的 Bot 凭据及 Apple Music 初始化后，用 `bash ops/stack.sh --profile lx up` 启动整套服务。选用此叠加配置会启用 LX 备用路径。需要 Telegram 统一文本搜索时，另将根 `.env` 的 `AMDL_UNIFIED_MUSIC_ENABLED` 设为 `true`；按根文档配置对应入口。底层客户端支持无损音质排序；320k 是否允许由 `AMDL_LX_FALLBACK_ALLOW_320` 控制。

也可直接使用 Compose，但应始终携带同一组叠加文件；用过运维升级后，继续使用运维脚本以保留镜像覆盖：

```sh
docker compose -f compose.yaml -f services/lx-music/compose.yaml --profile lx up -d lxserver
```

## 既有服务接入

保留原有 `data/config.js`、`users.json`、用户目录、歌单、`serverInfo.json` 和下载队列；不要对生产目录运行初始化脚本。先停写并做私有备份，再把服务状态接到对应 `LX_DATA_PATH`。原先的根 `config.js` 只有在数据目录配置不存在时才会被官方服务迁移；以正在使用的配置为准。

`LX_FRONTEND_AUTH_PATH` 指向的宿主文件应保存现有 `frontend.password` 的原文。格式为一个非空 UTF-8 文本行，可有结尾换行；它不是 JSON，也不是 LX 同步用户密码或播放器密码。文件设为 0600，并允许 Bot 容器用户只读。Bot 通过 `x-frontend-auth` 发送该值，固定使用 `x-user-name: default`，下载写入公共用户 `_open` 的音乐目录。若管理密码发生变化，应同步更新此秘密文件并重启 Bot。

升级到本模板不会自动导入或修改你的现有生产部署。已有独立 Compose 项目的运行服务需要人工确认项目归属、路径和写入者后迁移；不要让两个 LXServer 实例同时写入同一数据目录。

## 共享路径契约

| 宿主路径或变量 | LXServer 内 | Bot 内 |
| --- | --- | --- |
| `${MUSIC_PATH}/LX Music` | `/server/music/_open` | `/music/LX Music` |
| `${LX_DATA_PATH}/data` | `/server/data` | 不挂载 |
| `${LX_FRONTEND_AUTH_PATH}` | 其值对应 `data/config.js` 的 `frontend.password` | `/run/secrets/lx-frontend-auth`，只读 |

Bot 基址为 `http://lxserver:9527`，同一项目的默认网络还提供 `lxserver-test` 别名。Bot 的 `AMDL_LX_FALLBACK_SERVER_MUSIC_ROOT=/server/music/_open` 必须精确对应服务器返回路径，不能把同一宿主目录再次映射一层 `_open`。如果接入既有外部服务，其实际路径为 `/server/music` 或自定义目录，应将挂载和 Bot 映射同时改为该真实路径。路径不一致会让队列显示完成，但 Bot 找不到文件。

## 验收与运维

`check.py` 只对配置和持久化队列执行 GET 请求，发送鉴权、不跟随重定向、不使用环境代理、不输出响应正文或秘密。它只报告 API 就绪及任务总数/忙碌数。容器 healthcheck 只验证 HTTP 可用；完整接入仍需检查鉴权队列、共享文件、索引和实际可用音源。

升级前暂停外部提交并等 Bot 与 LX 队列都空闲；历史 `error`、`finished`、`exists` 和取消状态可以保留，不能为升级清空历史队列。

```sh
python3 services/lx-music/check.py \
  --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth --require-idle
bash ops/stack.sh --profile lx backup /srv/private-backups/amdl-lx-before-upgrade
```

根运维脚本会备份 LX 的 `/server/data` 及 Bot 的鉴权文件；其中包含账号、配置、歌单和队列，属于私有备份。媒体目录不在此快照中，应单独备份。若旧部署另有根 `config.js`、自定义音源文件或额外挂载，也须独立备份并保护；原始日志同样按私有材料处理。

复核候选镜像的配置/数据兼容性，在隔离目录复制私有状态验证迁移不会删除原有账号、歌单或队列字段，并将候选镜像提前拉取到部署主机，再执行：

```sh
bash ops/stack.sh --profile lx upgrade lxserver REGISTRY/IMAGE@sha256:REVIEWED_DIGEST /srv/private-backups/amdl-lx-upgrade
bash ops/stack.sh --profile lx status
python3 services/lx-music/check.py --auth-file /srv/apple-music-downloader/secrets/lx-frontend-auth
```

升级后核对运行版本、镜像摘要、原有用户与歌单、队列记录、挂载位置，再用自己有权使用的歌曲确认文件及索引归档一致。API 检查不能替代实际下载验收。若失败，`bash ops/stack.sh --profile lx rollback /srv/private-backups/amdl-lx-upgrade` 只回滚镜像并保留当前数据；恢复数据快照需要离线评估，避免覆盖升级后新增内容。不要删除保留镜像、用户目录或媒体作为修复手段。

上游配置依据：[v2.1.2 配置示例](https://github.com/XCQ0607/lxserver/blob/9006e61d73f15764b8eb1388efbd55d8d3271370/config.js)、[启动与持久化逻辑](https://github.com/XCQ0607/lxserver/blob/9006e61d73f15764b8eb1388efbd55d8d3271370/src/index.ts)。
