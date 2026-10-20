# 部署与运维

本手册适用于本仓库的可移植模板。生产环境应保存自己的配置、镜像摘要和验收记录。初始公开版本尚未完成干净主机部署与真实账号验收，不要直接覆盖现有生产目录。

## 1. 准备环境

使用 Linux x86-64、Docker Engine 和 Compose 插件。VM 方案需要宿主提供 `/dev/kvm`，BIOS 已开启虚拟化；Synology 的 KVM 模块和开机加载方法随机型、DSM 内核而异，先确认实际设备存在，不要套用其他机器的模块文件。

把源码放在专用本地开发或部署目录。建立 `.env`，限制为管理员可读，填写真实 token、允许的 chat ID、媒体位置与需要的代理。空 Telegram allowlist 在原服务逻辑中意味着放行所有聊天，因此必须设置明确的允许列表。Synology Chat 同样需要 webhook token 和允许的用户。

```sh
cp .env.example .env
chmod 600 .env
bash ops/stack.sh check
bash ops/stack.sh build
```

不要把完整展开配置、`docker inspect` 环境变量或 wrapper 账户接口返回值写进公开日志。不要把生产 `.env` 复制到源码目录之外的公开产物中。

## 2. 第一次启动与账号初始化

先单独启动 VM，按 [VM 说明](../vm/README.md) 完成上游 wrapper 初始化与交互式登录。账号必须有有效的 Apple Music 订阅。登录、2FA、会话与账号恢复均由使用者在自己的环境完成；仓库不提供账号和设备材料。

```sh
bash ops/stack.sh up legacy-wrapper
bash ops/stack.sh status
```

初次 VM 启动、cloud-init 和上游组件准备可能较慢。管理端口仅绑定宿主回环地址，远程使用 SSH 隧道。VM 的 manager 模式用于初始化；完成初始化后按 VM 说明切换持久实例运行方式，再启动依赖它的服务。

确认 wrapper 账户、M3U8 与处理通道都能实际工作后再启动其余默认服务：

```sh
bash ops/stack.sh up
bash ops/stack.sh probe
```

默认栈包含 bot、wrapper adapter、legacy-wrapper VM、PO token provider。登录浏览器使用 `auth` profile，歌词服务使用 `lyrics` profile。逐字声学对齐另需 whisper.cpp 可执行文件及匹配校验值的模型；只有模型文件名相同不代表模型已准备好。参阅 Compose 中相应环境变量和挂载，不要假定源码仓库包含模型。

YouTube / 哔哩哔哩的登录浏览器只在登录和 Cookie 刷新时启动，完成后停止。浏览器配置与导出的 Cookie 均为私有数据。启用自动浏览器管理或自愈前，阅读 `compose.self-heal.yaml`；Docker socket 可赋予容器宿主级控制权。

## 3. 验收清单

健康端口只用于辅助判断。上线前至少完成：

1. 用隔离测试 bot 发起搜索、单曲、专辑任务，验证选择、进度、完成与失败消息。
2. 用已授权账号验证所需的 ALAC / Atmos 模式，检查文件编码、时长、标签和可播放性。
3. 重复提交同一曲目，检查去重结果及目标目录，确认不会误跳过不同版本。
4. 如需 YouTube / 哔哩哔哩，分别验证音频、视频和登录失效后的反馈。
5. 如需歌词，核对 LRC / TTML 与音频实际时间轴；确认失败和取消后任务会正确结束、重试和通知，不用“文件存在”代替质量判断。
6. 让 Navidrome / Emby 扫描目标位置，确认实际媒体条目与播放。
7. 重启本栈，确认任务状态保留、只出现一个 Telegram 轮询器，VM 和依赖按顺序恢复。
8. 在可控维护窗口验证宿主重启后的 KVM、存储挂载和容器恢复，不能据此直接声称断电恢复已通过。

验收只使用专门选定的测试内容。故障排查优先读取状态，避免反复启动第二个生产轮询器。

## 4. 日常检查与自愈

```sh
bash ops/stack.sh status
bash ops/stack.sh probe
```

`probe` 应与实际队列、最后完成任务和媒体结果一起查看。检查磁盘余量、权限、挂载是否就绪以及虚拟机启动状态。一个下载失败不能直接归因为整个系统故障。

`compose.self-heal.yaml` 提供按服务恢复的可选配置。启用前核对容器名称、失败次数、宽限期和冷却时间。宿主已有巡检时复用现有机制，不安装第二套执行相同恢复的定时任务。自动恢复应只针对本栈相关服务，不能重启整台 NAS 或其他家庭服务。

## 5. 私有备份

```sh
bash ops/stack.sh backup /absolute/private-backup-directory
```

备份会暂停写入者并恢复此前运行的服务，属于维护操作，应选空闲窗口。确认备份范围覆盖实际配置、任务 / 歌词数据库、Cookie / 登录浏览器状态、wrapper 会话和 VM 数据；媒体目录另行备份。外置挂载、外部数据库和额外模型不应默认视为已备份。

备份含凭据和账号会话，只能放在受限目录或加密存储，绝不能上传公开 GitHub。记录源码 commit、Compose 配置版本、所有镜像 ID / digest、宿主版本和备份时间。至少做一次离线还原演练；仅有压缩包不代表可恢复。

升级前后请始终通过 `bash ops/stack.sh` 管理本栈；脚本会保留镜像覆盖记录。直接运行 `docker compose up` 可能忽略已选定的升级镜像。

## 6. 升级与回滚

先核对上游版本、GAMDL CLI / wrapper 协议、yt-dlp 与 PO provider 客户端 / 服务端兼容性。不要让 `latest` 直接替换已知可用组件。先构建新标签，在无生产 token、无生产可写挂载的隔离环境验收。

```sh
bash ops/stack.sh upgrade bot local/apple-music-downloader-bot:reviewed-version /absolute/private-upgrade-backup
bash ops/stack.sh probe
# 若验收失败：
bash ops/stack.sh rollback /absolute/private-upgrade-backup
```

脚本的升级范围是指定服务的镜像切换；数据库迁移、环境变量变化、VM 固件和模型更新需要单独评估。镜像回滚不能撤销数据库结构变更，需要时从私有一致性备份恢复数据。恢复账号状态前确认旧服务已停止，避免两个实例写同一状态目录或争用 token。

## 7. 常见故障

| 现象 | 检查与处理 |
|---|---|
| Telegram `409 Conflict` | 查同一 token 的其他轮询器，保留一套；反复重启不能解决双实例。 |
| Telegram 网络超时 | 分别验证 API 代理、DNS 与下载代理；这些路径可能独立，不能只看浏览器可上网。 |
| wrapper 端口通但任务失败 | 检查已登录账号、订阅、会话、M3U8 和实际处理通道；不要公开 `/me` 响应。 |
| VM 无法启动 | 查 `/dev/kvm`、虚拟化支持、磁盘空间、VM 数据权限和 cloud-init 状态。 |
| 媒体没有入库 | 对照容器与宿主挂载、媒体库扫描根目录、实际输出文件与权限。 |
| YouTube 登录错误或 PO token 错误 | 查客户端与 provider 版本、代理、Cookie 状态，按需登录并停止浏览器。 |
| 歌词任务积压 | 区分来源匹配、参考文本、对齐模型、worker 心跳与队列租约；先保留失败证据，避免盲目清库。 |
| 容器健康但无业务结果 | 检查实际实体：队列任务、完成文件、可播放性、歌词时间轴和媒体扫描。 |

## 8. 迁移

先备份旧环境，再在新主机恢复私有状态与同版本镜像。核对 UID/GID、媒体根目录、所有路径映射、模型摘要、KVM 与代理。停止旧轮询器后才启用新 bot；完成真实任务验收前保留旧环境和回滚资料。不要把 VM 磁盘或私有状态当作开源源码上传。
