# LX Music、SQ Music 与统一音乐入口

仓库包含 `bot/lx_fallback.py`、`bot/sq_music.py` 和 `bot/unified_music.py`，以及 Bot 中的统一搜索、录音匹配、下载验收与入库逻辑。后端模板位于 [LX Music](../services/lx-music/README.md) 和 [SQ Music](../services/sq-music/README.md)，与主 Compose 项目共用网络。

## 处理范围

开启 `AMDL_UNIFIED_MUSIC_ENABLED=true` 后，文字搜索会查询已启用的来源并合并同一录音的候选；选择后按 **Apple Music → LX → SQ** 顺序尝试无损下载。单曲 ALAC 链接也可进入该流程。专辑、歌单、Atmos 等不应被理解为都会自动走三来源补齐。

Apple Music 的登录、网络或普通下载故障仍按原重试逻辑处理，不会一律当成缺曲。只有满足现有缺曲判定且没有待核对的新文件时才进入补充来源。匹配检查考虑艺人、版本、专辑和时长，不能只凭同名替换。

统一流程要求真实无损。旧 LX 独立模式可以通过 `AMDL_LX_FALLBACK_ALLOW_320` 允许经过验证的 320k MP3；统一无损流程不会因此接受 MP3。关闭的来源会跳过，不会读取其凭据或尝试连接。

## 三个开关

`.env.example` 的统一入口、LX 和 SQ 开关全部默认关闭。选用 `--profile lx` / `--profile sq` 时，叠加文件会为 Bot 启用对应适配并加载它所需的挂载；统一入口仍由 `.env` 中的 `AMDL_UNIFIED_MUSIC_ENABLED` 控制。

| 配置 | 用途 |
|---|---|
| `AMDL_UNIFIED_MUSIC_ENABLED` | 统一文字搜索与单曲无损调度 |
| `AMDL_LX_FALLBACK_ENABLED` | LX 适配，选 `lx` profile 时由叠加配置启用 |
| `AMDL_SQ_MUSIC_ENABLED` | SQ 适配，选 `sq` profile 时由叠加配置启用 |

也可以只选用其中一个后端。配置项完整示例见根目录 `.env.example`，不要把填完的 `.env` 提交到 Git。

## 准备后端

先按两个服务目录的 README 完成初始化，并按 SQ README 的源码校验与构建步骤准备本地补丁镜像。LX 使用固定版本的上游镜像；SQ 另需 MySQL、网易云 API 和与本客户端匹配的任务状态补丁。SQ 的“已接受下载”响应不代表文件完成，Bot 需要补丁提供的鉴权任务查询、完成状态和实际文件路径。

本仓库不附带用户的自定义音源脚本、平台 Cookie、VIP 会话、私有 API 地址或可用音乐源。LX 的音源配置和 SQ 的平台登录由部署者在自己的私有环境中准备。

首次部署时还需先完成主服务的 wrapper / Apple 账号初始化；已有生产 Bot 时使用独立测试 token，防止启动两个轮询器。

```sh
# 按服务 README 准备配置与私有凭据，再检查组合配置。
bash ops/stack.sh --profile lx --profile sq check
bash ops/stack.sh --profile lx --profile sq up lxserver sq-mysql sq-neteasecloudmusicapi sqmusic-server
# 完成 SQ 首次数据库/账号初始化、确认后端可用后，再启动 Bot。
bash ops/stack.sh --profile lx --profile sq up bot
```

只启用 LX 时去掉 `--profile sq`，并只操作 `lxserver`；只启用 SQ 时去掉 `--profile lx`。运维脚本会自动合并所选服务的 Compose 文件，不需要手工维护另一套主项目。

## 凭据与路径必须对应

| 内容 | 后端视角 | Bot 视角 |
|---|---|---|
| LX 已入库音乐 | `${MUSIC_PATH}/LX Music` → `/server/music/_open` | 同一宿主目录通过 `/music/LX Music` 可见 |
| LX Web 鉴权 | 私有 `data/config.js` 中的 frontend 密码 | `LX_FRONTEND_AUTH_PATH` → `/run/secrets/lx-frontend-auth`，只读单行文件 |
| SQ 下载暂存 | `SQ_STAGING_PATH` → `/music` | 同一宿主目录 → `/sq-staging`，只读 |
| SQ 公开音乐 | Bot 验收后复制入库 | `${MUSIC_PATH}/SQ Music` → `/music/SQ Music` |
| SQ 登录 | 初始化后的专用账号 | `SQ_AUTH_PATH` → `/run/secrets/sq-auth.json`，只读 JSON |

SQ 的凭据 JSON 只有 `username`、`password` 字符串；实际文件不随仓库分发。LX 的鉴权文件不是 JSON。两个文件的宿主路径与容器内路径是不同配置，不要把宿主路径直接填成容器内路径。

SQ 暂存目录必须独立于公开媒体库。Bot 不会把“队列已接受”当成完成，也不会通过展示歌名猜文件路径；它检查受控任务结果、真实 FLAC、时长及文件边界后再入库。两份服务叠加都使用既有文件的只读鉴权挂载，缺失文件不会自动创建成目录。

## 验收与运维

管理端口默认仅绑定回环地址；需要远程访问时通过自己的可信隧道。先检查来源搜索与鉴权，再用选定测试曲目验证下载、实际编码、路径、去重、终止任务和失败反馈。还应检查缺失音源、错误密码和后端不可用的情况，确认不会写入错误录音或重复接收任务。

```sh
bash ops/stack.sh --profile lx --profile sq status
bash ops/stack.sh --profile lx --profile sq probe
bash ops/stack.sh --profile lx --profile sq backup /absolute/private-backup
```

备份前暂停 Bot 与两个后端 Web 界面的外部提交，等待所有下载结束。运维脚本能检查 Bot 与歌词任务，不能据此认定后端手动提交的队列也已空闲。离线备份会先停写入者和应用，再停 SQ 数据库；包含 LX 配置数据、SQ 数据库与两份鉴权文件。日志、缓存、SQ 暂存媒体和公开音乐库不在默认私有备份范围，媒体按自己的存储策略另行备份。

启停、备份、升级和回滚时都传入与部署相同的 profile。升级使用新镜像或明确摘要，不自动切换 MySQL 主版本；数据库结构迁移不能靠镜像回滚撤销。主机已有巡检时继续复用既有机制，不另外安装重复守护。

本次补充已在离线环境验证代码和配置契约。NAS 的公网 IPv6 仍不可达，未执行线上版本核对、后端冷启动或真实下载验收。
