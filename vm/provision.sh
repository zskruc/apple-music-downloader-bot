#!/usr/bin/env bash
# Executed inside the disposable Ubuntu guest, never on the NAS host.
set -Eeuo pipefail
install -d -m 0755 /opt/apple-wrapper
install -m 0755 /tmp/wrapper-manager /tmp/account-bridge.py /tmp/start-manager.sh /tmp/start-single.sh /opt/apple-wrapper/
install -m 0600 /tmp/apple-wrapper.env /etc/apple-wrapper.env
rm -f /tmp/apple-wrapper.env
source /etc/apple-wrapper.env
cat >/etc/systemd/system/apple-wrapper-manager.service <<'EOF'
[Unit]
Description=External wrapper-manager
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=/opt/apple-wrapper
ExecStart=/opt/apple-wrapper/start-manager.sh
Restart=on-failure
RestartSec=10
Environment=ANDROID_ROOT=/system ANDROID_DATA=/data ANDROID_STORAGE=/sdcard TMPDIR=/tmp
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/apple-wrapper-single.service <<'EOF'
[Unit]
Description=External fixed-port Apple Music wrapper
After=network-online.target
Wants=network-online.target
Conflicts=apple-wrapper-manager.service
[Service]
WorkingDirectory=/opt/apple-wrapper
ExecStart=/opt/apple-wrapper/start-single.sh
Restart=on-failure
RestartSec=10
Environment=ANDROID_ROOT=/system ANDROID_DATA=/data ANDROID_STORAGE=/sdcard TMPDIR=/tmp
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/apple-wrapper-account-bridge.service <<'EOF'
[Unit]
Description=Private account bridge for wrapper protocol adapter
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/apple-wrapper/account-bridge.py
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
if [[ "${WRAPPER_SINGLE_ENABLE}" == true ]]; then
  [[ -n "${WRAPPER_DEVICE_INFO}" ]] || { echo 'WRAPPER_DEVICE_INFO is required.' >&2; exit 1; }
  [[ -d /opt/apple-wrapper/data/wrapper/rootfs/data/instances ]] || { echo 'Log in through manager mode first.' >&2; exit 1; }
  systemctl disable --now apple-wrapper-manager.service || true
  # Only the guest's wrapper processes are stopped. The host is untouched.
  pkill -x wrapper || true
  systemctl enable --now apple-wrapper-account-bridge.service apple-wrapper-single.service
else
  systemctl disable --now apple-wrapper-single.service apple-wrapper-account-bridge.service || true
  systemctl enable --now apple-wrapper-manager.service
fi
