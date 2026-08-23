#!/bin/bash
# System-wide installation. Two ways to run it:
#
#   sudo ./install.sh
#   curl -fsSL https://raw.githubusercontent.com/availov/wattson/main/install.sh | sudo bash
#
# Piped through curl the script has no sources next to it, so it fetches them
# into a temporary directory and runs itself from there.
set -euo pipefail

# override to install another branch or a local tarball while testing
REPO_TARBALL=${WATTSON_TARBALL:-https://codeload.github.com/availov/wattson/tar.gz/refs/heads/main}

if [ "$(id -u)" -ne 0 ]; then
    echo "root required: sudo $0" >&2
    exit 1
fi

SOURCE_DIR=$(dirname "$(readlink -f "$0")")

if [ ! -f "$SOURCE_DIR/build.sh" ] || [ ! -d "$SOURCE_DIR/data" ]; then
    command -v tar >/dev/null || { echo "tar is required" >&2; exit 1; }
    if command -v curl >/dev/null; then
        DOWNLOAD="curl -fsSL"
    elif command -v wget >/dev/null; then
        DOWNLOAD="wget -qO-"
    else
        echo "curl or wget is required" >&2
        exit 1
    fi

    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    echo "downloading the sources"
    $DOWNLOAD "$REPO_TARBALL" | tar -xz -C "$WORK" --strip-components=1
    bash "$WORK/install.sh"
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

cd "$SOURCE_DIR"

OLD_BIN=/usr/local/bin/asus-fan-control
OLD_CONFIG=/etc/asus-fan-control.json
NEW_CONFIG=/etc/wattson.json

# Build before touching anything in the system: if the build fails, the
# working installation has to stay untouched.
bash ./build.sh

# The project used to be called asus-fan-control. If it is still installed it
# has to go right here, otherwise its old timer keeps applying its own config
# every 30 seconds and fights the new one over the same sysfs files.
WAS_ENABLED=no

if [ -e "$OLD_BIN" ] || [ -e /etc/systemd/system/asus-fan-control.timer ]; then
    echo "found an installation under the old name asus-fan-control — migrating"

    if [ "$(systemctl is-enabled asus-fan-control.timer 2>/dev/null)" = "enabled" ]; then
        WAS_ENABLED=yes
    fi
    systemctl disable --now asus-fan-control.timer 2>/dev/null || true

    if [ -f "$OLD_CONFIG" ] && [ ! -f "$NEW_CONFIG" ]; then
        install -m644 "$OLD_CONFIG" "$NEW_CONFIG"
        echo "  settings migrated: $OLD_CONFIG -> $NEW_CONFIG"
    fi

    rm -f "$OLD_BIN" \
          /etc/systemd/system/asus-fan-control.service \
          /etc/systemd/system/asus-fan-control.timer \
          /usr/share/polkit-1/actions/com.yura.asusfancontrol.policy \
          /usr/share/applications/asus-fan-control.desktop \
          "$OLD_CONFIG"
    echo "  the old version has been removed"
    # curve and charge threshold are left alone: the new timer picks them up
fi

install -m755 dist/wattson                     /usr/local/bin/wattson
install -m644 data/wattson.service             /etc/systemd/system/
install -m644 data/wattson.timer               /etc/systemd/system/
install -m644 data/com.yura.wattson.policy     /usr/share/polkit-1/actions/
install -m644 data/wattson.desktop             /usr/share/applications/

systemctl daemon-reload
/usr/local/bin/wattson config init

# autostart was on in the old version — do not lose it silently while migrating
if [ "$WAS_ENABLED" = yes ]; then
    systemctl enable --now wattson.timer
    echo "autostart migrated and switched on"
fi

python3 -c 'import gi' 2>/dev/null || echo "note: python3-gi with GTK 4 is missing — the CLI works, the GUI does not"

echo
echo "installed:"
echo "  /usr/local/bin/wattson               command and GUI"
echo "  /etc/wattson.json                    settings"
echo "  wattson.timer                        autostart"
echo
echo "start the GUI:         wattson"
echo "current state:         wattson status"
echo "what eats the battery: wattson power"
echo "switch autostart on:   wattson autostart on"
