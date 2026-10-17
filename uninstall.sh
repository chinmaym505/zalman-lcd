#!/usr/bin/env bash
# Remove the service, udev rule and installed driver. Leaves the apt packages installed.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi

systemctl disable --now zalman-lcd 2>/dev/null || true   # stopping hands the panel back to its own screen
rm -f /etc/systemd/system/zalman-lcd.service
systemctl daemon-reload

rm -f /etc/udev/rules.d/99-zalman-lcd.rules
udevadm control --reload

rm -f /usr/local/bin/zalman-lcd
rm -rf /opt/zalman-lcd
echo "removed"
