#!/usr/bin/env bash
# Host-side operator entry point; this never starts a second bot or timer.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
exec python3 "$ROOT/services/youtube/youtube_auth.py" "$@"
