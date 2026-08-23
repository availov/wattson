#!/bin/bash
# Full removal: restores the factory curve and lifts the charge threshold.
# Run through sudo.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root required: sudo $0" >&2
    exit 1
fi

systemctl disable --now wattson.timer 2>/dev/null || true

if [ -x /usr/local/bin/wattson ]; then
    /usr/local/bin/wattson reset || true
    # otherwise the battery would keep its limit forever once the program is gone
    /usr/local/bin/wattson battery off || true
fi

rm -f /usr/local/bin/wattson \
      /etc/systemd/system/wattson.service \
      /etc/systemd/system/wattson.timer \
      /usr/share/polkit-1/actions/com.yura.wattson.policy \
      /usr/share/applications/wattson.desktop \
      /etc/wattson.json

systemctl daemon-reload
echo "removed: the fan curve is back to factory, charging goes up to 100 % again"
