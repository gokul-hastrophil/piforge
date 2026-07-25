#!/bin/bash
#
# Mass-flash a Raspberry Pi OS image to multiple SD cards in parallel using
# rpi-imager --cli, with Imager-style first-boot customization (hostname,
# user, password, Wi-Fi, country, SSH) injected via firstrun.sh.
#
# This is the simple/portable CLI path. For much faster parallel writes
# (decompress once, dd from page cache) use the web UI instead:
#   sudo python3 server.py
#
# Usage:
#   sudo ./flash-all.sh /dev/sda /dev/sdb /dev/sdc ...
#
# Config comes from config.json next to this script (gitignored — your real
# Wi-Fi/password) or config.example.json as a fallback. Copy the example and
# edit it before first use:
#   cp config.example.json config.json
#
# Each card gets hostname base + index (pi1, pi2, ...) unless
# number_hostnames is false in config.json, in which case all get the same
# hostname (fine for a single card; a network conflict for more than one).

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="${CONFIG_FILE:-$SELF_DIR/config.json}"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="$SELF_DIR/config.example.json"
[ -f "$CONFIG_FILE" ] || { echo "ERROR: no config.json or config.example.json found next to $0"; exit 1; }
command -v jq >/dev/null || { echo "ERROR: install jq (sudo apt install jq)"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 required"; exit 1; }
command -v rpi-imager >/dev/null || { echo "ERROR: rpi-imager not found (sudo apt install rpi-imager, or snap install rpi-imager)"; exit 1; }

IMAGE_URL="$(jq -r '.image_url' "$CONFIG_FILE")"
HOSTNAME_BASE="$(jq -r '.hostname // "raspberrypi"' "$CONFIG_FILE")"
NUMBER_HOSTNAMES="$(jq -r 'if .number_hostnames == false then 0 else 1 end' "$CONFIG_FILE")"
IMAGE_FILE="$HOME/rpi-images/$(echo -n "$IMAGE_URL" | md5sum | cut -d' ' -f1).img.xz"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ $# -ge 1 ] || { echo "Usage: sudo $0 /dev/sdX [/dev/sdY ...]"; exit 1; }

DEVICES=("$@")

# Safety: refuse non-removable or system disks
for dev in "${DEVICES[@]}"; do
    [ -b "$dev" ] || { echo "ERROR: $dev is not a block device"; exit 1; }
    name=$(basename "$dev")
    rm_flag=$(cat "/sys/block/$name/removable" 2>/dev/null || echo 0)
    if [ "$rm_flag" != "1" ]; then
        echo "ERROR: $dev is not removable. Refusing (system disk protection)."
        exit 1
    fi
    if lsblk -no MOUNTPOINT "$dev" | grep -qE '^(/|/boot|/boot/firmware|/home)$'; then
        echo "ERROR: $dev holds a system mount. Refusing."
        exit 1
    fi
done

echo "WARNING: ALL DATA on these devices will be PERMANENTLY DESTROYED:"
lsblk -d -o NAME,SIZE,MODEL,TRAN "${DEVICES[@]}"
read -rp "Type 'yes' to continue: " ans
[ "$ans" = "yes" ] || { echo "Aborted."; exit 1; }

# --- Download image once (resumable) ---
mkdir -p "$(dirname "$IMAGE_FILE")"
if [ ! -f "$IMAGE_FILE" ]; then
    echo ">> Downloading image..."
    curl -L -C - -o "$IMAGE_FILE.part" "$IMAGE_URL"
    mv "$IMAGE_FILE.part" "$IMAGE_FILE"
else
    echo ">> Using cached image: $IMAGE_FILE"
fi

# --- Flash one card: write + inject customization ---
flash_one() {  # $1 = device, $2 = index
    local dev="$1" idx="$2" hn part mnt
    if [ "$NUMBER_HOSTNAMES" = 1 ]; then hn="${HOSTNAME_BASE}${idx}"; else hn="$HOSTNAME_BASE"; fi
    local log="/tmp/flash-$(basename "$dev").log"

    echo "[$dev] writing (hostname: $hn)..."
    umount "${dev}"?* 2>/dev/null || true
    if ! rpi-imager --cli "$IMAGE_FILE" "$dev" >"$log" 2>&1; then
        echo "[$dev] FAILED — see $log"
        return 1
    fi

    partprobe "$dev" 2>/dev/null || true
    sleep 2
    part="${dev}1"; [ -b "${dev}p1" ] && part="${dev}p1"

    mnt=$(mktemp -d)
    mount "$part" "$mnt"
    python3 "$SELF_DIR/firstrun_gen.py" --config "$CONFIG_FILE" --hostname "$hn" > "$mnt/firstrun.sh"
    chmod +x "$mnt/firstrun.sh"
    sed -i 's| systemd.run.*||g' "$mnt/cmdline.txt"
    sed -i '1 s|$| systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target|' "$mnt/cmdline.txt"
    sync
    umount "$mnt"; rmdir "$mnt"

    echo "[$dev] DONE ($hn)"
}

# --- Run all cards in parallel ---
pids=(); i=1; fail=0
for dev in "${DEVICES[@]}"; do
    flash_one "$dev" "$i" &
    pids+=($!)
    i=$((i+1))
done
for p in "${pids[@]}"; do wait "$p" || fail=1; done

echo
if [ "$fail" = 0 ]; then
    echo "All ${#DEVICES[@]} cards flashed and configured. Safe to remove."
else
    echo "Some cards FAILED — check /tmp/flash-*.log"
fi
exit "$fail"
