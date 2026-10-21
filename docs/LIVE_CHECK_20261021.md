# 线上核验 / Live deployment check — 2026-10-21

本次通过连接时解析的域名 AAAA 记录、IPv6 SSH 直连进行只读检查。
没有改动 NAS 配置、重启服务、启动登录浏览器、导出 Cookie 或触发新下载。
私有源码快照留在本地开发目录，没有上传账号状态、Cookie、浏览器 profile、
媒体、原始日志或实际部署配置。

## 已确认

- Bot、wrapper、PO provider、歌词 worker、LX 和 SQ 相关容器均在运行。
  Bot 与 PO 健康接口返回成功；本栈容器没有重启计数。
- 对照运行容器内的全部 57 个公开 Python 源码文件：41 个逐字节一致，
  其余差异按功能审查。路径、项目标识、字体、许可说明及公开版安全改进
  均有明确用途。核对发现的一处遗漏已补回：服务退出时保留原任务编号、
  将未完成任务留待恢复；用户主动停止仍然停止。
- 状态目录和完整 Chromium profile 都是宿主持久挂载；Bot 对 profile 只读，
  登录浏览器使用同一宿主目录。浏览器当前停止；活动 Cookie 为 `0600`，
  profile 目录为 `0700`。
- 已有视频及 SRT/ASS 字幕确实存在。抽查三份既有 MKV，FFprobe 可读取
  3840×2160 VP9 视频、AAC 音频、时长及同目录字幕。没有重新下载或实际播放。
- 视频选格式、字幕语言选择、双语对齐、重试上限和取消支持的核心行为，
  与整理时的维护资料相符；没有发现此前未部署的视频优先候选规则。
- 实际安装的 yt-dlp、EJS、PO 插件版本与仓库固定依赖相符。
- 线上专用 SABR 工具确实存在，SHA-256 与上游公开发布资产相符。
  相关兼容路径已作为显式可选能力补齐，默认关闭；来源、固定校验值及
  第三方许可见 [SABR_SOURCES.md](../services/youtube/SABR_SOURCES.md)。

## 公开版与生产版的区别

公开版新增的 Cookie 状态查询、`.previous` 回滚、导出文件锁、强制候选校验、
无 socket 时的停止确认、刷新状态 `0600` 和日志脱敏，尚未自动部署到 NAS。
生产版刷新状态文件仍使用旧权限，且没有 `.previous`；这不能据此判断
当前 Cookie 已失效。生产版禁止下载进程回写共享 Cookie，公开版使用私有副本。

生产版字幕引用 PingFang/Arial；Bot 内字体查询实际回退到 DejaVu。
公开镜像改用 Noto CJK/Liberation。外置 ASS 不嵌入字体，播放端或 Emby
渲染器仍须独立检查，不能把 Bot 的字体查询当作播放效果验收。

构建上下文审查另修复了 Cookie 文件忽略规则误排除 Python 执行器的问题。
CI 增加离线 `FROM scratch` / `COPY` 检查，确认执行器、可选安装器和许可文件
真正进入镜像构建上下文；这不代替完整服务镜像构建。

## 尚未验证

本次未验证当前账号 Cookie 的实际可用性、YouTube Music 新下载、字幕在线请求、
媒体播放、Navidrome/Emby 扫描、新镜像构建、手动/自动重新登录或备份恢复。
已有音频目录没有可用于抽查的音频文件。健康接口和既有视频不能代替这些检查。
本仓库源码与可选工具的测试通过，也不能视为生产更新已部署。

## English

A read-only check used the domain's current AAAA resolution and direct IPv6
SSH. No NAS configuration or service was changed, browser started, Cookie
exported or new download submitted. Private snapshots remain outside the
public checkout.

All 57 published Python files were compared with the running bot: 41 matched
byte-for-byte; the remaining differences were reviewed. One missing deployed
behavior was restored: preserve the same unfinished task for service restart,
while honoring an explicit user stop. Other differences concern portable
defaults, attribution and documented safety improvements.

The relevant services were running, bot/PO health checks succeeded, and
state/browser bind mounts were persistent. Existing video/subtitle files were
present; three MKV samples had readable 4K VP9/AAC streams and subtitle sidecars.
The production SABR executable matched the public upstream asset's SHA-256;
its path is now explicitly optional in this repository with preserved notices.

The new Cookie rollback/locking/status features are not deployed automatically.
Production still uses the older refresh-state permissions and has no `.previous`
copy. Font fallback inside the bot does not establish rendering on a separate
player. No fresh audio/video download, authenticated probe, caption fetch,
playback, full image build, login recovery or backup/restore was performed.
