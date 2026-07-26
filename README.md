# PiForge

Flash Raspberry Pi OS to many SD cards at once — in parallel, with hostname,
user, password, Wi-Fi, country, timezone, keyboard layout, SSH keys and even
per-card static IPs pre-configured, so every card boots straight to a working
login. No setup wizard, no per-card manual typing.

Built for anyone provisioning more than one Raspberry Pi at a time: classroom
kits, workshops, IoT fleets, cluster builds.

## Install as a system app (.deb)

On Debian, Ubuntu, or Raspberry Pi OS itself, install PiForge like any other
desktop app — the same way Raspberry Pi Imager ships its own `.deb`:

```bash
./packaging/build-deb.sh
sudo apt install ./packaging/dist/piforge_1.0.0_all.deb
```

That installs a **PiForge** entry in your application menu. Click it and
it's ready to use — no terminal, no manual `sudo python3 server.py`:

- The app itself (`/opt/piforge`) is read-only and shared by every user on
  the machine.
- Each user's own settings live in their own `~/.config/piforge/` and
  `~/rpi-images/` — never mixed with another user's, and never inside
  `/opt/piforge`.
- Flashing needs root (raw block-device writes), so the launcher opens a
  native graphical password prompt (`pkexec`) the first time — no terminal
  needed. The server then keeps running quietly in the background
  (bound to `127.0.0.1` only) so relaunching PiForge later reuses it
  instantly instead of prompting again.
- It opens in its own app-style window (no address bar/tabs) if you have
  Chromium, Chrome, or Brave installed; otherwise your default browser.

Uninstall with `sudo apt remove piforge`. To rebuild after making changes,
just rerun `./packaging/build-deb.sh` — it regenerates the package fresh
from whatever's currently in the repo (bump the version first: edit
`VERSION`).

Prefer the manual workflow, or you're on a distro `apt` doesn't cover? See
**Web UI** below — the packaged app and running `server.py` directly are
the exact same code, just launched differently.

## How it works

1. You start a small local web server (`server.py`, Python stdlib only — no
   pip installs).
2. Open `http://127.0.0.1:8000` in a browser. It auto-detects every
   removable card in your USB reader(s) and lists them, flagging any that
   already contain data.
3. Fill in the card-setup form (or load a saved profile), select the cards,
   hit **START**.
4. The OS image is downloaded once and decompressed once. Every selected
   card is then written **in parallel**, straight from RAM (page cache) —
   so writing 4 cards takes about the same time as writing 1.
5. Each card gets a unique hostname (`raspberrypi1`, `raspberrypi2`, ...) plus
   your configured user/password/Wi-Fi/timezone/keyboard/SSH, injected the
   same way the official Raspberry Pi Imager does it (`firstrun.sh` +
   `cmdline.txt`).
6. Boot the card — it applies the config, reboots once, and you're at a
   normal login. No wizard.

## Requirements

Linux only (uses `lsblk`, `/sys/block/*/stat`, `dd oflag=direct`,
`blockdev`, `partprobe` — none of which exist on macOS/Windows). Tested on
Ubuntu/Debian-family distros.

Run this once after cloning:

```bash
./check-requirements.sh
```

It checks for: `python3`, `lsblk`, `dd`, `xz`, `openssl`, `wpa_passphrase`,
`partprobe`, `jq`, `curl` (all from standard repos — e.g.
`sudo apt install python3 util-linux coreutils xz-utils openssl wpasupplicant parted jq curl`).
`rpi-imager` is optional, only used by the alternate CLI path (`flash-all.sh`).

## Setup

```bash
cp config.example.json config.json
```

Edit `config.json` with your real values — see the field reference below.
It's gitignored; your real password, Wi-Fi credentials, and SSH key never
get committed.

Running from a repo checkout, `config.json`/`profiles.json` next to
`server.py` always take priority if present (this workflow). Running the
installed `.deb` instead, where `/opt/piforge` ships only the `.example`
templates, PiForge falls back to `~/.config/piforge/` — resolved against
the actual invoking user (via `$SUDO_USER`/`$PKEXEC_UID`), not `root`, even
though the server itself must run as root to write block devices. Same
for the downloaded OS image cache and flash history: always under the real
user's own `~/rpi-images/`, never `/root/rpi-images/`.

## Web UI (recommended, fastest)

```bash
sudo python3 server.py
```

Open `http://127.0.0.1:8000`. Insert SD cards — they're detected
automatically and pre-selected. Adjust the form if needed, then **START**.

Root is required only for actually writing to block devices; device
detection and the page itself work without it (flashing is refused with a
clear error until you restart with `sudo`).

### Why parallel writes are fast

The image is decompressed exactly once (`xz -T0`, using every CPU core) into
a raw `.img`, which typically fits in RAM (~6 GB for the standard desktop
image) after the first flash. Every card then reads that image straight from
the kernel's page cache via `dd`, so N cards write concurrently at close to
each card's own hardware speed — not sequentially, and not re-paying the
decompression cost per card.

### Feature reference

- **OS image picker** — choose from Raspberry Pi Foundation's official
  "latest" links (Desktop/Lite/Full × current/Legacy Bookworm) or paste any
  custom `.img.xz` URL. Switching the image automatically invalidates the
  cached download so the new one is fetched.
- **Card capacity check** — before writing, each card's real size is
  compared against the image size. A card too small is refused with a clear
  error instead of silently getting a truncated, unbootable write.
- **Wi-Fi country / timezone / keyboard layout** — proper dropdowns (not
  free text), so you can't typo a country code. Timezone list comes from
  your browser's live IANA database when available.
- **Password eye icons** — click to reveal the password/Wi-Fi-password
  fields before submitting.
- **SSH access** (collapsible section) — paste one or more public keys to
  install into `~/.ssh/authorized_keys`; optionally disable password login
  entirely (key-only). Providing a key auto-enables SSH even if the
  checkbox is off.
- **Static IP** (collapsible section) — set a base address
  (e.g. `192.168.50.10`) and every card gets that address +1 per card,
  matching the same numbering as hostnames. Writes a `dhcpcd.conf` static
  profile for the interface you choose (`eth0`/`wlan0`).
- **Profiles** — save the whole form under a name (e.g. `classroom-kit`,
  `robot-cluster`) via **Save as**, reload it any time from the dropdown,
  delete with 🗑. Stored server-side in `profiles.json` (gitignored) so
  profiles persist across browsers/machines using the same server, unlike
  the localStorage-based "Remember settings".
- **Remember settings** — separately, saves your current form to this
  browser's `localStorage` so a page refresh doesn't lose your edits.
  **Reset** clears it and reloads `config.json`'s defaults.
- **Verify after write** — full byte-for-byte readback comparison after
  writing (roughly doubles time per batch). Off by default for speed;
  recommended for production/unattended batches.
- **Live speed + ETA** — each card's progress message shows current MB/s
  and estimated time remaining, not just a percentage.
- **Has-data warning** — a card that already contains recognizable
  filesystems is flagged with a badge and named explicitly in the erase
  confirmation dialog.
- **Retry** — a card that failed gets a one-click **↻ Retry** button that
  re-flashes just that card with the current form settings, keeping its
  original hostname/IP index rather than renumbering it as card 1.
- **History** — collapsible panel showing the last 50 cards flashed
  (timestamp, device, hostname, status, duration), read from
  `~/rpi-images/flash-history.csv`.
- **Browser notification** — check "Notify me when the batch finishes" to
  get a system notification with a done/failed summary when a batch
  completes (useful if you tab away during a big run).

## CLI only (no browser, no server)

```bash
sudo ./flash-all.sh /dev/sda /dev/sdb /dev/sdc
```

Requires `rpi-imager`. Simpler and more portable, but slower than the web UI
(rpi-imager decompresses and verifies per card rather than sharing one
decompressed image across all cards), and doesn't have the profiles/history/
retry/notification features — those are web-UI only.

## Injecting config into an already-flashed card

If a card was flashed by something else (another tool, a different
pipeline step that repartitions the boot volume afterward, etc.) and boots
into the first-run setup wizard instead of your configured user, that means
`firstrun.sh` and the `cmdline.txt` patch didn't survive to boot time —
usually because a later step overwrote the boot partition. Re-inject as the
very last step before the card is removed:

```bash
sudo ./inject-config.sh /dev/sda            # finds and mounts the boot partition
# or, if already mounted:
./inject-config.sh /media/you/bootfs
```

Optionally pass a hostname as a second argument to override the one in
`config.json`.

## Config field reference

See `config.example.json` for the full set with safe defaults. Notable ones
beyond the obvious hostname/user/password/Wi-Fi:

| Field | Meaning |
|---|---|
| `image_url` | Any Raspberry Pi OS `.img.xz` link. Get current links from https://www.raspberrypi.com/software/operating-systems/, or use one of the UI's built-in presets. |
| `ssh_authorized_key` | One or more public keys (newline-separated) to install for the configured user. |
| `disable_ssh_password` | `true` to require key-only SSH login. |
| `static_ip_base` / `static_ip_cidr` / `static_ip_gateway` / `static_ip_dns` / `static_ip_iface` | Static networking; leave `static_ip_base` empty to use DHCP (default). |
| `number_hostnames` | `false` to give every card in a batch the identical hostname — fine for one card, a network name conflict for more than one. |
| `verify` | `true` to always byte-verify after writing. |

Profiles use the same fields; see `profiles.example.json` for two sample
profiles you can copy to `profiles.json` and adapt (or just build them from
the UI's **Save as**).

## Safety

- Only removable block devices are ever listed or written — the device list
  actively excludes anything mounted at `/`, `/boot`, `/boot/firmware`,
  `/home`, or under `/usr`. Fixed internal disks never appear as flashable
  targets.
- A card smaller than the image is refused before any write starts.
- The web UI requires an explicit confirmation dialog naming every device
  (and any data already on it) before erasing anything.
- `flash-all.sh` requires typing `yes` before it touches any device.
- Still — this tool **permanently erases everything** on the cards you
  select. Double-check `lsblk` output before confirming if you have any
  doubt about which device is which.

## Project layout

| File | Purpose |
|---|---|
| `server.py` | Web UI backend: device detection, parallel `dd` flashing, profiles, history, config injection |
| `index.html` | Web UI frontend |
| `firstrun_gen.py` | Single shared implementation of the `firstrun.sh` template (hostname, user, SSH keys, Wi-Fi, static IP, timezone, keyboard) — used by `server.py`, `inject-config.sh`, and `flash-all.sh` so there's one place to trust, not three |
| `flash-all.sh` | CLI-only alternative using `rpi-imager --cli` |
| `inject-config.sh` | Re-inject config into an already-flashed card |
| `config.example.json` | Template — copy to `config.json` and edit |
| `profiles.example.json` | Sample named presets — copy to `profiles.json`, or just build them from the UI |
| `check-requirements.sh` | Verifies all required tools are installed |
| `VERSION` | Single source of truth for the package version |
| `packaging/build-deb.sh` | Builds the `.deb` from the current repo contents |
| `packaging/piforge` | Desktop launcher installed as `/usr/bin/piforge` — handles the `pkexec` prompt and app-window browser launch |
| `packaging/debian/piforge.desktop` | Application-menu entry |
| `packaging/piforge.svg` | App icon |

## License

MIT — see `LICENSE`.
