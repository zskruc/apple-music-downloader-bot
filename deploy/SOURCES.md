# Runtime dependency provenance

These dependencies are obtained on the deploying user's machine. This repository
contains original preparation code and references, not SQ Java sources, patched
SQ images, model weights, media, accounts, or third-party application bundles.
Every image default in `dependencies.DEFAULT_IMAGES` uses a digest. No mutable
tag is resolved during installation. Updating these pins requires review.

## SQ bridge

- Source: [SQ commit cf0f6ed136be0aa13845bbab028c27e404b89d5d](https://github.com/59799517/simple_sq_music_plus/tree/cf0f6ed136be0aa13845bbab028c27e404b89d5d).
- Source and base-image licensing/provenance: [existing SQ records](../services/sq-music/SOURCES.md).
- Preparation fetches only the exact commit into the user's external cache,
  verifies origin, commit, clean status and the two audited source hashes, and
  calls `services/sq-music/prepare_build.py`. A temporary external context builds
  the bridge using the pinned base image's JDK/dependencies. Generated source is
  removed after the build; the user's source cache remains for retries.
- No upstream license was found at this audited SQ revision. Local fetching and
  patching do not grant redistribution rights. Original helper code is MIT;
  neither that license nor automation relicenses SQ or authorizes publishing its
  sources/images. Installation does not push any image or source archive.

## Whisper worker models

The [whisper.cpp model repository](https://huggingface.co/ggerganov/whisper.cpp)
identifies these GGML conversions of OpenAI Whisper models as MIT-licensed. Its
[model card at revision 5359861c739e955e79d9a303bcbc70fb988958b1](https://huggingface.co/ggerganov/whisper.cpp/blob/5359861c739e955e79d9a303bcbc70fb988958b1/README.md)
publishes the SHA-1 values required by the existing worker. The pinned
[Hugging Face file metadata](https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1)
provides LFS SHA-256 and byte counts. `prepare_models` verifies all three before
atomic publication; it does not accept hash or source overrides.

| Model | Bytes | SHA-1 | SHA-256 |
| --- | ---: | --- | --- |
| `ggml-large-v3-turbo-q5_0.bin` | 574041195 | `e050f7970618a659205450ad97eb95a18d69c9ee` | `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2` |
| `ggml-large-v3-q5_0.bin` | 1081140203 | `e6e2ed78495d403bef4b7cff42ef4aaadcfea8de` | `d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1` |

Both downloads use immutable-revision HTTPS URLs on that repository. Interrupted
transfers keep mode-0600 `.part` files and resume through a checked HTTP Range
response. A server ignoring Range causes a restart, never concatenation. An
already existing model is hashed before reuse; a mismatch is preserved and
rejected. Complete candidates must match the pinned size and both digests.
Only verified files become mode-0644 worker inputs. Weights are not downloaded
when importing the module, running synthetic tests, or reading configuration.

## Images

Public publisher metadata was read on 2026-10-21. Navidrome, Emby, LrcAPI, MySQL,
Chromium and PO-provider digests below were obtained from their Docker Hub tag
APIs, including `linux/amd64` entries. The version labels identify the publisher's
tag observed at review time; the immutable digest, not that tag, is used at
runtime. LX, SQ-base and NetEase pins were also requested directly from their
public registries: the returned manifest bytes matched each pinned SHA-256.
These were metadata requests, not image-layer downloads. Their component source
records remain applicable.

| Variable | Reviewed version | Image digest |
| --- | --- | --- |
| `SQ_MYSQL_IMAGE` | Docker Official MySQL 5.7.44 | `mysql@sha256:4bc6bc963e6d8443453676cae56536f4b8156d78bae03c0145cbe47c2aad73bb` |
| `SQ_NETEASE_IMAGE` | SQ upstream's NetEase API v4.32.0 | `registry.cn-hangzhou.aliyuncs.com/sqdockler/netease_cloud_music_api@sha256:87e4c7bf459da928309fd3c8840142f0f005b6a0f8812f5360acb3eac1e8553c` |
| `SQ_BASE_IMAGE` | SQ 3.1.33 | `ghcr.io/59799517/simple_sq_music_plus@sha256:4f3e499c07da526bea5648fc4e1b9c449e363356c5e8c1ef4d96c2d67b1e20f2` |
| `LX_IMAGE` | Audited LX service | `ghcr.io/xcq0607/lxserver@sha256:aa3db966049584585639ca6563e85ce5b7ebf33569dd1f5064c21bd7330382af` |
| `AUTH_BROWSER_IMAGE` | LinuxServer Chromium, publisher `latest` snapshot on 2026-10-21 | `linuxserver/chromium@sha256:9761e4dce2375165ab85566d5a58920525d49c49469ed7a08a6d874dcdb1cedc` |
| `PO_PROVIDER_IMAGE` | bgutil PO provider 2.0.2 | `brainicism/bgutil-ytdlp-pot-provider@sha256:d7d34108b76e52c7d3b575fe9e797e4bf41c1b2a08f80c5292eff6f851ce8472` |
| `NAVIDROME_IMAGE` | Navidrome 0.64.2 | `navidrome/navidrome@sha256:38dc2727bfcfd5ede290f8ada114fc90368146f265ae4701ddddbcbe2a44ee52` |
| `EMBY_IMAGE` | Emby 4.10.2.0 | `emby/embyserver@sha256:7082c63b10a2fcb3805a7eaebba407d5723bc57c085c9f0b29b7d0a702765cc1` |
| `LRCAPI_IMAGE` | LrcAPI 1.6.0 | `hisatri/lrcapi@sha256:9efbf412d3c0f87e2f090954865666df2268ae2f14f104f9f430868e6abef2de` |

Publisher and license references:

- [MySQL official image](https://hub.docker.com/_/mysql) and [5.7.44 registry metadata](https://hub.docker.com/v2/repositories/library/mysql/tags/5.7.44).
  The historical 5.7 line is retained for SQ compatibility, not silently migrated
  to another database major version. MySQL and image dependencies retain their
  own GPL/commercial and component terms.
- [LinuxServer Chromium documentation](https://docs.linuxserver.io/images/docker-chromium/),
  [publisher image source](https://github.com/linuxserver/docker-chromium),
  [frozen tag metadata](https://hub.docker.com/v2/repositories/linuxserver/chromium/tags/latest).
  This uses the verified publisher's Docker Hub artifact rather than assuming a
  digest is interchangeable with another registry. Container scripts and bundled
  Chromium/system packages have their own licenses.
- [bgutil source](https://github.com/Brainicism/bgutil-ytdlp-pot-provider),
  [2.0.2 metadata](https://hub.docker.com/v2/repositories/brainicism/bgutil-ytdlp-pot-provider/tags/2.0.2).
  Upstream GPL-3.0 and bundled component licenses apply.
- [Media-service primary sources](../services/media/SOURCES.md) cover Navidrome,
  Emby and LrcAPI separately. [LX sources](../services/lx-music/README.en.md) and
  [SQ sources](../services/sq-music/SOURCES.md) cover the retained optional music
  backends and their distribution boundaries.

These metadata checks and fixture tests did not pull/run the images, download
the large weights, boot the VM, sign in to services or verify a fresh deployment.

## Docker build bases and compiled upstreams

The bot and VM Dockerfiles also pin these publisher artifacts. Their tags are
human-readable review labels; the digest fixes the selected manifest. Metadata
was checked on 2026-10-21 without downloading image layers.

| Build input | Tag and digest | Primary metadata |
| --- | --- | --- |
| Bot Python runtime | `python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` | [Docker Official Python](https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim-bookworm) |
| Deno executable stage | `denoland/deno:bin-2.9.6@sha256:4cf0029b9aeeeed5efcbb71828737f0d7c8c8a20072df960e51a5679ef0d21ba` | [Deno publisher metadata](https://hub.docker.com/v2/repositories/denoland/deno/tags/bin-2.9.6) |
| whisper.cpp build and VM runtime | `debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8` | [Docker Official Debian](https://hub.docker.com/v2/repositories/library/debian/tags/bookworm-slim) |
| wrapper-manager compiler | `golang:1.26.10-bookworm@sha256:5b1f78db1f54cc8cb46e652c51d3f854441c4310a449161b6fe9fb553edab505` | [Docker Official Go](https://hub.docker.com/v2/repositories/library/golang/tags/1.26.10-bookworm) |

- [whisper.cpp revision 4979e04f5dcaccb36057e059bbaed8a2f5288315](https://github.com/ggml-org/whisper.cpp/tree/4979e04f5dcaccb36057e059bbaed8a2f5288315)
  supplies the CPU `whisper-cli`; its upstream MIT license is retained in the
  image. The model weights are separate verified downloads described above.
- [wrapper-manager revision d40d2046fead280f568c61a661cfdc10bd71c7dc](https://github.com/WorldObservationLog/wrapper-manager/tree/d40d2046fead280f568c61a661cfdc10bd71c7dc)
  remains AGPL-3.0; its license and source revision are retained in the VM image.
  Its [go.mod](https://github.com/WorldObservationLog/wrapper-manager/blob/d40d2046fead280f568c61a661cfdc10bd71c7dc/go.mod)
  declares Go 1.23.0 and toolchain 1.23.5. The pinned Go 1.26.10 compiler is from
  a supported line under the [Go release policy](https://go.dev/doc/devel/release#policy).
  This does not substitute for actually compiling and exercising the manager.
- Deno's MIT license is copied from [the retained notice](../licenses/Deno-MIT.txt).
  Python, Go, Debian and their bundled libraries retain their own PSF,
  BSD-style and package-specific terms. ffmpeg, fonts and other apt-installed
  packages keep their distribution licenses; the repository's MIT license does
  not replace them. See [third-party notices](../THIRD_PARTY_NOTICES.md).
- The optional [SABR source record](../services/youtube/SABR_SOURCES.md) fixes
  variant `2026.08.19.233452`, upstream commit
  `6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6`, and executable SHA-256
  `8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d`.
  Its original Unlicense/component notices are preserved. Manual builds opt in;
  the full installer enables the build and runtime settings. A pre-release fork
  does not guarantee media availability, quality or 4K playback.

Pinned bases and source revisions do not make every build bit-for-bit
reproducible: apt repositories, transitive build tools and upstream guest
provisioning still use their own package-resolution and network mechanisms.

## Ubuntu KVM base image

The default is the official Ubuntu 24.04 amd64 cloud image from the dated
[2026-10-17 release directory](https://cloud-images.ubuntu.com/releases/noble/release-20261017/):

- File: `ubuntu-24.04-server-cloudimg-amd64.img`.
- SHA-256: `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`,
  verified against the official [SHA256SUMS](https://cloud-images.ubuntu.com/releases/noble/release-20261017/SHA256SUMS).
- The VM entrypoint verifies both newly downloaded candidates and an existing
  cached base before use. Persistent guest disks, login state and wrapper
  sessions are private runtime data, not source-release assets.
- Ubuntu is a distribution of components under their respective licenses;
  consult its [licensing policy](https://ubuntu.com/licensing) and package
  copyright files. The image is not relicensed under this repository's MIT terms.
  New guest provisioning may need Ubuntu repositories and upstream downloads;
  saving this base image alone is not a complete offline installation.

## Source releases and private migration bundles

The v0.2.0 bootstrap downloads `source.tar.gz` and its SHA-256 file from the
repository's fixed-version GitHub release. `scripts/build_release.py` packages
the committed public tree. A source archive contains no media, passwords,
cookies, models, third-party application layers or VM disks. The archive hash
checks transfer integrity against the expected release metadata, not an
independent publisher signature.

Full `install.py backup` bundles are a different artifact: they include private
runtime files, media and Docker image archives by default. They must remain
private and are not encrypted by the helper. `--without-media` and
`--without-images` explicitly record omissions. A verified source archive plus
a fully initialized private bundle and its images supports offline recovery of
the recorded inputs; missing images, model files or unfinished guest setup can
still require network access. See the [deployment guide](../docs/ONE_COMMAND.en.md).

The deployment acceptance harness and isolated CI workflow are provided, but a
real Docker/KVM run and authenticated business acceptance have not yet been
completed in this round. No fresh download, VM boot or playback result is
claimed by the metadata records above.
