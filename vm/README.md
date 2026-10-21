# 外部 Wrapper 虚拟机 / External wrapper VM

该容器只适用于 **x86-64 Linux + 可用的 `/dev/kvm`**。macOS Docker Desktop、ARM NAS 和没有 KVM 的机器不在此模板的运行范围内。部署者需要自行确认主机的 KVM 模块、设备权限和资源；本项目不自动修改 NAS 内核或安装主机守护。

`vm/Dockerfile` 从官方 [wrapper-manager 固定提交](https://github.com/WorldObservationLog/wrapper-manager/tree/d40d2046fead280f568c61a661cfdc10bd71c7dc) 编译独立 AGPL-3.0 服务。镜像附上游许可证和源码链接，不包含已登录账户、已下载的 wrapper/rootfs、设备文件或 NAS 私有镜像。wrapper-manager 在运行与登录过程中会获取其外部 wrapper 运行材料；这些材料的来源、授权与兼容性由部署者自行核对，不能由本仓库的 MIT 许可证推定。

## 首次初始化

1. 复制 `.env.example` 为 `.env`，填写本机绝对路径；先保留 `WRAPPER_SINGLE_ENABLE=false`。默认固定 [Ubuntu 24.04 cloud image，2026-10-17 发布](https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img)，`UBUNTU_IMAGE_SHA256=04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`。首次启动需要联网下载，缓存持久化到 `VM_DATA_PATH`；之后每次启动仍核验基础镜像缓存的 SHA-256。
2. 在项目根目录运行 `bash ops/stack.sh build legacy-wrapper`，再运行 `bash ops/stack.sh up legacy-wrapper`。初始没有账户时，decrypt 端口未就绪，容器显示 `unhealthy` 是预期的；此阶段先只启动该服务。manager 的 gRPC 端口映射到 NAS 的 `127.0.0.1:18080`（可配置），不是网页。
3. 通过受信任的 SSH 隧道访问该 loopback 端口，例如在管理电脑运行 `ssh -L 18080:127.0.0.1:18080 user@nas.example`。这只是通用示例，按实际服务器地址及安全连接策略修改。不要将 manager、SSH、account bridge 或 decrypt 端口公开到公网。
4. 使用上游 [manager.proto](https://github.com/WorldObservationLog/wrapper-manager/blob/d40d2046fead280f568c61a661cfdc10bd71c7dc/proto/manager.proto) 的双向流 `Login` RPC（支持 2FA）完成账户登录。也可以在**独立目录和独立环境**按 [AppleMusicDecrypt 的上游说明](https://github.com/WorldObservationLog/AppleMusicDecrypt)使用其 `tools/login.py`，配置该外部工具的 `[instance] url = "127.0.0.1:18080"`、`secure = false`。该工具为 AGPL 软件，未复制到本项目；`amdecrypt_once.py` 亦未收录。不要把账户或密码放进命令行、Git 文件或可分享日志。
5. 登录应只创建**一个**持久化实例。自行确认上游账户状态；提供自己适用的 `WRAPPER_DEVICE_INFO` 后，将 `.env` 的 `WRAPPER_SINGLE_ENABLE=true`，运行 `bash ops/stack.sh up legacy-wrapper` 重建容器。虚拟机盘保留，启动脚本切换到固定端口模式（10020/20020）并启动私有 account bridge（30020）。未提供设备描述或实例数量不是 1 时，服务会拒绝启动。
6. 确认 legacy-wrapper 的三个端口健康后再启动 wrapper、bot 和 PO 服务。`bash ops/stack.sh probe` 只读取健康和任务数量；它不证明真实下载、订阅有效或授权成功。真实功能验收需要部署者用自己有权限的内容执行。

会话、Ubuntu 磁盘、生成的 VM SSH 私钥和账户 tokens 都留在 `VM_DATA_PATH`，应按凭据保护，禁止上传 GitHub。删除该目录会丢失账户状态；升级和重建镜像无需删除它。每次启动都会核验基础镜像缓存；已有可写虚拟机盘会保留，不会被基础镜像校验自动替换。切换账户或恢复 manager 模式前先备份，且不要同时运行 manager 与 single 两种模式。

旧部署使用不同服务名时，映射到 `legacy-wrapper` 的恢复必须包含该原服务的完整镜像归档。安装器会加载并使用归档中的精确镜像，保留原来的启动入口，避免将旧虚拟机盘交给不同布局的新启动脚本。仅有数据的异构虚拟机迁移需要另行审定，默认拒绝；同名的本项目托管备份仍支持不带镜像恢复。

## English

This optional external runtime requires an **x86-64 Linux host with working `/dev/kvm`**. It is not a working configuration for macOS Docker Desktop, ARM NAS systems, or hosts without KVM. The template does not install host kernel modules or host daemons.

The image builds the separate AGPL-3.0 wrapper-manager from the fixed upstream revision linked above and retains its license/source URL. No authenticated VM, wrapper binary, device files, tokens, or proprietary NAS image is distributed. The manager obtains external runtime material during operation/login; its separate licensing and compatibility must be checked by the operator.

Initialize with `WRAPPER_SINGLE_ENABLE=false` and configure local bind paths in `.env`. The default base image is pinned to the Ubuntu 24.04 cloud release dated 2026-10-17, with SHA-256 `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`. Run `bash ops/stack.sh build legacy-wrapper` and `bash ops/stack.sh up legacy-wrapper`. Before login, failing transport health is expected. Only the VM should be started at this stage. The loopback management endpoint is **gRPC**, not an HTTP UI.

Forward the host's `127.0.0.1:18080` over your trusted SSH connection. Authenticate using the upstream `Login` bidirectional RPC and its 2FA exchange, or use AppleMusicDecrypt's separately installed AGPL `tools/login.py` with `[instance] url = "127.0.0.1:18080"` and `secure = false`. That helper and `amdecrypt_once.py` are not included here. Follow the external tool's own setup and licensing instructions; enter credentials interactively outside shared logs.

After exactly one account instance exists, supply your own applicable `WRAPPER_DEVICE_INFO`, set `WRAPPER_SINGLE_ENABLE=true`, and recreate `legacy-wrapper` with the maintenance script. The persistent VM switches to fixed-port mode and enables its private account bridge. Start the remaining stack only after VM transport health succeeds. A successful health probe does not verify content downloads or account entitlement.

Protect the entire `VM_DATA_PATH`, including the generated VM SSH key, account state, and disk images. Do not publish these files. Image updates preserve the writable VM disk; deleting the data directory destroys the session. The cached base image is verified on every startup; this check does not replace an existing writable VM disk.

Restoring an older deployment whose service name maps to `legacy-wrapper` requires the original service's complete image archive. The installer loads and uses that exact archived image, preserving its entrypoint instead of applying a different VM layout to the old disk. Data-only migration of a heterogeneous VM requires separate review and is refused by default. Canonical backups from this project's managed `legacy-wrapper` still support restoration without images. Infrastructure acceptance does not verify account login or content downloads.
