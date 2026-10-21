#!/usr/bin/env python3
"""Install one reviewed SABR zipapp; production URL and digest are not overridable."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

ASSET_URL = "https://github.com/bashonly/yt-dlp/releases/download/sabr/yt-dlp"
SOURCE_COMMIT = "6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6"
VERSION = "2026.08.19.233452"
EXPECTED_SHA256 = "8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d"
EXPECTED_BYTES = 3_146_671
DEFAULT_DESTINATION = Path("/app/vendor/yt-dlp-sabr/yt-dlp")
DOWNLOAD_TIMEOUT_SECONDS = 60


class InstallError(RuntimeError):
    pass


def install_tool(destination: Path, *, opener: Callable | None = None) -> None:
    """Publish only a complete matching asset; leave an existing tool intact."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    open_url = urllib.request.urlopen if opener is None else opener
    request = urllib.request.Request(ASSET_URL, headers={"User-Agent": "amdl-reviewed-sabr-installer"})
    with tempfile.NamedTemporaryFile(
        prefix=".yt-dlp-download-", dir=destination.parent, delete=False,
    ) as output:
        candidate = Path(output.name)
        try:
            digest = hashlib.sha256()
            received = 0
            with open_url(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise InstallError("SABR download redirected outside HTTPS; tool was not installed.")
                while chunk := response.read(256 * 1024):
                    received += len(chunk)
                    if received > EXPECTED_BYTES:
                        raise InstallError("SABR asset size differs from the reviewed release; tool was not installed.")
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            if received != EXPECTED_BYTES or digest.hexdigest() != EXPECTED_SHA256:
                raise InstallError("SABR asset SHA-256/size verification failed; tool was not installed.")
            # The candidate is private and non-executable until all checks pass.
            candidate.chmod(0o755)
            os.replace(candidate, destination)
        finally:
            candidate.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    args = parser.parse_args()
    try:
        install_tool(args.destination)
    except (OSError, urllib.error.URLError, InstallError):
        parser.exit(1, "SABR installation failed; the reviewed tool was not installed.\n")
    print(f"Installed reviewed SABR {VERSION}; SHA-256 {EXPECTED_SHA256}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
