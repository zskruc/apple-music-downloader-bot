# YouTube 认证运维入口

[English](README.en.md) · [完整中文指南](../../docs/YOUTUBE.zh-CN.md)

本目录管理主 Compose 中已有的 `youtube-auth-browser` 与 bot 导出器，不另起 bot、定时器或守护。浏览器 `/config` 持久目录与 `/state/youtube` Cookie 目录必须位于 NAS 本地磁盘，并纳入私有备份。不要提交 Cookie、浏览器配置或刷新状态。

在部署仓库根目录运行，保持与已部署栈相同的 `.env` / `COMPOSE_PROJECT_NAME`。入口复用 `ops/stackctl.py`，自动继承运维工具保存的当前镜像覆盖文件 `state/ops/images.compose.json`：

```sh
bash services/youtube/youtube-auth.sh start
# 通过本地端口或 SSH 转发访问浏览器，自行输入密码、验证码、2FA。
bash services/youtube/youtube-auth.sh status
bash services/youtube/youtube-auth.sh export 'https://www.youtube.com/watch?v=PUBLIC_VIDEO_ID'
# 仅在确认上一份会话更合适时：
bash services/youtube/youtube-auth.sh rollback 'https://www.youtube.com/watch?v=PUBLIC_VIDEO_ID'
```

如果部署启用了相应运维选项，将同样选项放在动作前，例如 `bash services/youtube/youtube-auth.sh --auth-refresh --self-heal --profile lx --profile sq status`。仅填写实际启用的选项；`auth` profile 已自动加入。入口只启动浏览器，导出时在正在运行的 bot 内执行，不会重建 bot 或修改其镜像。

脚本先停止浏览器并用 Docker inspect 确认停止，再导出临时候选文件。导出器只保留 `youtube.com` 域、检查必需会话 Cookie、强制使用候选 Cookie 实测链接；全部成功后才原子替换。旧文件留在 `cookies.txt.previous`，权限均为 `0600`；回滚同样先校验，并将当前文件保存为新的 `.previous`。单份 `.previous` 是操作回退，不等同完整备份。停止浏览器是为了让 Chromium 写盘并避免导出正在轮换的会话；导出后保持登录浏览器停止。

`status` 只返回存在性、大小、文件权限、更新时间、缺少的标准 Cookie 名等元数据；不会证明实际下载可用。浏览器状态在没有 Docker socket 时显示 `null`，导出则由主机脚本确认停止。bot 容器未启动时先按主指南启动已有 bot；脚本不会为认证创建第二个 bot。

`bot/nas_youtube_auth.py` 另支持 `--profile`、`--destination`、`--probe-url`、`--status`、`--rollback`。`--browser-stopped` 仅用于没有容器内 Docker socket 时的显式停止确认，不能代替主机验证；有 socket 时仍检查容器。`--skip-probe` 只供明确离线维护，不能据此报告会话有效；常规脚本没有此开关。

自动恢复默认关闭。选择启用时，须同时显式启用 `ONLINE_AUTH_AUTO_REFRESH_ENABLED` 并挂载 Docker socket（高权限）；使用主指南指定的 opt-in Compose 配置。既有 `auth_auto_refresh.py` 在认证失败时按需启动/等待/停止同一浏览器，再实测导出；已有人操作浏览器时不接管。成功冷却默认 300 秒、失败 1800 秒；刷新状态文件权限为 `0600`，不会保存 Cookie 或原始提取器输出。它不能代替账号重新登录/2FA，不能保证 Google 接受现有会话。导出、回滚和人工登录期间暂停相关任务/关闭自动刷新，避免并行操作。

## 可选 SABR fork

默认镜像不下载、不安装 SABR fork，运行时也关闭该策略。需要时同时设置私有 `.env` 的 `INSTALL_YT_SABR=true`、`YT_SABR_FALLBACK_ENABLED=true`，工具路径为 `YT_SABR_TOOL_PATH=/app/vendor/yt-dlp-sabr/yt-dlp`。前者在构建时安装，后者在运行时启用；认证脚本本身不会下载或安装工具。

安装器 `install_sabr_tool.py` 固定版本 `2026.08.19.233452`、来源提交 `6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6`，并校验可执行文件 SHA-256 `8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d`。可变的上游 `sabr` 标签返回不同文件时拒绝安装。来源与独立许可见 [SABR_SOURCES.md](SABR_SOURCES.md)。它是预发布工具，不保证下载或 4K；候选 Cookie 校验和下载仍使用私有 Cookie 副本。启用并校验工具后，候选探测优先强制 `cookies-sabr`（仍需 PO 开启），不允许匿名回退；运行时普通下载策略还要求完整 Cookie 链和非空 provider 地址。

生产更新先构建全新镜像标签，例如 `docker build --build-arg INSTALL_YT_SABR=true --tag local/apple-music-downloader-bot:sabr-reviewed .`，再按[完整指南](../../docs/YOUTUBE.zh-CN.md#显式可选的-sabr-工具)执行备份、单服务 `upgrade` 和验收。`bash ops/stack.sh build bot` 会使用当前镜像覆盖选定的标签，不应直接覆盖仍需回退的生产标签。此次没有下载执行该可选工具，也没有因此更改 NAS 或发起新下载。

旧的 macOS Cookie 同步或 launchd 方案是可选外部集成，本项目不安装、不自动启用它。不要让 Mac 同步、人工导出与 NAS 自动恢复同时覆盖同一个 Cookie 文件；本仓库不包含个人 SSH 路径、账号或私有域名。
