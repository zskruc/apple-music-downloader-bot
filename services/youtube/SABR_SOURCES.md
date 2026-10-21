# Optional SABR tool: sources and distribution boundaries

The optional component is the public `bashonly/yt-dlp` SABR prerelease. It is
separate from this repository's normal `yt-dlp` dependency and is disabled by
default. Its upstream license is retained; this repository's MIT license does
not relicense it.

| Item | Reviewed value |
| --- | --- |
| Upstream repository | [bashonly/yt-dlp](https://github.com/bashonly/yt-dlp) |
| Immutable source commit | [`6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6`](https://github.com/bashonly/yt-dlp/commit/6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6) |
| Published version | `2026.08.19.233452` |
| Origin / channel | `bashonly/yt-dlp` / `bashonly/yt-dlp@sabr` |
| Published release | [sabr 2026.08.19.233452](https://github.com/bashonly/yt-dlp/releases/tag/sabr) |
| Asset | [`yt-dlp`, platform-independent Unix zipimport executable](https://github.com/bashonly/yt-dlp/releases/download/sabr/yt-dlp) |
| SHA-256 | `8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d` |
| Bytes | `3146671` |
| Installation path | `/app/vendor/yt-dlp-sabr/yt-dlp` |

The release's [asset metadata](https://github.com/bashonly/yt-dlp/releases/expanded_assets/sabr)
publishes this SHA-256; the reviewed running deployment had the same digest,
version, origin/channel, and release commit. This establishes a byte match to
that published artifact. It is not a claim of a reproducible build or an
official stable yt-dlp release.

The `sabr` release tag and download URL are mutable. The installer pins the
reviewed digest and byte count independently of that tag. A moved tag, replaced
asset, truncated transfer, or changed content makes installation fail. It does
not accept a new digest from an environment variable or command-line flag.
Review the corresponding source and licenses before deliberately updating the
pin. The upstream [version generator](https://github.com/bashonly/yt-dlp/blob/6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6/devscripts/update-version.py)
and [build workflow](https://github.com/bashonly/yt-dlp/blob/6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6/.github/workflows/build.yml)
explain why generated release metadata differs from the checked-in `version.py`.

## Build and runtime selection

`INSTALL_YT_SABR=false` is the Docker build default and makes no SABR asset
request. To create a separate reviewed image:

```sh
docker build --build-arg INSTALL_YT_SABR=true -t local/apple-music-downloader-bot:sabr-reviewed .
```

`install_sabr_tool.py` uses only Python's standard library. It downloads to a
mode-0600, non-executable candidate in the destination directory, checks the
fixed byte count and SHA-256, then atomically publishes the mode-0755 zipapp.
Failure removes the candidate and preserves an existing destination. The
binary is fetched during an explicitly enabled image build, not vendored in
this source repository. The installer never executes it as part of validation.

Runtime selection is a separate opt-in: `YT_SABR_FALLBACK_ENABLED=true`, with
`YT_SABR_TOOL_PATH=/app/vendor/yt-dlp-sabr/yt-dlp`. Follow the
[English guide](../../docs/YOUTUBE.en.md) / [中文指南](../../docs/YOUTUBE.zh-CN.md)
for the remaining settings, image upgrade/rollback, cookies and functional
validation. Enabling a build flag alone does not activate SABR downloads.

## License scope

The exact commit's [LICENSE](https://github.com/bashonly/yt-dlp/blob/6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6/LICENSE)
is the Unlicense; its source and binary forms may be redistributed. The
release's [licensing statement](https://github.com/bashonly/yt-dlp/blob/6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6/README.md#licensing)
also identifies Meriyah (ISC) and Astring (MIT) in the Unix zipimport executable.
Retain their copyright and permission notices when redistributing that asset.
The release's separately packaged PyInstaller executables include further
dependencies and have different combined license requirements; they are not
the pinned asset installed here.

Unmodified copies from the exact source commit are retained at:

- [`licenses/yt-dlp-sabr-Unlicense.txt`](../../licenses/yt-dlp-sabr-Unlicense.txt)
- [`licenses/yt-dlp-sabr-THIRD_PARTY_LICENSES.txt`](../../licenses/yt-dlp-sabr-THIRD_PARTY_LICENSES.txt)

The third-party aggregation covers multiple upstream build variants; it does
not mean every listed component is embedded in this Unix zipapp. Both texts
and this provenance document are included in the image under
`/usr/share/doc/yt-dlp-sabr/`. Other installed packages and runtime dependencies
retain their own license obligations. No cookies, browser profile, account
permissions, private routes, or media are included with this component.
