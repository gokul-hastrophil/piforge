#!/bin/bash
#
# Inject Raspberry Pi OS first-boot customization (hostname, user, password,
# Wi-Fi, country, timezone, SSH) into an already-written SD card.
#
# Run this as the LAST step, after any repartitioning/provisioning tools,
# since those typically rewrite the boot partition and would destroy this
# configuration if it ran before them.
#
# Usage:
#   sudo ./inject-config.sh /dev/sda        [hostname]   # device: finds+mounts boot partition
#   ./inject-config.sh /media/you/bootfs    [hostname]   # already-mounted boot partition
#
# Config comes from config.json next to this script (gitignored — your real
# Wi-Fi/password) or config.example.json as a fallback. Copy the example and
# edit it before first use:
#   cp config.example.json config.json
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="${CONFIG_FILE:-$SELF_DIR/config.json}"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="$SELF_DIR/config.example.json"
[ -f "$CONFIG_FILE" ] || { echo "ERROR: no config.json or config.example.json found next to $0"; exit 1; }
command -v jq >/dev/null || { echo "ERROR: install jq (sudo apt install jq)"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 required"; exit 1; }

HOSTNAME_DEFAULT="$(jq -r '.hostname // "raspberrypi"' "$CONFIG_FILE")"
PI_USER="$(jq -r '.user // "pi"' "$CONFIG_FILE")"
WIFI_SSID="$(jq -r '.wifi_ssid // ""' "$CONFIG_FILE")"
TIMEZONE="$(jq -r '.timezone // "UTC"' "$CONFIG_FILE")"
ENABLE_SSH="$(jq -r 'if .enable_ssh == false then "no" else "yes" end' "$CONFIG_FILE")"

TARGET="${1:-}"
HOSTNAME="${2:-$HOSTNAME_DEFAULT}"
[ -n "$TARGET" ] || { echo "Usage: $0 <device|boot-mount-dir> [hostname]"; exit 1; }

MOUNTED_BY_US=0
if [ -d "$TARGET" ]; then
    BOOT="$TARGET"
elif [ -b "$TARGET" ]; then
    part="${TARGET}1"; [ -b "${TARGET}p1" ] && part="${TARGET}p1"
    [ -b "$part" ] || { echo "ERROR: no partition 1 on $TARGET"; exit 1; }
    BOOT=$(findmnt -no TARGET "$part" | head -1 || true)
    if [ -z "$BOOT" ]; then
        [ "$(id -u)" -eq 0 ] || { echo "ERROR: $part not mounted; run with sudo so I can mount it."; exit 1; }
        BOOT=$(mktemp -d /tmp/bootfs-XXXX)
        mount "$part" "$BOOT"
        MOUNTED_BY_US=1
    fi
else
    echo "ERROR: $TARGET is neither a block device nor a directory"; exit 1
fi

cleanup() {
    if [ "$MOUNTED_BY_US" = 1 ]; then
        umount "$BOOT" 2>/dev/null || true
        rmdir "$BOOT" 2>/dev/null || true
    fi
}
trap cleanup EXIT

[ -f "$BOOT/cmdline.txt" ] || { echo "ERROR: $BOOT/cmdline.txt not found — not a Pi boot partition?"; exit 1; }

python3 "$SELF_DIR/firstrun_gen.py" --config "$CONFIG_FILE" --hostname "$HOSTNAME" \
    > "$BOOT/firstrun.sh"
chmod +x "$BOOT/firstrun.sh" 2>/dev/null || true

# Patch cmdline.txt (single line; strip any previous systemd.run first)
sed -i 's| systemd.run[^ ]*||g; s| systemd.run_success_action[^ ]*||g; s| systemd.unit=kernel-command-line.target||g' "$BOOT/cmdline.txt"
sed -i '1 s|$| systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target|' "$BOOT/cmdline.txt"

sync

# Verify
[ -s "$BOOT/firstrun.sh" ] || { echo "VERIFY FAILED: firstrun.sh missing"; exit 1; }
grep -q 'systemd.run=/boot/firstrun.sh' "$BOOT/cmdline.txt" || { echo "VERIFY FAILED: cmdline.txt not patched"; exit 1; }
bash -n "$BOOT/firstrun.sh" || { echo "VERIFY FAILED: firstrun.sh syntax error"; exit 1; }

echo "OK: injected config (hostname=$HOSTNAME, user=$PI_USER, wifi=${WIFI_SSID:-<none>}, tz=$TIMEZONE, ssh=$ENABLE_SSH)"
echo "First boot will apply settings and reboot once."
