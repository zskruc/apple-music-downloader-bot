# YouTube Music、YouTube 视频、字幕与登录持久化

[English](YOUTUBE.en.md) · [部署运维](OPERATIONS.zh-CN.md) · [认证服务与命令](../services/youtube/README.md)

本文依据仓库中的 `bot/youtube_ingest.py`、`bot/nas_youtube_auth.py`、`bot/auth_auto_refresh.py` 和 Compose 模板说明行为。YouTube Music 与 YouTube downloader 使用同一个 bot 和 yt-dlp 下载引擎，分别走音频、视频入库流程，不需要另外搭建两个下载站点。只处理你有权访问、下载和保存的内容；本仓库不提供账号、Cookie、媒体或访问权限。

## 1. 发送链接与任务分流

| 输入 | 行为 |
| --- | --- |
| `https://music.youtube.com/watch?v=VIDEO_ID` | 自动作为音乐任务，写入 YouTube Music 音频目录 |
| 普通 YouTube / youtu.be 链接 | Telegram 显示音乐、视频选择按钮；Synology Chat 需用明确命令 |
| `/ytmusic https://www.youtube.com/watch?v=VIDEO_ID` | 将普通 YouTube 或 YouTube Music 链接作为音乐处理；`/ytaudio` 是别名 |
| `/ytvideo https://www.youtube.com/watch?v=VIDEO_ID` | 作为视频处理，也适用于 YouTube Music 链接 |
| `/status`、`/progress` | 查看队列及当前任务进度 |
| `/deletevideo` | Telegram 分页选择视频，确认后删除媒体、关联元数据、字幕和对应下载记录 |

每条消息只接受一个 YouTube 链接，不能把 Apple Music、YouTube、哔哩哔哩链接混在同一消息。命令支持单集及 yt-dlp 能解析的播放列表/集合；`YT_PLAYLIST_MAX_ITEMS=50` 是默认安全上限。探测超过上限的集合会拒绝执行，不是静默下载前 50 项。不要把包含 `list=` 的链接误当作单集；只想下载某一集时发送不带列表参数的 watch 链接。

这里没有 YouTube 歌名搜索命令；发送纯歌名使用的是主 bot 的 Apple / LX / SQ 搜索。YouTube Music 使用链接。

## 2. 音频质量、路径与媒体库

音乐优先选择 `bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio`，然后由 yt-dlp 提取为 M4A，配置为 `--audio-quality 0`。已有兼容 AAC 音轨可能直接复制；其他音轨可能重新编码。**M4A 是容器，不表示无损；YouTube 的有损音源不会因此变成 ALAC、FLAC、Hi-Res 或录音室母带。** `--audio-quality 0` 也不是无损保证。

主 Compose 的映射如下；宿主机路径由私有 `.env` 指定，需位于本地数据盘，避免 Git 工作树、iCloud、Documents 和 Desktop。

| 用途 | 宿主机变量/路径 | bot 内路径 |
| --- | --- | --- |
| 音乐输出 | `YOUTUBE_MUSIC_PATH` | `/youtube-music` |
| 视频输出 | `YOUTUBE_VIDEO_PATH` | `/youtube-video` |
| 下载状态、历史、临时目录 | `STATE_PATH/youtube` | `/state/youtube` |
| 日志 | `STATE_PATH/logs` | `/state/logs` |

音乐按艺术家/专辑整理，文件名带 `[YT_VIDEO_ID]`；可写标签、封面，经 Beets 补充元数据，尝试歌词服务/LRCLIB 的 LRC，最后触发 Navidrome 扫描。`YT_BEETS_ENABLED`、`YT_LRC_ENABLED` 控制可选整理步骤；匹配结果和歌词时间轴需要实际核对。LRC 来自歌词匹配流程，和视频字幕是两套功能。

配置 `NAVIDROME_URL`、`NAVIDROME_USER`、`NAVIDROME_PASSWORD` 并在 Navidrome 映射同一宿主音乐目录，才能完成扫描。视频的 `EMBY_URL`、`EMBY_USER`、`EMBY_PASSWORD` 同理；启用 `EMBY_METADATA_SYNC_ENABLED` 时，`EMBY_YOUTUBE_LIBRARY_PATH` 应是 Emby 看到的视频根路径，而非 bot 内路径。扫描请求成功不代表播放器已可播放文件。`YT_OUTPUT_UID/GID` 要匹配媒体服务的读取权限。

## 3. 视频格式、分辨率与播放兼容

视频选择独立的 `bestvideo+bestaudio` 流，默认 `YT_VIDEO_MAX_HEIGHT=0` 不限制高度，并使用：

```text
YT_VIDEO_FORMAT_SORT=res,fps,hdr,quality,lang,tbr,vcodec,channels,acodec,asr
YT_VIDEO_MERGE_FORMAT=mkv
```

分辨率、帧率、动态范围等排在编码偏好之前；在前置条件相近时，yt-dlp 的[编码排序](https://github.com/yt-dlp/yt-dlp#sorting-formats)可选择 AV1 / VP9。实际只能下载当前账号、地区、客户端和源视频提供的格式。4K/8K、HDR 和特定编码都不是对所有链接的承诺。设置 `YT_VIDEO_MAX_HEIGHT=1080` 可以限制高度；调整 `.env` 后需要更新 bot 容器环境。

MKV 用来容纳源视频与音轨；合并通常不重编码视频，也不会提升源画质。旧电视、浏览器或 Emby 客户端对 AV1、VP9、HDR、MKV 的支持不同；检查实际编码及播放情况。需要更广泛兼容性时另做可控转码副本，并保留原文件。此视频选择器没有自动回退到低清合流格式，缺少可用独立流时可能失败。

视频目录按频道/集合/年份组织，每集目录含视频、可取得的 `poster.jpg` / `folder.jpg`、NFO、`youtube.info.json` 及处理后的外置字幕。JSON 可能含描述、媒体 URL 和账号可见内容信息，发布日志或示例前需脱敏。

## 4. 字幕选择、翻译与双语 ASS

媒体下载成功后，程序分开请求人工字幕和自动字幕，并转换为 SRT。通常根据 `.info.json` 的可用轨道选择：

1. 中文优先人工轨道；没有人工中文时尝试自动字幕/YouTube 提供的自动翻译中文轨道。优先顺序为 `zh-Hans`、`zh-CN`、`zh`、`zh-Hant`、`zh-TW`、`zh-HK`，再考虑其他 `zh-*`。
2. 原语种优先按视频语言元数据选择人工字幕，其次自动原文轨道。英文片或无法识别原语种时才以英文作为后备；日语、韩语等片源不应一律配成中英双语。
3. 中文字幕视频保留一份默认中文字幕 SRT。非中文视频同时有中文和原语种时生成一份中文 + 原语种的默认 ASS；只有中文时保留中文字幕 SRT。若非中文视频没有可用中文，当前整理逻辑会丢弃外语单轨，视频仍可入库但无字幕。

只有缺少可读取的媒体信息 JSON 时，才使用 `YT_AUTOMATIC_SUBTITLE_LANGUAGES` 的后备列表，默认是 `zh-Hans,zh-Hant,zh-CN,zh-TW,zh-HK,zh,en-orig,en,.*-orig`。修改这个变量不会取代正常流程的轨道选择逻辑。

**翻译由 YouTube 已提供的轨道决定。** 本流程没有调用本地翻译模型、翻译 API 或 Whisper 为视频重新识别、翻译字幕；仓库的逐字歌词 worker 也不会自动补全视频字幕。没有中文翻译轨道、字幕被禁用或下载失败时，不承诺生成中文字幕。

人工和自动字幕请求各自有认证策略回退，429 限流采用递增等待。默认每次字幕请求超时 900 秒、字幕间隔 2 秒、每种策略最多 4 次限流尝试，等待为 60、120、180 秒；对应变量为 `YT_SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS`、`YT_SUBTITLE_SLEEP_SECONDS`、`YT_SUBTITLE_RATE_LIMIT_RETRIES`、`YT_SUBTITLE_RATE_LIMIT_RETRY_SECONDS`。字幕失败记录在任务日志中，缺少字幕不会自动判定媒体下载失败。

双语 ASS 将中文与原语种按时间对齐，清理重叠/无效片段并输出质量统计；不保证每一句都成对。使用 1920×1080 的样式坐标，中文在上、原语种在下，文件名为 `视频名 [YT_ID].zh-CN.default.ass` 或 `zh-TW.default.ass`；单语为对应的 `.default.srt`。播放器需要加载同名外置字幕并支持 ASS 样式。程序会收敛为一个默认轨道，不会保留所有下载到的原始字幕。

字体、字号和留白可用以下参数调整，默认选择适用于 Linux 的 Noto CJK / Liberation 字体：

```dotenv
YT_BILINGUAL_CHINESE_FONT=Noto Sans CJK SC
YT_BILINGUAL_ENGLISH_FONT=Liberation Sans
YT_BILINGUAL_CHINESE_SIZE=68
YT_BILINGUAL_ENGLISH_SIZE=42
YT_BILINGUAL_MARGIN_V=64
YT_BILINGUAL_MARGIN_H=96
YT_BILINGUAL_SAFE_WIDTH=1728
YT_BILINGUAL_CUE_GAP_MS=80
```

bot 镜像安装 Noto CJK、Liberation 与 fontconfig，`.env.example` 显式选择上述字体；播放器/Emby 渲染端仍需安装相应的合法字体，ASS 文件不嵌入字体。本仓库不分发 PingFang 或 Arial 字体。仅生成软字幕不需要在 bot 内渲染；若由 Emby 烧录，字体需可供 Emby 使用。设置字号后仍应检查长句、竖屏视频与不同宽高比。

### 可选烧录副本

bot 默认输出外置软字幕，**没有自动烧录开关**。软字幕可切换，不改动视频；烧录会把字变成画面的一部分，必须重新编码视频。可在装有 libass 的 FFmpeg 环境单独制作副本。以下示例在一个私有工作目录执行，把所需文件以简单名称放在该目录中，避免滤镜路径的特殊字符转义问题；`fonts/` 放匹配 ASS 字体名称的合法字体：

```sh
ffmpeg -n -i input.mkv -map 0:v:0 -map '0:a?' \
  -vf 'ass=filename=subtitles.ass:fontsdir=fonts' \
  -c:v libx264 -crf 18 -preset medium -c:a copy output-with-subtitles.mkv
```

`-n` 避免覆盖现有输出。此例适合 SDR，不能作为保留 HDR/Dolby Vision 的通用转码方案；HDR 需另行设计色彩管理与编码流程。先试听试看并保留原视频和软字幕。FFmpeg 滤镜说明见[官方 ASS / subtitles 文档](https://ffmpeg.org/ffmpeg-filters.html#ass)。

## 5. 代理、PO Token 与依赖

bot 镜像固定安装 yt-dlp、yt-dlp-ejs、bgutil 插件、Deno 和 FFmpeg，版本见 `requirements.txt` / `Dockerfile`。Compose 内 `youtube-pot` 提供 PO Token HTTP 服务，bot 通过 `http://youtube-pot:4416` 使用它；PO Token 不是登录 Cookie，不替代账号权限，也不需要将一个私有 token 硬编码进仓库。

| 请求路径 | 配置和行为 |
| --- | --- |
| 媒体信息、大小预估、视频字幕、封面 | `YT_DOWNLOAD_PROXY_URL`；模板显式留空，在未另设通用代理环境变量时直连 |
| 大体积音频/视频下载 | 配置 `YT_LARGE_DOWNLOAD_PROXY_URL` 后，预估超过 `YT_LARGE_DOWNLOAD_THRESHOLD_MIB=500` 或大小未知时优先使用该路径；留空则沿用普通路径 |
| 下载过程中的网络错误 | 在两条不同路径间尝试切换，保留本次下载的分片；两条配置相同则不重复切换 |
| Chromium 登录浏览器 | 独立的 `YOUTUBE_BROWSER_ARGS`；需要时加入 `--proxy-server=http://host.docker.internal:7890`，不会自动继承 bot 的下载代理 |
| bot → PO 服务健康检测 | 容器内网直连 `/ping`，显式绕过系统代理 |
| Beets 元数据、LRCLIB 歌词查询 | 使用普通 `YT_DOWNLOAD_PROXY_URL`；本地 `YT_LRCAPI_URL` 请求则使用 urllib 默认的环境代理规则 |
| Telegram、Apple、Bilibili | 使用各自配置，不受 `YT_DOWNLOAD_PROXY_URL` 自动控制 |

大小预估默认最多 300 秒（`YT_DOWNLOAD_SIZE_PROBE_TIMEOUT_SECONDS`）。只配置实际可用、容器可达的代理。宿主机 `127.0.0.1` 在容器中指容器自己；模板为宿主服务提供 `host.docker.internal`。含用户名/密码的代理 URL 属于凭据，避免放进公开命令、问题报告和截图。

`YT_PLAYER_CLIENT=mweb` 与 PO provider 配合。正常解析可尝试 Cookie + PO、Cookie + embedded、匿名 embedded、匿名 + PO 等策略；没有 Cookie 的公开内容也可能成功。Cookie 候选校验则强制使用 Cookie 策略，禁止匿名回退，以免“公开链接能打开”被误判为“登录已恢复”。PO 服务故障时先检查健康和版本兼容；无 Docker socket 的默认部署需管理员通过运维命令恢复服务，bot 无法自行启动/重启它。

升级应共同检查 yt-dlp、EJS、Deno、PO 插件/服务和当前 YouTube 客户端行为。不要仅升级其中一个包就声称最高画质或登录仍兼容。外部规则会变化，参见 [yt-dlp YouTube 说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube)与 [PO provider 上游](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)。

## 6. Cookie 与 Chromium 配置持久化

| 私有数据 | 宿主路径 | 容器路径 |
| --- | --- | --- |
| 完整浏览器配置 | `YOUTUBE_BROWSER_PATH` | 浏览器 `/config`；bot 只读 `/youtube-auth-browser` |
| Chromium Default profile | `YOUTUBE_BROWSER_PATH/.config/chromium/Default` | bot `/youtube-auth-browser/.config/chromium/Default` |
| 生效的 Netscape Cookie 文件 | `STATE_PATH/youtube/cookies.txt` | `/state/youtube/cookies.txt` |
| 上一份 Cookie | `STATE_PATH/youtube/cookies.txt.previous` | `/state/youtube/cookies.txt.previous` |
| 自动刷新冷却状态 | `STATE_PATH/auth-refresh/youtube.json` | `/state/auth-refresh/youtube.json` |

重建 bot 或浏览器容器不会删除这些宿主 bind mount。不要执行清空数据目录的清理，也不要把 profile 放在容器临时层。默认 profile 路径由 `YT_AUTH_BROWSER_PROFILE_PATH` 指定；更换 Chromium 发行版/目录前应检查实际布局。

浏览器使用 `--password-store=basic`，导出器按 Chromium `BASICTEXT` 读取会话，所以完整 profile 与导出的 Cookie 都应按登录凭据保护。导出仅保留 `youtube.com` 及子域 Cookie，要求包含 SID、HSID、SSID、APISID、SAPISID、LOGIN_INFO、`__Secure-1PSID` 和 `__Secure-3PSID`。Cookie 名称齐全只证明结构完整，链接探测和真实下载仍需另验。

持久 profile 便于重新唤醒浏览器并刷新已有会话，**不保证 Cookie 永不过期**。YouTube 会轮换会话；登录结束后回到 `https://www.youtube.com/robots.txt`，关闭其他 YouTube 标签页并停止浏览器。不要把它当作日常浏览器反复播放视频。上游还描述了一次性无痕会话导出方式；本仓库提供的是可持续维护的专用 profile，不能保证与一次性导出有相同的会话寿命。参见 [yt-dlp Cookie 轮换说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)。

### 首次人工登录与导出

以下命令在部署主机的仓库目录运行，已先完成 `.env` 配置并启动 bot / PO 服务。保持相同的 `COMPOSE_PROJECT_NAME` 和部署选项；helper 自动继承现有镜像覆盖，启用了认证恢复或其他 profile 时，也给 helper 传入对应的 `--auth-refresh` / `--self-heal` / `--profile lx` / `--profile sq`：

```sh
bash services/youtube/youtube-auth.sh start
bash services/youtube/youtube-auth.sh status
```

浏览器 HTTPS 只监听宿主 loopback，默认 `127.0.0.1:31011`。远程操作时从本地电脑建立自己的 SSH 隧道，例如 `ssh -N -L 31011:127.0.0.1:31011 nas-user@nas.example.net`，再打开 `https://127.0.0.1:31011`。按自己的 SSH、端口和网络配置替换示例；不要将浏览器直接暴露到公网。自签名证书应在确认目标为自己建立的隧道后处理。

在专用 Chromium 页面由账号所有者手动输入密码、验证码和 2FA；不要交给 bot，不要粘贴到日志或 GitHub。确认 YouTube 已登录，然后返回 robots.txt，关闭其他 YouTube 标签页。用你有权访问且适合验证登录的实际链接替换示例：

```sh
bash services/youtube/youtube-auth.sh export 'https://www.youtube.com/watch?v=VIDEO_ID'
bash services/youtube/youtube-auth.sh status
```

helper 会停止 Chromium、通过 Docker 确认停止，再在运行中的 bot 内执行导出。导出器创建同目录 0600 候选文件，检查 Cookie 名称，强制使用候选 Cookie 和 PO/标准 Cookie 策略解析给定链接，关闭递归自动刷新与匿名回退；成功后保留原 Cookie 为 `.previous`，以原子替换使新 Cookie 生效。失败不会替换现有生效文件，临时候选会清理。不要跳过停止步骤：浏览器运行时可能仍在轮换或写入 SQLite 会话。

所有 yt-dlp 调用使用 0700 临时目录中的 0600 Cookie 副本；调用期间的 Cookie 更新只写入该副本，正常退出、`SystemExit` 和 `SIGTERM` 时清理，避免较早启动的下载进程退出时覆盖刚刷新过的持久 Cookie。`SIGKILL` 或宿主断电无法保证即时清理；停服务后检查并清理容器临时目录中的 `amdl-ytdlp-cookies-*` 残留，不触碰持久 Cookie/profile。持久文件的正式更新走认证导出流程。

解析探测不会下载整段媒体，也不证明某账户拥有所有视频或高级音频权限；随后用短小、获授权内容分别验证音频和视频。`status` 只输出存在性、数量、缺失名称等摘要，不输出 Cookie 值。

需要退回上一份会话时：

```sh
bash services/youtube/youtube-auth.sh rollback 'https://www.youtube.com/watch?v=VIDEO_ID'
```

回滚同样停止浏览器并探测备份候选；验证成功才交换当前与 `.previous`。若两份都失效，应重新人工登录。不要用文本编辑器手工拼接 Cookie；不要同时运行导出、回滚和自动刷新。底层 `--skip-probe` 只适合明确的离线维护，不是会话有效性验收；主 helper 始终要求链接验证。

## 7. 失效后的手动恢复与可选自动刷新

默认 Compose 禁用自动刷新，也不挂载 Docker socket。遇到登录/反机器人验证，先分清网络错误、PO 服务异常、缺失 Cookie 和会话拒绝；手动执行上一节的 start → 人工验证 → export。持久 profile 不能代替密码、2FA 或账号恢复。

需要自动刷新时先阅读 `compose.auth-refresh.yaml`，在私有 `.env` 设置 `ONLINE_AUTH_AUTO_REFRESH_ENABLED=true`，再使用显式 socket 覆盖启动目标 bot，例如：

```sh
bash ops/stack.sh --auth-refresh --profile auth up bot
```

这会把 Docker socket 授予 bot，具有宿主 Docker 管理权限；名称白名单不能构成权限隔离。`--auth-refresh` 不创建 watchdog，也不启用 wrapper 自动重启。已有 `--self-heal` 部署可继续保留该覆盖并启用同一 `.env` 开关；NAS 已有守护时继续复用原有巡检，不叠加重复循环。先人工创建并登录过浏览器容器，自动刷新才有可复用的 profile。

认证错误或下载格式不可用错误可触发以下流程：检查浏览器当前未运行 → 启动持久浏览器 → 等待 HTTPS 端口及默认 50 秒落盘时间 → 停止并确认 → 导出候选 → 使用当前链接强制探测 → 替换 Cookie → 同画质重试一次任务。若浏览器正在人工登录，不会接管；若需要密码/2FA，返回人工处理提示。SABR/格式限制不一定是 Cookie 问题，刷新也不保证恢复高清格式；再次失败就结束当前任务，让后续队列继续。它是错误触发的恢复，不是定时抓取 Cookie 或账号保活服务。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `ONLINE_AUTH_AUTO_REFRESH_SETTLE_SECONDS` | 12 | 各站点通用的页面就绪等待 |
| `YT_AUTH_AUTO_REFRESH_SETTLE_SECONDS` | 50 | YouTube 等待，实际取它与通用值的较大者 |
| `ONLINE_AUTH_BROWSER_READY_TIMEOUT_SECONDS` | 60 | 浏览器端口等待上限 |
| `ONLINE_AUTH_EXPORT_TIMEOUT_SECONDS` | 240 | 自动导出与链接探测上限 |
| `ONLINE_AUTH_SUCCESS_COOLDOWN_SECONDS` | 300 | 成功后 5 分钟复用结果，避免重复启动 |
| `ONLINE_AUTH_FAILURE_COOLDOWN_SECONDS` | 1800 | 失败后 30 分钟内不再自动尝试 |
| `YT_AUTH_AUTO_REFRESH_DISABLED` | false | 单次诊断/候选探测禁止递归自动恢复 |

冷却状态持久保存在 `/state/auth-refresh`。手动完成登录后正常 export 即可，不应频繁用 force 绕过失败冷却。手动维护期间暂停提交并避免运行自动恢复任务。

## 8. 去重、重试、备份和升级

同一来源、聊天、消息 ID 的重复投递会复用已有任务卡；另发一条消息会建立新的任务，然后由媒体历史去重。音频、视频分别记录在 `/state/youtube/archive-audio.txt`、`archive-video.txt`，详细输出记录在 `downloads.sqlite3`；同一 ID 的音频和视频是不同用途。每次下载使用临时 archive，只有文件成功整理并登记后才合并该 ID，减少“下载完成但入库失败”造成的永久漏项。视频还有同目录已有文件检查。不要为重试单个任务清空整个 archive 或数据库；已有记录且文件丢失需先针对该条记录诊断。

网络错误切换代理时保留本次分片；切换认证/token 策略时清理本次部分文件后重试。所有可重试高画质策略耗尽后，默认等待 `YT_HIGH_QUALITY_RETRY_DELAY_SECONDS=60` 再尝试一轮，`YT_HIGH_QUALITY_RETRY_MAX_CYCLES=2` 限制每次下载尝试最多两轮（配置范围 1–10），随后退出到可选浏览器恢复流程，不自动降画质。`YT_DOWNLOAD_TIMEOUT_SECONDS=21600` 是每次下载子进程的 6 小时上限，**不是整个任务的总时限**。关注 `/progress` 和任务日志，需要时使用任务卡停止；等待和浏览器刷新链路支持取消。结束或失败后的临时目录会清理，不能承诺跨任务、跨容器重建继续下载分片。

暂停外部提交并等任务结束，再使用现有运维工具；如部署启用了其他 profile / socket 覆盖，以下命令需带上相同选项，例如 `--auth-refresh` 或 `--self-heal`：

```sh
bash ops/stack.sh --profile auth backup /srv/private-backups/youtube-before-change
bash ops/stack.sh --profile auth upgrade bot local/apple-music-downloader-bot:reviewed-version /srv/private-backups/youtube-upgrade
bash ops/stack.sh probe
# 升级验收失败时：
bash ops/stack.sh --profile auth rollback /srv/private-backups/youtube-upgrade
```

目的目录必须是尚不存在的私有目录，位于 Git 工作树和源数据之外。快照会暂停写入者，覆盖 `.env`、状态、Cookie、完整浏览器 profile、相关服务私有数据和镜像切换记录；**不加密**，媒体库需单独备份。包含 `--profile auth` 将认证浏览器的 `/config` 纳入快照配置；带 `--auth-refresh` 时也保存该 overlay。不要把备份、Cookie、profile、数据库或日志上传到 GitHub。

`upgrade` 要求候选镜像已在本机拉取/构建，并保留旧 image ID；之后继续使用 `ops/stack.sh` 以保留 `STATE_PATH/ops/images.compose.json` 的镜像覆盖。`rollback` 只回退服务镜像，保留当前数据，不会恢复旧 Cookie、数据库或 profile。恢复数据应离线停止相关写入者，从同一一致性快照同时恢复 state/profile，核对 UID/GID 和挂载，再启服务验证；保留恢复前副本。不要并行启动两个 bot poller 或让两套 Chromium 写同一 profile。

## 9. 验收与排障边界

容器健康、浏览器能打开、Cookie 名称齐全和链接解析成功都是分层证据，不能代替真实下载。部署验收至少包括：

- 一条短音频和一条短视频：检查实际编码、时长、音轨、输出路径与权限，确认 Navidrome / Emby 真正播放。
- 一个小播放列表及一个超过限制的列表：验证集合数量、拒绝策略及重提去重；测试失败成员重试不污染成功历史。
- 人工中文、自动中文/翻译、非英文原语种、完全无字幕四类：检查字幕选择、缺失轨道提示、ASS 时间轴、字体、长句和播放端效果。
- 重建容器后仍能读取 state/profile，验证过期会话提示、人工重登、候选验证失败不覆盖旧 Cookie、备份回滚和自动刷新冷却。
- 网络故障与 PO 服务故障分别演练，确认错误归因及恢复范围；不要据此重启无关 NAS 服务。

分享诊断前删去 Cookie 值、完整 Cookie/profile、账号标识、token、代理认证、私有 URL、聊天 ID、完整环境配置及备份内容。正常日志会隐藏 Cookie 路径，并过滤常见代理 URL 中的用户名/密码；这不能保证任意上游输出都已完全脱敏。

本次补充按本地维护源码核对并进行离线检查。济南 NAS 的 IPv6 直连不可达，尚未核对线上最新状态，也没有在真实账号上完成浏览器登录、字幕请求、媒体下载、播放或在线备份恢复验收。公开文档说明的是仓库实现与待验收步骤，不能据此认定生产服务已通过这些检查。
