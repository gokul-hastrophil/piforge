#!/usr/bin/env python3
"""
Shared first-boot script generator — the one place that knows how to build
an Imager-style firstrun.sh (hostname, user, password, Wi-Fi, country,
timezone, SSH). Used directly by server.py, and via the CLI below by
inject-config.sh / flash-all.sh, so there is exactly one implementation to
trust instead of three copies drifting apart.

CLI usage:
    python3 firstrun_gen.py --config config.json --hostname pi1
    python3 firstrun_gen.py --config config.json --hostname pi1 --json '{"password":"x", ...}'

Prints the rendered firstrun.sh to stdout. Config keys (see config.example.json):
  user, password, wifi_ssid, wifi_password, wifi_country, timezone, keymap,
  enable_ssh. wifi_ssid may be empty/absent to skip Wi-Fi setup entirely
  (e.g. Ethernet-only boards).
"""

import argparse
import json
import re
import subprocess
import sys

FIRSTRUN_TEMPLATE = r"""#!/bin/bash
set +e
CURRENT_HOSTNAME=$(cat /etc/hostname | tr -d " \t\n\r")
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_hostname '@@HOSTNAME@@'
else
   echo '@@HOSTNAME@@' >/etc/hostname
   sed -i "s/127.0.1.1.*$CURRENT_HOSTNAME/127.0.1.1\t@@HOSTNAME@@/g" /etc/hosts
fi
FIRSTUSER=$(getent passwd 1000 | cut -d: -f1)
FIRSTUSERHOME=$(getent passwd 1000 | cut -d: -f6)
@@SSH_BLOCK@@
if [ -f /usr/lib/userconf-pi/userconf ]; then
   /usr/lib/userconf-pi/userconf '@@USER@@' '@@HASH@@'
else
   echo "$FIRSTUSER:"'@@HASH@@' | chpasswd -e
   if [ "$FIRSTUSER" != "@@USER@@" ]; then
      usermod -l '@@USER@@' "$FIRSTUSER"
      usermod -m -d "/home/@@USER@@" '@@USER@@'
      groupmod -n '@@USER@@' "$FIRSTUSER"
      if grep -q "^autologin-user=" /etc/lightdm/lightdm.conf ; then
         sed /etc/lightdm/lightdm.conf -i -e "s/^autologin-user=.*/autologin-user=@@USER@@/"
      fi
      if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
         sed /etc/systemd/system/getty@tty1.service.d/autologin.conf -i -e "s/$FIRSTUSER/@@USER@@/"
      fi
      if [ -f /etc/sudoers.d/010_pi-nopasswd ]; then
         sed -i "s/^$FIRSTUSER /@@USER@@ /" /etc/sudoers.d/010_pi-nopasswd
      fi
   fi
fi
@@WIFI_BLOCK@@
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_keymap '@@KEYMAP@@'
   /usr/lib/raspberrypi-sys-mods/imager_custom set_timezone '@@TZ@@'
else
   rm -f /etc/localtime
   echo "@@TZ@@" >/etc/timezone
   dpkg-reconfigure -f noninteractive tzdata
fi
rm -f /boot/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/cmdline.txt
exit 0
"""

SSH_BLOCK = """if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom enable_ssh
else
   systemctl enable ssh
fi"""

WIFI_BLOCK_TEMPLATE = """if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_wlan '@@SSID@@' '@@PSK@@' '@@COUNTRY@@'
else
cat >/etc/wpa_supplicant/wpa_supplicant.conf <<'WPAEOF'
country=@@COUNTRY@@
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
ap_scan=1
update_config=1
network={
	ssid="@@SSID@@"
	psk=@@PSK@@
}
WPAEOF
   chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
   rfkill unblock wifi
   for filename in /var/lib/systemd/rfkill/*:wlan ; do
       echo 0 > $filename
   done
fi"""


def sh_hash_password(password):
    return subprocess.run(["openssl", "passwd", "-6", password],
                          capture_output=True, text=True, check=True).stdout.strip()


def wifi_psk(ssid, password):
    out = subprocess.run(["wpa_passphrase", ssid, password],
                         capture_output=True, text=True).stdout
    m = re.search(r"^\s*psk=([0-9a-f]{64})$", out, re.M)
    return m.group(1) if m else password


def make_firstrun(cfg, hostname):
    """cfg: dict with user, password, wifi_ssid, wifi_password, wifi_country,
    timezone, keymap, enable_ssh. Hashes/PSK are computed here if not already
    present as _hash/_psk (server.py precomputes them once for many cards)."""
    ssh_block = SSH_BLOCK if cfg.get("enable_ssh", True) else ""

    wifi_block = ""
    if cfg.get("wifi_ssid"):
        psk = cfg.get("_psk") or wifi_psk(cfg["wifi_ssid"], cfg.get("wifi_password", ""))
        wifi_block = (WIFI_BLOCK_TEMPLATE
                      .replace("@@SSID@@", cfg["wifi_ssid"])
                      .replace("@@PSK@@", psk)
                      .replace("@@COUNTRY@@", cfg.get("wifi_country") or "US"))

    password_hash = cfg.get("_hash") or sh_hash_password(cfg["password"])

    s = FIRSTRUN_TEMPLATE
    s = s.replace("@@SSH_BLOCK@@", ssh_block)
    s = s.replace("@@WIFI_BLOCK@@", wifi_block)
    for token, val in (
        ("@@HOSTNAME@@", hostname),
        ("@@USER@@", cfg["user"]),
        ("@@HASH@@", password_hash),
        ("@@TZ@@", cfg.get("timezone") or "UTC"),
        ("@@KEYMAP@@", cfg.get("keymap") or "us"),
    ):
        s = s.replace(token, val)
    return s


def _main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="path to config.json")
    ap.add_argument("--hostname", required=True)
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    if not cfg.get("password"):
        print("ERROR: config has no password set", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(make_firstrun(cfg, args.hostname))


if __name__ == "__main__":
    _main()
