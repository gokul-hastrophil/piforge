#!/bin/bash
#
# One-command install: builds the .deb if needed, then installs it through
# apt so every dependency (python3-gi, webkit2gtk, jq, wpasupplicant, ...)
# resolves and installs automatically — the same as `apt install` for any
# other package. This matters because `dpkg -i` alone does NOT do this: it
# only records unmet dependencies and leaves the package half-configured,
# which is the most common reason a manually-installed .deb "doesn't
# install its dependencies". Always prefer apt over raw dpkg for this
# reason; this script does it the right way and falls back safely if apt
# itself isn't available for some reason.
#
# Usage:  ./packaging/install.sh
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SELF_DIR}/.." && pwd)"
VERSION="$(cat "${REPO_DIR}/VERSION")"
DEB="${SELF_DIR}/dist/piforge_${VERSION}_all.deb"

if [ ! -f "$DEB" ]; then
    echo ">> Building piforge_${VERSION}_all.deb..."
    "${SELF_DIR}/build-deb.sh"
fi

if ! command -v apt >/dev/null 2>&1; then
    echo "ERROR: apt not found — this installer targets Debian/Ubuntu/Raspberry Pi OS." >&2
    echo "See README.md for a from-source install on other distros." >&2
    exit 1
fi

echo ">> Refreshing package lists..."
sudo apt-get update -qq

echo ">> Installing PiForge (apt resolves and installs every dependency automatically)..."
if sudo apt install -y "$DEB"; then
    :
else
    echo ">> apt install hit a snag — falling back to dpkg + dependency repair..."
    sudo dpkg -i "$DEB" || true
    sudo apt-get install -f -y
fi

echo
echo "Done. Find PiForge in your application menu, or run:  piforge"
