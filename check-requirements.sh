#!/bin/bash
#
# Verify the host has everything PiForge needs. Run this once after
# cloning, or whenever something behaves unexpectedly.
#
set -uo pipefail

ok=1
need() {
    if command -v "$1" >/dev/null 2>&1; then
        echo "  OK   $1"
    else
        echo "  MISS $1  ($2)"
        ok=0
    fi
}

echo "Checking required tools..."
need python3     "sudo apt install python3"
need lsblk        "sudo apt install util-linux"
need dd           "sudo apt install coreutils"
need xz           "sudo apt install xz-utils"
need openssl      "sudo apt install openssl"
need wpa_passphrase "sudo apt install wpasupplicant"
need partprobe    "sudo apt install parted"
need jq           "sudo apt install jq"
need curl         "sudo apt install curl"

echo
echo "Optional (only needed for flash-all.sh, the CLI-only path):"
if command -v rpi-imager >/dev/null 2>&1; then
    echo "  OK   rpi-imager"
else
    echo "  MISS rpi-imager  (sudo apt install rpi-imager, or: sudo snap install rpi-imager)"
fi

echo
if [ "$(uname -s)" != "Linux" ]; then
    echo "WARNING: this tool relies on Linux-specific interfaces (lsblk, /sys/block,"
    echo "dd oflag=direct, partprobe). It will not work on macOS or Windows as-is."
    ok=0
fi

echo
if [ ! -f "$(dirname "$0")/config.json" ]; then
    echo "NOTE: config.json not found — copy config.example.json to config.json"
    echo "      and set your real hostname/user/password/Wi-Fi before flashing."
fi

echo
if [ "$ok" = 1 ]; then
    echo "All required tools present."
else
    echo "Some required tools are missing — install them, then re-run this script."
    exit 1
fi
