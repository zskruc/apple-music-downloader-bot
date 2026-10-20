#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/apple-wrapper.env
cd /opt/apple-wrapper
args=(--host=0.0.0.0 --port=8080 --mirror="${WRAPPER_MANAGER_MIRROR}" --debug=false)
if [[ -n "${WRAPPER_MANAGER_PROXY}" ]]; then args+=(--proxy="${WRAPPER_MANAGER_PROXY}"); fi
exec ./wrapper-manager "${args[@]}"
