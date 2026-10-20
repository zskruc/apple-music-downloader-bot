# Third-party notices

The repository's root license applies to its original source code. Third-party
software and data retain their own licenses; the root license does not relicense
them. Names and trademarks belong to their respective owners.

## Included dictionary data: OpenCC

`bot/word_lyrics_automation/_opencc_t2s.py` contains a
modified subset of OpenCC's traditional-to-simplified character dictionary.
OpenCC was developed by BYVoid and contributors. The source used to create this
subset was the dictionary distributed with `opencc-python-reimplemented 0.1.7`.

- [OpenCC source](https://github.com/BYVoid/OpenCC)
- [Source dictionary](https://github.com/BYVoid/OpenCC/blob/master/data/dictionary/TSCharacters.txt)
- [Python package source](https://github.com/yichen0831/opencc-python)
- License: Apache License 2.0; the full source-package license is retained in
  [licenses/OpenCC-Apache-2.0.txt](licenses/OpenCC-Apache-2.0.txt) and embedded in the module.
- Modification made on 2026-10-17: retain only unambiguous mappings with one
  source code point and one target code point; exclude phrase and contextual
  conversions; collapse safe chains to their terminal target.
- Source dictionary SHA-256:
  `6b5a0a799bea2bb22c001f635eaa3fc2904310f0c08addbff275477a80ecf09a`.

Preserve the module's provenance, modification notice, checksums, and embedded
license when copying or redistributing it.

## External runtime components

These components are installed or run separately. Their source archives,
executables, wheels, Docker image layers, account sessions, and device files are
not included in this source repository. License copies below are provided for
reference and attribution.

| Component | Role | Upstream license / reference |
| --- | --- | --- |
| [Deno](https://github.com/denoland/deno) | JavaScript runtime copied from the official Deno image at build time | MIT; copyright 2018-2026 the Deno authors. [License copy](licenses/Deno-MIT.txt). |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Optional speech alignment executable built from a pinned upstream commit | MIT; copyright (c) 2023-2024 The ggml authors. [License copy](licenses/whisper.cpp-MIT.txt). |
| [GAMDL](https://github.com/glomatico/gamdl) | Apple Music command line backend; used by the storefront and library adapter | MIT; copyright (c) 2024 Glomatico. [License copy](licenses/GAMDL-MIT.txt). |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube/Bilibili command line backend | Source: Unlicense. [License copy](licenses/yt-dlp-Unlicense.txt). Bundled dependencies and release executables can have additional licenses; consult the exact release's license information. |
| [wrapper-manager](https://github.com/WorldObservationLog/wrapper-manager) | Independent optional VM / wrapper service, built from a pinned upstream commit by the deployment recipe | GNU AGPL version 3. [License copy](licenses/wrapper-manager-AGPL-3.0.txt). |
| [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) | Optional external PO token provider and plugin | GNU GPL version 3. [License copy](licenses/bgutil-GPL-3.0.txt). |

The wrapper-manager source is fetched from upstream at build time and is not
vendored in this repository. It runs as an independent service with its own
license. Distributing a compiled wrapper-manager executable or a container
image that contains it requires compliance with its AGPL terms, including
providing the corresponding source and applicable build/install materials for
the exact distributed version. Preserve upstream copyright and license notices;
retain the pinned source commit and any local modifications. The default recipe
pins commit `d40d2046fead280f568c61a661cfdc10bd71c7dc`. If you modify it
and let users interact with that version over a network, also satisfy AGPL
section 13's source-offer requirements. The main repository's MIT license does
not replace these obligations.

Python requirements, operating-system packages, FFmpeg, optional media tools,
optional speech-alignment runtimes, model weights, browser images, and wrapper
services remain subject to the licenses distributed by their publishers.
Installing a component does not change its license. If publishing container
images or other binary bundles, retain the actual installed packages' notices
and fulfill their source-distribution requirements where applicable; this file
is not a complete license inventory for a built image.

The Linux image installs Debian's [Noto CJK](https://packages.debian.org/bookworm/fonts-noto-cjk)
and [Liberation](https://packages.debian.org/bookworm/fonts-liberation2) font
packages for the default subtitle styles. Their installed copyright/license
files are retained under `/usr/share/doc/`; no font binaries are vendored here.
ASS sidecars reference font names and do not embed those fonts. A separate
player or media-server renderer needs its own appropriate fonts.

AppleMusicDecrypt source and its `amdecrypt_once.py` integration are not included
in this repository. AppleMusicDecrypt has a separate
[AGPL-3.0 license](https://github.com/WorldObservationLog/AppleMusicDecrypt/blob/master/LICENSE.txt).
Device credentials, DRM device blobs, authentication cookies, private keys,
account sessions, music, videos, and downloaded lyrics are excluded.

## Optional LX / SQ backends

[LXServer](https://github.com/XCQ0607/lxserver), version 2.1.2 at commit
`9006e61d73f15764b8eb1388efbd55d8d3271370`, is an independent Apache-2.0
service. Its [license](services/lx-music/LICENSE.upstream.txt) and pinned
image/source metadata are retained in the service directory. Its sources are
not vendored, and personal music-source scripts are not included.

[SQ Music Plus](https://github.com/59799517/simple_sq_music_plus) is referenced
at version 3.1.33, commit `cf0f6ed136be0aa13845bbab028c27e404b89d5d`.
No license file or grant was found in the locally audited source snapshot.
This repository includes an original transformation recipe and build/config
instructions, without redistributing the upstream Java tree or binaries.
The repository's MIT license does not grant rights over that upstream work.
See [SQ sources and boundaries](services/sq-music/SOURCES.md).

MySQL and the NetEase API service images retain their publishers' licensing.
Image contents are not included in this repository. Review the exact selected
images and corresponding source notices before distributing a built bundle.
