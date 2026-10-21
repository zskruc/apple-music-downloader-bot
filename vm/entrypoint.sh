#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
DATA_DIR=${DATA_DIR:-/data}
IMAGE_URL=${UBUNTU_IMAGE_URL:-https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img}
IMAGE_SHA256=${UBUNTU_IMAGE_SHA256:-04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94}
VM_MEMORY=${VM_MEMORY:-4096}
VM_CPUS=${VM_CPUS:-2}
VM_DISK_SIZE=${VM_DISK_SIZE:-32G}
WRAPPER_SINGLE_ENABLE=${WRAPPER_SINGLE_ENABLE:-false}
WRAPPER_DEVICE_INFO=${WRAPPER_DEVICE_INFO:-}
WRAPPER_MANAGER_PROXY=${WRAPPER_MANAGER_PROXY:-}
WRAPPER_MANAGER_MIRROR=${WRAPPER_MANAGER_MIRROR:-false}
[[ -c /dev/kvm ]] || { echo '/dev/kvm is unavailable; an x86-64 Linux KVM host is required.' >&2; exit 1; }
[[ "${VM_MEMORY}" =~ ^[0-9]+$ && "${VM_CPUS}" =~ ^[0-9]+$ && "${VM_DISK_SIZE}" =~ ^[0-9]+[GM]$ ]] || { echo 'Invalid VM sizing.' >&2; exit 1; }
[[ "${WRAPPER_SINGLE_ENABLE}" == true || "${WRAPPER_SINGLE_ENABLE}" == false ]] || exit 1
[[ "${WRAPPER_MANAGER_MIRROR}" == true || "${WRAPPER_MANAGER_MIRROR}" == false ]] || exit 1
mkdir -p "${DATA_DIR}"
base="${DATA_DIR}/ubuntu-cloudimg-amd64.img"
disk="${DATA_DIR}/apple-wrapper-vm.qcow2"
key="${DATA_DIR}/id_ed25519"
if [[ ! -f "${key}" ]]; then ssh-keygen -q -t ed25519 -N '' -f "${key}"; fi
chmod 0600 "${key}"
if [[ ! -f "${base}" ]]; then
  curl --fail --location --retry 5 --output "${base}.tmp" "${IMAGE_URL}"
  [[ "${IMAGE_SHA256}" =~ ^[[:xdigit:]]{64}$ ]] || { echo 'Invalid image SHA-256.' >&2; exit 1; }
  printf '%s  %s\n' "${IMAGE_SHA256}" "${base}.tmp" | sha256sum -c -
  mv "${base}.tmp" "${base}"
fi
# A resumed install must also reject a corrupted or replaced cached base.
[[ "${IMAGE_SHA256}" =~ ^[[:xdigit:]]{64}$ ]] || { echo 'Invalid image SHA-256.' >&2; exit 1; }
printf '%s  %s\n' "${IMAGE_SHA256}" "${base}" | sha256sum -c -
if [[ ! -f "${disk}" ]]; then qemu-img create -f qcow2 -F qcow2 -b "${base}" "${disk}" "${VM_DISK_SIZE}"; fi
cat >"${DATA_DIR}/meta-data" <<'EOF'
instance-id: apple-music-wrapper-vm
local-hostname: apple-music-wrapper-vm
EOF
cat >"${DATA_DIR}/user-data" <<EOF
#cloud-config
users:
  - name: vmuser
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - $(cat "${key}.pub")
ssh_pwauth: false
disable_root: true
write_files:
  - path: /etc/sysctl.d/99-wrapper-userns.conf
    content: |
      kernel.unprivileged_userns_clone = 1
runcmd:
  - [sh, -lc, 'sysctl --system']
  - [systemctl, enable, --now, ssh]
EOF
genisoimage -quiet -output "${DATA_DIR}/seed.iso" -volid cidata -joliet -rock "${DATA_DIR}/user-data" "${DATA_DIR}/meta-data"
qemu-system-x86_64 -enable-kvm -cpu host -smp "${VM_CPUS}" -m "${VM_MEMORY}" \
  -drive "file=${disk},if=virtio,format=qcow2,cache=writeback" \
  -drive "file=${DATA_DIR}/seed.iso,if=virtio,format=raw,readonly=on" \
  -device virtio-net-pci,netdev=net0 \
  -netdev 'user,id=net0,hostfwd=tcp::2222-:22,hostfwd=tcp::18080-:8080,hostfwd=tcp::10020-:10020,hostfwd=tcp::20020-:20020,hostfwd=tcp::30020-:30020' \
  -nographic -display none &
qemu_pid=$!
cleanup() {
  if [[ "${ready:-false}" == true ]]; then
    ssh "${ssh_args[@]}" vmuser@127.0.0.1 sudo poweroff >/dev/null 2>&1 || true
    for ((attempt=0; attempt<20; attempt++)); do
      kill -0 "${qemu_pid}" 2>/dev/null || break
      sleep 1
    done
  fi
  kill "${qemu_pid}" 2>/dev/null || true
  wait "${qemu_pid}" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
ssh_args=(-i "${key}" -p 2222 -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${DATA_DIR}/known_hosts" -o ConnectTimeout=3)
ready=false
for ((attempt=0; attempt<180; attempt++)); do
  kill -0 "${qemu_pid}" || exit 1
  if ssh "${ssh_args[@]}" vmuser@127.0.0.1 true >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
[[ "${ready}" == true ]] || { echo 'Guest SSH did not become ready.' >&2; exit 1; }
env_file="${DATA_DIR}/apple-wrapper.env"
for name in WRAPPER_SINGLE_ENABLE WRAPPER_DEVICE_INFO WRAPPER_MANAGER_PROXY WRAPPER_MANAGER_MIRROR; do
  printf '%s=%q\n' "${name}" "${!name}"
done >"${env_file}"
scp -i "${key}" -P 2222 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=${DATA_DIR}/known_hosts" \
  /opt/payload/* "${env_file}" vmuser@127.0.0.1:/tmp/ >/dev/null
ssh "${ssh_args[@]}" vmuser@127.0.0.1 sudo bash /tmp/provision.sh
rm -f "${env_file}"
echo 'Guest provisioned. Account login and transport readiness must be verified separately.'
wait "${qemu_pid}"
