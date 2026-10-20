#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/apple-wrapper.env
[[ -n "${WRAPPER_DEVICE_INFO}" ]] || { echo 'Set your own WRAPPER_DEVICE_INFO before single mode.' >&2; exit 1; }
cd /opt/apple-wrapper/data/wrapper
install -d rootfs/dev
for specification in 'null 1 3' 'zero 1 5' 'random 1 8' 'urandom 1 9'; do
  read -r node major minor <<<"${specification}"
  if [[ ! -c "rootfs/dev/${node}" ]]; then
    rm -f "rootfs/dev/${node}"
    mknod "rootfs/dev/${node}" c "${major}" "${minor}"
  fi
  chmod 0666 "rootfs/dev/${node}"
done
shopt -s nullglob
instances=(rootfs/data/instances/*/)
[[ ${#instances[@]} -eq 1 ]] || { echo 'Single mode requires exactly one persisted account instance.' >&2; exit 1; }
instance_id=$(basename "${instances[0]}")
exec ./wrapper -H0.0.0.0 -B"/data/instances/${instance_id}" -D10020 -M20020 -I"${WRAPPER_DEVICE_INFO}"
