#!/usr/bin/env python3
"""
Mass SD-card flasher for Raspberry Pi OS (Legacy, 64-bit, Bookworm + desktop).

Serves index.html, auto-detects removable SD cards, flashes them in parallel
with rpi-imager --cli (falls back to xzcat|dd if rpi-imager is missing) and
injects Imager-style firstrun.sh customization (hostname, user, password,
Wi-Fi, country, timezone, SSH).

Run:  sudo python3 server.py       then open http://127.0.0.1:8000
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from firstrun_gen import sh_hash_password, wifi_psk, make_firstrun

PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
DEFAULT_IMAGE_URL = "https://downloads.raspberrypi.com/raspios_oldstable_arm64_latest"

IMAGES_DIR = os.path.expanduser("~/rpi-images")
IMAGE_FILE = os.path.join(IMAGES_DIR, "os-image.img.xz")   # compressed download
RAW_IMAGE = os.path.join(IMAGES_DIR, "os-image.img")       # decompressed once
URL_MARKER = os.path.join(IMAGES_DIR, "os-image.url")      # which URL is cached
DD_BS = "8M"

# Generic, secret-free fallback used only if config.json is absent — the UI
# lets you edit every field before flashing regardless.
BUILTIN_DEFAULTS = {
    "image_url": DEFAULT_IMAGE_URL,
    "hostname": "raspberrypi",
    "number_hostnames": True,
    "user": "pi",
    "password": "changeme",
    "wifi_ssid": "",
    "wifi_password": "",
    "wifi_country": "US",
    "timezone": "UTC",
    "keymap": "us",
    "enable_ssh": True,
    "verify": False,
}


def load_config_defaults():
    """Merge config.json (gitignored, user's real values) over built-in
    generic defaults. Never raises — a missing/broken config.json just
    means the form starts from safe generic placeholders."""
    cfg = dict(BUILTIN_DEFAULTS)
    for path in (CONFIG_EXAMPLE_PATH, CONFIG_PATH):
        try:
            with open(path) as f:
                cfg.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception:
            pass  # malformed config.json shouldn't crash the server
    return cfg


def current_image_url():
    return load_config_defaults().get("image_url") or DEFAULT_IMAGE_URL

# ---------------------------------------------------------------- state

JOBS = {}          # device -> {state, percent, message, hostname}
JOBS_LOCK = threading.Lock()
PREP_LOCK = threading.Lock()
PREP_STATE = {"phase": "idle", "percent": 0, "error": None}  # idle|download|extract|ready
FLASH_ACTIVE = threading.Event()


def set_job(dev, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(dev, {})
        JOBS[dev].update(kw)


# ---------------------------------------------------------------- devices

def list_devices():
    """Removable block devices safe to flash."""
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,SIZE,MODEL,TRAN,RM,TYPE,MOUNTPOINTS"],
        capture_output=True, text=True, check=True).stdout
    devs = []
    for d in json.loads(out).get("blockdevices", []):
        if d.get("type") != "disk" or not d.get("rm"):
            continue
        if not d.get("size"):
            continue  # empty reader slot
        mounts = []
        def collect(node):
            for m in node.get("mountpoints") or []:
                if m:
                    mounts.append(m)
            for c in node.get("children") or []:
                collect(c)
        collect(d)
        if any(m in ("/", "/boot", "/boot/firmware", "/home") or m.startswith("/usr")
               for m in mounts):
            continue  # never offer a system disk
        path = "/dev/" + d["name"]
        with JOBS_LOCK:
            job = dict(JOBS.get(path, {}))
        devs.append({
            "device": path,
            "size": d["size"],
            "size_h": human_size(d["size"]),
            "model": (d.get("model") or "").strip() or "Unknown",
            "tran": d.get("tran") or "",
            "job": job,
        })
    return devs


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


# ---------------------------------------------------------------- image

def prepare_image():
    """Download + decompress once; safe to call from many threads.

    Decompressing once (xz -T0, all cores) and dd-ing the raw image means the
    kernel page cache feeds every card from RAM — no per-card decompression.
    If the configured image_url changed since the last run, the stale cache
    is wiped so switching OS images (e.g. Lite vs desktop) redownloads.
    """
    image_url = current_image_url()
    with PREP_LOCK:
        cached_url = None
        if os.path.exists(URL_MARKER):
            cached_url = open(URL_MARKER).read().strip()
        if cached_url != image_url:
            for f in (IMAGE_FILE, RAW_IMAGE, URL_MARKER):
                if os.path.exists(f):
                    os.remove(f)
        global _UNCOMP_SIZE
        _UNCOMP_SIZE = None

        if os.path.exists(RAW_IMAGE):
            PREP_STATE.update(phase="ready", percent=100)
            return
        os.makedirs(IMAGES_DIR, exist_ok=True)
        try:
            if not os.path.exists(IMAGE_FILE):
                PREP_STATE.update(phase="download", percent=0, error=None)
                tmp = IMAGE_FILE + ".part"
                req = urllib.request.Request(image_url, headers={"User-Agent": "mass-flasher"})
                with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            PREP_STATE["percent"] = round(got * 100 / total, 1)
                os.replace(tmp, IMAGE_FILE)

            PREP_STATE.update(phase="extract", percent=0, error=None)
            total = uncompressed_size()
            tmp = RAW_IMAGE + ".part"
            with open(tmp, "wb") as out:
                proc = subprocess.Popen(["xz", "-dc", "-T0", IMAGE_FILE], stdout=out)
                while proc.poll() is None:
                    time.sleep(1)
                    PREP_STATE["percent"] = round(
                        min(100.0, os.path.getsize(tmp) * 100 / total), 1)
                if proc.returncode != 0:
                    raise RuntimeError("xz extraction failed")
            os.replace(tmp, RAW_IMAGE)
            with open(URL_MARKER, "w") as f:
                f.write(image_url)
            # Pre-warm page cache so parallel dd readers hit RAM, not disk
            subprocess.run(f"cat '{RAW_IMAGE}' > /dev/null", shell=True)
            PREP_STATE.update(phase="ready", percent=100)
        except Exception as e:
            PREP_STATE.update(phase="idle", error=str(e))
            raise


_UNCOMP_SIZE = None

def uncompressed_size():
    global _UNCOMP_SIZE
    if _UNCOMP_SIZE:
        return _UNCOMP_SIZE
    out = subprocess.run(["xz", "--robot", "-l", IMAGE_FILE],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        f = line.split("\t")
        if f and f[0] == "totals":
            _UNCOMP_SIZE = int(f[4])
            return _UNCOMP_SIZE
    return os.path.getsize(IMAGE_FILE) * 2  # rough fallback


# ---------------------------------------------------------------- firstrun
#
# The actual firstrun.sh template lives in firstrun_gen.py — one shared
# implementation used by this server, inject-config.sh, and flash-all.sh.


# ---------------------------------------------------------------- flashing

def sectors(dev, index):
    """Read cumulative read(2)/write(6) sectors from /sys/block/X/stat."""
    try:
        with open(f"/sys/block/{os.path.basename(dev)}/stat") as f:
            return int(f.read().split()[index])
    except Exception:
        return 0


def watch_progress(dev, proc, total_bytes, state, stat_index):
    """Poll sysfs disk stats while a flash/verify process runs."""
    base = sectors(dev, stat_index)
    label = "Writing" if state == "writing" else "Verifying"
    while proc.poll() is None:
        time.sleep(1)
        done = (sectors(dev, stat_index) - base) * 512
        pct = min(100.0, done * 100 / total_bytes)
        rate = done / 1048576  # cumulative MB, for rough speed on message
        set_job(dev, state=state, percent=round(pct, 1),
                message=f"{label}… {pct:.0f}% ({rate:.0f} MB)")


def boot_partition(dev):
    for suffix in ("1", "p1"):
        if os.path.exists(dev + suffix):
            return dev + suffix
    return None


def flash_device(dev, cfg, hostname):
    log = f"/tmp/flash-{os.path.basename(dev)}.log"
    try:
        if not os.path.exists(RAW_IMAGE):
            set_job(dev, state="downloading", percent=0, message="Preparing image (once)…")
            prepare_image()

        total = os.path.getsize(RAW_IMAGE)
        set_job(dev, state="writing", percent=0, message="Starting write…", hostname=hostname)

        subprocess.run(f"umount {dev}?* 2>/dev/null", shell=True)

        with open(log, "w") as lf:
            proc = subprocess.Popen(
                ["dd", f"if={RAW_IMAGE}", f"of={dev}", f"bs={DD_BS}",
                 "oflag=direct", "conv=fsync", "iflag=fullblock"],
                stdout=lf, stderr=subprocess.STDOUT)
            watch_progress(dev, proc, total, "writing", 6)
            rc = proc.wait()
        if rc != 0:
            tail = open(log).read()[-400:]
            raise RuntimeError(f"dd exited {rc}: …{tail}")

        if cfg.get("verify"):
            set_job(dev, state="verifying", percent=0, message="Verifying…")
            with open(log, "a") as lf:
                proc = subprocess.Popen(
                    f"dd if='{dev}' bs={DD_BS} iflag=direct,fullblock 2>>'{log}'"
                    f" | head -c {total} | cmp -s - '{RAW_IMAGE}'",
                    shell=True, stdout=lf, stderr=subprocess.STDOUT)
                watch_progress(dev, proc, total, "verifying", 2)
                rc = proc.wait()
            if rc != 0:
                raise RuntimeError("verification FAILED — card data differs from image")

        set_job(dev, state="configuring", percent=100, message="Injecting configuration…")
        subprocess.run(["partprobe", dev], capture_output=True)
        time.sleep(2)
        part = boot_partition(dev)
        if not part:
            raise RuntimeError("boot partition not found after write")

        # kick out any desktop automount so we are the only writer
        subprocess.run(["umount", part], capture_output=True)

        mnt = tempfile.mkdtemp(prefix="bootfs-")
        try:
            subprocess.run(["mount", part, mnt], check=True, capture_output=True)
            with open(os.path.join(mnt, "firstrun.sh"), "w") as f:
                f.write(make_firstrun(cfg, hostname))
            os.chmod(os.path.join(mnt, "firstrun.sh"), 0o755)
            cmdline_path = os.path.join(mnt, "cmdline.txt")
            with open(cmdline_path) as f:
                cmdline = f.read()
            cmdline = re.sub(r" systemd\.run.*", "", cmdline).rstrip("\n")
            cmdline += (" systemd.run=/boot/firstrun.sh"
                        " systemd.run_success_action=reboot"
                        " systemd.unit=kernel-command-line.target\n")
            with open(cmdline_path, "w") as f:
                f.write(cmdline)
            subprocess.run(["sync"])
        finally:
            subprocess.run(["umount", mnt], capture_output=True)

        # verify config landed: fresh read-only mount, check both files
        try:
            subprocess.run(["mount", "-o", "ro", part, mnt], check=True, capture_output=True)
            ok = (os.path.getsize(os.path.join(mnt, "firstrun.sh")) > 0
                  and "systemd.run=/boot/firstrun.sh" in open(os.path.join(mnt, "cmdline.txt")).read())
            if not ok:
                raise RuntimeError("config verify failed: firstrun.sh/cmdline.txt not persisted")
        finally:
            subprocess.run(["umount", mnt], capture_output=True)
            os.rmdir(mnt)

        set_job(dev, state="done", percent=100,
                message=f"Done — {hostname} configured & verified, safe to remove")
    except Exception as e:
        set_job(dev, state="error", percent=0, message=str(e)[:300])


def start_flash(devices, cfg):
    # Re-validate against current safe device list
    safe = {d["device"] for d in list_devices()}
    bad = [d for d in devices if d not in safe]
    if bad:
        raise ValueError(f"not a removable/safe device: {', '.join(bad)}")

    cfg["_hash"] = sh_hash_password(cfg["password"])
    if cfg.get("wifi_ssid"):
        cfg["_psk"] = wifi_psk(cfg["wifi_ssid"], cfg.get("wifi_password", ""))

    threads = []
    for i, dev in enumerate(sorted(devices), start=1):
        hostname = (f"{cfg['hostname']}{i}" if cfg.get("number_hostnames", True)
                    else cfg["hostname"])
        set_job(dev, state="queued", percent=0, message="Queued…", hostname=hostname)
        t = threading.Thread(target=flash_device, args=(dev, cfg, hostname), daemon=True)
        threads.append(t)

    def runner():
        FLASH_ACTIVE.set()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        FLASH_ACTIVE.clear()

    threading.Thread(target=runner, daemon=True).start()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._json({"error": "index.html missing"}, 404)
        elif self.path == "/api/devices":
            try:
                self._json({
                    "devices": list_devices(),
                    "flashing": FLASH_ACTIVE.is_set(),
                    "prep": PREP_STATE,
                    "image_cached": os.path.exists(RAW_IMAGE),
                    "image_url": current_image_url(),
                    "root": os.geteuid() == 0,
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/config":
            # Prefill values for the UI form. config.json (gitignored) wins
            # over config.example.json wins over generic built-in defaults —
            # nothing here is ever the repo's real Wi-Fi password.
            self._json(load_config_defaults())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/flash":
            return self._json({"error": "not found"}, 404)
        if os.geteuid() != 0:
            return self._json({"error": "server not running as root — restart with sudo"}, 403)
        if FLASH_ACTIVE.is_set():
            return self._json({"error": "flash already in progress"}, 409)
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            devices = req["devices"]
            cfg = req["config"]
            for key in ("hostname", "user", "password", "timezone"):
                if not cfg.get(key):
                    raise ValueError(f"missing config field: {key}")
            # Wi-Fi is optional (Ethernet-only boards); country is required
            # only if a network is actually being configured.
            if cfg.get("wifi_ssid") and not cfg.get("wifi_country"):
                raise ValueError("wifi_country is required when wifi_ssid is set")
            if not devices:
                raise ValueError("no devices selected")
            start_flash(devices, cfg)
            self._json({"ok": True, "count": len(devices)})
        except Exception as e:
            self._json({"error": str(e)}, 400)


if __name__ == "__main__":
    addr = ("127.0.0.1", PORT)
    print(f"Serving on http://{addr[0]}:{addr[1]}  (root: {os.geteuid() == 0})")
    if os.geteuid() != 0:
        print("WARNING: not root — device detection works, flashing will be refused.")
    ThreadingHTTPServer(addr, Handler).serve_forever()
