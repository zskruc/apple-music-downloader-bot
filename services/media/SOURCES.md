# Optional media services: sources and boundaries

This overlay references official publisher images; it does not redistribute
their layers, applications, private databases, accounts or media. Profiles are
independent (`navidrome`, `emby`, `lrcapi`) and disabled unless selected. Image
digests were read from publisher Docker Hub APIs on 2026-10-21; no image was
pulled or executed during preparation.

| Service | Reviewed release | Pinned multi-platform digest | Upstream terms |
| --- | --- | --- | --- |
| Navidrome | 0.64.2 | `sha256:38dc2727bfcfd5ede290f8ada114fc90368146f265ae4701ddddbcbe2a44ee52` | GPL-3.0, plus included components |
| Emby | 4.10.2.0 | `sha256:7082c63b10a2fcb3805a7eaebba407d5723bc57c085c9f0b29b7d0a702765cc1` | Emby proprietary terms and bundled component licenses |
| LrcAPI | 1.6.0 | `sha256:9efbf412d3c0f87e2f090954865666df2268ae2f14f104f9f430868e6abef2de` | GPL-3.0, plus included components |

## Primary references

- Navidrome: [official Docker installation](https://www.navidrome.org/docs/installation/docker/),
  [release 0.64.2](https://github.com/navidrome/navidrome/releases/tag/v0.64.2),
  [license at that tag](https://github.com/navidrome/navidrome/blob/v0.64.2/LICENSE),
  [publisher's image metadata](https://hub.docker.com/v2/repositories/navidrome/navidrome/tags/0.64.2).
  The current documentation uses `navidrome/navidrome`; the former official
  `deluan/navidrome:0.64.2` resolves to the same reviewed index digest. The
  overlay uses `user`, not LinuxServer-only PUID/PGID environment conventions.
- Emby: [official x64 Docker page](https://emby.media/docker-server.html),
  [official publisher image instructions](https://hub.docker.com/r/emby/embyserver),
  [release 4.10.2.0](https://github.com/MediaBrowser/Emby.Releases/releases/tag/4.10.2.0),
  [fixed tag metadata](https://hub.docker.com/v2/repositories/emby/embyserver/tags/4.10.2.0),
  [Emby terms](https://emby.media/terms.html).
  `linux/amd64` is selected explicitly for this suite. Optional paid features
  remain subject to Emby entitlement; no license key or bypass is supplied.
- LrcAPI: [official Docker instructions](https://docs.lrc.cx/docs/deploy/docker/),
  [1.6.0 source commit](https://github.com/HisAtri/LrcApi/tree/51bea152fca244fdcb5b52f95f6eaf681eede7fb),
  [license](https://github.com/HisAtri/LrcApi/blob/51bea152fca244fdcb5b52f95f6eaf681eede7fb/LICENSE),
  [fixed tag metadata](https://hub.docker.com/v2/repositories/hisatri/lrcapi/tags/1.6.0).
  This is `hisatri/lrcapi` directly; no third-party image accelerator, proxy
  account or Cloudflare credentials are included. Upstream lyric lookup can
  contact its public aggregation service and music providers; correct lyrics,
  service availability and rights to source content are not guaranteed.

## Configuration contracts

- Each service has separate writable private state. Media bind mounts reuse the
  bot's configured output paths and are read-only, with no implicit creation of
  media directories. Existing personal libraries are not mounted by default;
  `personal-libraries.compose.example.yaml` is an explicit additional opt-in.
- Host management ports bind only to `127.0.0.1`: Navidrome 14533, Emby 18096,
  LrcAPI 28883 by default. No host network, discovery port, hardware device,
  privileged mode or Docker socket is added.
- Internal endpoints are `http://navidrome:4533`, `http://emby:8096` and
  `http://lrcapi:28883`. The installer configures only selected internal
  integrations. Existing external player URLs and personal data remain outside
  this overlay; matching credentials must be stored in private configuration.
- Navidrome scans `/music`, with the bot's Apple, YouTube and Bilibili audio in
  separate subdirectories. Emby library examples use those same audio paths and
  `/video/YouTube`, `/video/Bilibili`. Library setup and actual playback require
  the deployment host, not just a responding management port.
- LrcAPI reads/writes its `/app/config` directory but cannot write to the media
  mounts. The bot's existing `/lyrics` client sends no API_AUTH header, so the
  sample leaves API authentication empty inside the private network/loopback
  boundary. No AI/translation key is provided. Configure additional access
  controls if intentionally exposing this service through a separate gateway.
- HTTP/SOCKS routing remains an explicit external deployment setting. No
  Mihomo service, subscription, private proxy URL or shared account is bundled.

Original overlay/configuration examples are covered by the repository's MIT
license. It does not relicense any referenced service or dependency.
