#!/usr/bin/env bash
# Install the Zalman P40 DS LCD driver + dashboard service.
# Usage: sudo ./install.sh [user]      (user defaults to the account that ran sudo)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo $0 [user]" >&2
    exit 1
fi

RUN_USER="${1:-${SUDO_USER:-}}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    echo "pass the account the dashboard should run as: sudo $0 <user>" >&2
    exit 1
fi
id "$RUN_USER" >/dev/null

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> installing dependencies"
apt-get install -y python3-pil python3-psutil fonts-dejavu-core

echo "==> installing driver to /opt/zalman-lcd"
install -d /opt/zalman-lcd
install -m 755 "$HERE/zalman_lcd.py" /opt/zalman-lcd/zalman_lcd.py
ln -sf /opt/zalman-lcd/zalman_lcd.py /usr/local/bin/zalman-lcd

echo "==> udev rule (/dev/zalman-lcd, group plugdev)"
install -m 644 "$HERE/udev/99-zalman-lcd.rules" /etc/udev/rules.d/99-zalman-lcd.rules
udevadm control --reload
udevadm trigger --subsystem-match=hidraw
usermod -aG plugdev "$RUN_USER"

echo "==> systemd service (runs as $RUN_USER)"
sed "s/@USER@/$RUN_USER/" "$HERE/systemd/zalman-lcd.service" > /etc/systemd/system/zalman-lcd.service
chmod 644 /etc/systemd/system/zalman-lcd.service
systemctl daemon-reload
systemctl enable zalman-lcd
systemctl restart zalman-lcd

sleep 3
systemctl --no-pager --lines=8 status zalman-lcd || true
echo
echo "done. logs: journalctl -u zalman-lcd -f"
