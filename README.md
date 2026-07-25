# Pi Mass Flasher

Flash Raspberry Pi OS to many SD cards at once — in parallel, with hostname,
user, password, Wi-Fi, country, timezone and SSH pre-configured so every card
boots straight to a working desktop or headless login. No setup wizard, no
per-card manual typing.

Built for anyone provisioning more than one Raspberry Pi at a time: classroom
kits, workshops, IoT fleets, cluster builds.

## How it works

1. You start a small local web server (`server.py`, Python stdlib only — no
   pip installs).
2. Open `http://127.0.0.1:8000` in a browser. It auto-detects every
   removable card in your USB reader(s) and lists them.
3. Fill in the card-setup form (or let it prefill from `config.json`), select
   the cards, hit **START**.
4. The OS image is downloaded once and decompressed once. Every selected
   card is then written **in parallel**, straight from RAM (page cache) —
   so writing 4 cards takes about the same time as writing 1.
5. Each card gets a unique hostname (`raspberrypi1`, `raspberrypi2`, ...) plus
   your configured user/password/Wi-Fi/timezone/SSH, injected the same way
   the official Raspberry Pi Imager does it (`firstrun.sh` + `cmdline.txt`).
6. Boot the card — it applies the config, reboots once, and you're at a
   normal login. No wizard.

## Requirements

Linux only (uses `lsblk`, `/sys/block/*/stat`, `dd oflag=direct`,
`partprobe` — none of which exist on macOS/Windows). Tested on
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

Edit `config.json` with your real values:

```json
{
  "image_url": "https://downloads.raspberrypi.com/raspios_oldstable_arm64_latest",
  "hostname": "raspberrypi",
  "number_hostnames": true,
  "user": "pi",
  "password": "changeme",
  "wifi_ssid": "",
  "wifi_password": "",
  "wifi_country": "US",
  "timezone": "UTC",
  "keymap": "us",
  "enable_ssh": true,
  "verify": false
}
```

`config.json` is gitignored — it holds your real password and Wi-Fi
credentials and will never be committed. Leave `wifi_ssid` empty to skip
Wi-Fi setup entirely (e.g. Ethernet-only boards).

`image_url` accepts any Raspberry Pi OS `.img.xz` download link — Lite,
desktop, 32-bit, 64-bit, Legacy (Bookworm) or current (Trixie+). Get the
current links from https://www.raspberrypi.com/software/operating-systems/.
Switching `image_url` automatically invalidates the cached image and
re-downloads.

## Usage — Web UI (recommended, fastest)

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

Enable **Verify after write** in the UI if you want a full byte-for-byte
readback comparison afterward (roughly doubles the time per batch). Off by
default for speed; recommended for production/unattended batches.

## Usage — CLI only (no browser, no server)

```bash
sudo ./flash-all.sh /dev/sda /dev/sdb /dev/sdc
```

Requires `rpi-imager`. Simpler and more portable, but slower than the web UI
(rpi-imager decompresses and verifies per card rather than sharing one
decompressed image across all cards).

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

## Safety

- Only removable block devices are ever listed or written — the device list
  actively excludes anything mounted at `/`, `/boot`, `/boot/firmware`,
  `/home`, or under `/usr`. Fixed internal disks never appear as flashable
  targets.
- The web UI requires an explicit confirmation dialog naming every device
  before erasing anything.
- `flash-all.sh` requires typing `yes` before it touches any device.
- Still — this tool **permanently erases everything** on the cards you
  select. Double-check `lsblk` output before confirming if you have any
  doubt about which device is which.

## Project layout

| File | Purpose |
|---|---|
| `server.py` | Web UI backend: device detection, parallel `dd` flashing, config injection |
| `index.html` | Web UI frontend |
| `firstrun_gen.py` | Single shared implementation of the `firstrun.sh` template — used by `server.py`, `inject-config.sh`, and `flash-all.sh` so there's one place to trust, not three |
| `flash-all.sh` | CLI-only alternative using `rpi-imager --cli` |
| `inject-config.sh` | Re-inject config into an already-flashed card |
| `config.example.json` | Template — copy to `config.json` and edit |
| `check-requirements.sh` | Verifies all required tools are installed |

## License

MIT — see `LICENSE`.
