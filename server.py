#!/usr/bin/env python3
"""
PiForge — mass SD-card installer for Raspberry Pi OS.

Serves index.html, auto-detects removable SD cards, decompresses the OS
image once and writes every selected card in parallel straight from the
page cache (dd), then injects Imager-style firstrun.sh customization
(hostname, user, password, SSH keys, Wi-Fi, country, timezone, keyboard,
static IP). Also serves named config profiles and a flash history log.

Run:  sudo python3 server.py       then open http://127.0.0.1:8000
"""

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import csv

from firstrun_gen import sh_hash_password, wifi_psk, make_firstrun, compute_static_ip

PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IMAGE_URL = "https://downloads.raspberrypi.com/raspios_oldstable_arm64_latest"


def real_home():
    """The invoking user's actual home directory, even when this process
    is running as root via sudo/pkexec (as it must be, to write block
    devices). Without this, '~' resolves to /root and every user's image
    cache, profiles, and history end up hidden in root's home instead of
    their own — fine for a single developer running this from a repo
    checkout, wrong for a packaged app meant to be installed and used by
    anyone. Falls back to '~' for a real root login or a plain dev-mode
    `python3 server.py` with no privilege escalation involved."""
    uid = os.environ.get("PKEXEC_UID")
    if uid:
        import pwd
        return pwd.getpwuid(int(uid)).pw_dir
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        return pwd.getpwnam(sudo_user).pw_dir
    return os.path.expanduser("~")


USER_CONFIG_DIR = os.path.join(real_home(), ".config", "piforge")


def resolve_data_path(filename):
    """Prefer a file already sitting next to server.py — the dev-repo
    workflow (clone + edit config.json in place), unchanged from before
    packaging existed. Otherwise use the invoking user's own config
    directory, which is what a real `apt install`'d copy under /opt should
    use instead of writing into its own (root-owned) install directory."""
    local = os.path.join(BASE_DIR, filename)
    if os.path.exists(local):
        return local
    os.makedirs(USER_CONFIG_DIR, exist_ok=True)
    return os.path.join(USER_CONFIG_DIR, filename)


CONFIG_PATH = resolve_data_path("config.json")
CONFIG_EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
PROFILES_PATH = resolve_data_path("profiles.json")

IMAGES_DIR = os.path.join(real_home(), "rpi-images")
IMAGE_FILE = os.path.join(IMAGES_DIR, "os-image.img.xz")   # compressed download
RAW_IMAGE = os.path.join(IMAGES_DIR, "os-image.img")       # decompressed once
URL_MARKER = os.path.join(IMAGES_DIR, "os-image.url")      # which URL is cached
HISTORY_PATH = os.path.join(IMAGES_DIR, "flash-history.csv")
HISTORY_FIELDS = ["timestamp", "device", "hostname", "status", "duration_s", "image_url"]
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
    "ssh_authorized_key": "",
    "disable_ssh_password": False,
    "static_ip_base": "",
    "static_ip_cidr": 24,
    "static_ip_gateway": "",
    "static_ip_dns": "",
    "static_ip_iface": "eth0",
    "notify_on_finish": False,
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


def load_profiles():
    """Named config presets, e.g. {"classroom-kit": {...}}. Gitignored —
    may contain real Wi-Fi passwords per profile."""
    try:
        with open(PROFILES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_profiles(profiles):
    tmp = PROFILES_PATH + ".part"
    with open(tmp, "w") as f:
        json.dump(profiles, f, indent=2)
    os.replace(tmp, PROFILES_PATH)


def log_history(device, hostname, status, duration_s, image_url):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "device": device,
            "hostname": hostname,
            "status": status,
            "duration_s": round(duration_s, 1),
            "image_url": image_url,
        })


def read_history(limit=50):
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows))[:limit]

# ---------------------------------------------------------------- state

JOBS = {}          # device -> {state, percent, message, hostname}
JOBS_LOCK = threading.Lock()
PREP_LOCK = threading.Lock()
PREP_STATE = {"phase": "idle", "percent": 0, "error": None}  # idle|download|extract|ready
FLASH_ACTIVE = threading.Event()

RUNNING_PROCS = {}   # device -> Popen of the currently running dd/verify step
PROCS_LOCK = threading.Lock()
CANCEL_FLAGS = set()  # devices with a pending/handled cancel request
CANCEL_LOCK = threading.Lock()


def set_job(dev, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(dev, {})
        JOBS[dev].update(kw)


BUSY_STATES = {"queued", "downloading", "writing", "verifying", "configuring"}


def register_proc(dev, proc):
    with PROCS_LOCK:
        RUNNING_PROCS[dev] = proc


def unregister_proc(dev):
    with PROCS_LOCK:
        RUNNING_PROCS.pop(dev, None)


def is_cancelled(dev):
    with CANCEL_LOCK:
        return dev in CANCEL_FLAGS


def clear_cancel(dev):
    with CANCEL_LOCK:
        CANCEL_FLAGS.discard(dev)


def request_cancel(devices):
    """devices: explicit list, or falsy to cancel every currently-busy card.
    Kills the running dd/verify process group immediately; flash_device
    notices via is_cancelled() and marks the card 'cancelled' instead of
    'error'. Cards still queued/downloading (no process yet) are caught by
    the same check before they start writing."""
    if not devices:
        with JOBS_LOCK:
            devices = [d for d, j in JOBS.items() if j.get("state") in BUSY_STATES]
    with CANCEL_LOCK:
        CANCEL_FLAGS.update(devices)
    for dev in devices:
        set_job(dev, state="cancelling", message="Cancelling…")
        with PROCS_LOCK:
            proc = RUNNING_PROCS.get(dev)
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    return devices


# ---------------------------------------------------------------- devices

def list_devices():
    """Removable block devices safe to flash."""
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,SIZE,MODEL,TRAN,RM,TYPE,MOUNTPOINTS,FSTYPE"],
        capture_output=True, text=True, check=True).stdout
    devs = []
    for d in json.loads(out).get("blockdevices", []):
        if d.get("type") != "disk" or not d.get("rm"):
            continue
        if not d.get("size"):
            continue  # empty reader slot
        mounts = []
        fstypes = []
        def collect(node):
            for m in node.get("mountpoints") or []:
                if m:
                    mounts.append(m)
            if node.get("fstype"):
                fstypes.append(node["fstype"])
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
            "has_data": bool(fstypes),
            "fstypes": sorted(set(fstypes)),
            "job": job,
        })
    return devs


def device_size_bytes(dev):
    out = subprocess.run(["blockdev", "--getsize64", dev],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


# ---------------------------------------------------------------- image

def prepare_image(image_url=None):
    """Download + decompress once; safe to call from many threads.

    Decompressing once (xz -T0, all cores) and dd-ing the raw image means the
    kernel page cache feeds every card from RAM — no per-card decompression.
    image_url comes from the flash request itself (the UI's image picker),
    falling back to config.json's default only if the caller doesn't have
    one. If it changed since the last run, the stale cache is wiped so
    switching OS images (e.g. Lite vs desktop) redownloads.
    """
    image_url = image_url or current_image_url()
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
                req = urllib.request.Request(image_url, headers={"User-Agent": "piforge"})
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


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def watch_progress(dev, proc, total_bytes, state, stat_index):
    """Poll sysfs disk stats while a flash/verify process runs."""
    base = sectors(dev, stat_index)
    label = "Writing" if state == "writing" else "Verifying"
    start = time.time()
    while proc.poll() is None:
        time.sleep(1)
        done = (sectors(dev, stat_index) - base) * 512
        pct = min(100.0, done * 100 / total_bytes)
        elapsed = time.time() - start
        rate_mb_s = (done / 1048576) / elapsed if elapsed > 0 else 0
        eta = f" · ETA {format_duration((total_bytes - done) / (done / elapsed))}" \
            if done > 0 and elapsed > 0 else ""
        set_job(dev, state=state, percent=round(pct, 1),
                message=f"{label}… {pct:.0f}% · {rate_mb_s:.1f} MB/s{eta}")


def boot_partition(dev):
    for suffix in ("1", "p1"):
        if os.path.exists(dev + suffix):
            return dev + suffix
    return None


class Cancelled(Exception):
    """Raised inside flash_device when the user cancelled this card."""


def check_cancelled(dev):
    if is_cancelled(dev):
        clear_cancel(dev)
        raise Cancelled()


def flash_device(dev, cfg, hostname, static_ip=None):
    log = f"/tmp/flash-{os.path.basename(dev)}.log"
    start_time = time.time()
    image_url = cfg.get("image_url") or current_image_url()
    try:
        check_cancelled(dev)  # cancelled while still queued, before any work

        if not os.path.exists(RAW_IMAGE):
            set_job(dev, state="downloading", percent=0, message="Preparing image (once)…")
            prepare_image(image_url)

        check_cancelled(dev)
        total = os.path.getsize(RAW_IMAGE)

        # Refuse to silently truncate: a card smaller than the image would
        # write successfully but boot into a corrupt/incomplete filesystem.
        try:
            dev_size = device_size_bytes(dev)
        except Exception:
            dev_size = None
        if dev_size is not None and dev_size < total:
            raise RuntimeError(
                f"card too small: {human_size(dev_size)} available, "
                f"{human_size(total)} needed for this image")

        set_job(dev, state="writing", percent=0, message="Starting write…", hostname=hostname)

        subprocess.run(f"umount {dev}?* 2>/dev/null", shell=True)

        with open(log, "w") as lf:
            proc = subprocess.Popen(
                ["dd", f"if={RAW_IMAGE}", f"of={dev}", f"bs={DD_BS}",
                 "oflag=direct", "conv=fsync", "iflag=fullblock"],
                stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            register_proc(dev, proc)
            try:
                watch_progress(dev, proc, total, "writing", 6)
                rc = proc.wait()
            finally:
                unregister_proc(dev)
        check_cancelled(dev)
        if rc != 0:
            tail = open(log).read()[-400:]
            raise RuntimeError(f"dd exited {rc}: …{tail}")

        if cfg.get("verify"):
            set_job(dev, state="verifying", percent=0, message="Verifying…")
            with open(log, "a") as lf:
                proc = subprocess.Popen(
                    f"dd if='{dev}' bs={DD_BS} iflag=direct,fullblock 2>>'{log}'"
                    f" | head -c {total} | cmp -s - '{RAW_IMAGE}'",
                    shell=True, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
                register_proc(dev, proc)
                try:
                    watch_progress(dev, proc, total, "verifying", 2)
                    rc = proc.wait()
                finally:
                    unregister_proc(dev)
            check_cancelled(dev)
            if rc != 0:
                raise RuntimeError("verification FAILED — card data differs from image")

        check_cancelled(dev)
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
                f.write(make_firstrun(cfg, hostname, static_ip=static_ip))
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
        log_history(dev, hostname, "done", time.time() - start_time, image_url)
    except Cancelled:
        unregister_proc(dev)
        set_job(dev, state="cancelled", percent=0,
                message="Cancelled — card is incomplete, reflash before use")
        log_history(dev, hostname, "cancelled", time.time() - start_time, image_url)
    except Exception as e:
        unregister_proc(dev)
        set_job(dev, state="error", percent=0, message=str(e)[:300])
        log_history(dev, hostname, "error", time.time() - start_time, image_url)


def start_flash(devices, cfg):
    # Re-validate against current safe device list. Index cards by their
    # position in the *full* currently-connected list (not just the ones
    # being flashed this call) so retrying a single failed card reuses the
    # same hostname/static-IP it would have gotten in the original batch,
    # instead of renumbering it as "card 1".
    all_present = sorted(d["device"] for d in list_devices())
    bad = [d for d in devices if d not in all_present]
    if bad:
        raise ValueError(f"not a removable/safe device: {', '.join(bad)}")

    cfg["_hash"] = sh_hash_password(cfg["password"])
    if cfg.get("wifi_ssid"):
        cfg["_psk"] = wifi_psk(cfg["wifi_ssid"], cfg.get("wifi_password", ""))

    threads = []
    for dev in devices:
        i = all_present.index(dev) + 1
        hostname = (f"{cfg['hostname']}{i}" if cfg.get("number_hostnames", True)
                    else cfg["hostname"])
        static_ip = compute_static_ip(cfg["static_ip_base"], i) if cfg.get("static_ip_base") else None
        set_job(dev, state="queued", percent=0, message="Queued…", hostname=hostname)
        t = threading.Thread(target=flash_device, args=(dev, cfg, hostname, static_ip), daemon=True)
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
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
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
        elif path == "/api/devices":
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
        elif path == "/api/config":
            # Prefill values for the UI form. config.json (gitignored) wins
            # over config.example.json wins over generic built-in defaults —
            # nothing here is ever the repo's real Wi-Fi password.
            self._json(load_config_defaults())
        elif path == "/api/profiles":
            self._json(load_profiles())
        elif path == "/api/history":
            limit = int(query.get("limit", ["50"])[0])
            self._json({"rows": read_history(limit)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"

        if parsed.path == "/api/profiles":
            try:
                req = json.loads(body)
                name = (req.get("name") or "").strip()
                if not name:
                    raise ValueError("profile name is required")
                profiles = load_profiles()
                profiles[name] = req["config"]
                save_profiles(profiles)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path == "/api/cancel":
            try:
                req = json.loads(body)
                cancelled = request_cancel(req.get("devices") or None)
                self._json({"ok": True, "devices": cancelled})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path != "/api/flash":
            return self._json({"error": "not found"}, 404)
        if os.geteuid() != 0:
            return self._json({"error": "server not running as root — restart with sudo"}, 403)
        if FLASH_ACTIVE.is_set():
            return self._json({"error": "flash already in progress"}, 409)
        try:
            req = json.loads(body)
            devices = req["devices"]
            cfg = req["config"]
            for key in ("hostname", "user", "password", "timezone"):
                if not cfg.get(key):
                    raise ValueError(f"missing config field: {key}")
            # Wi-Fi is optional (Ethernet-only boards); country is required
            # only if a network is actually being configured.
            if cfg.get("wifi_ssid") and not cfg.get("wifi_country"):
                raise ValueError("wifi_country is required when wifi_ssid is set")
            if cfg.get("static_ip_base"):
                compute_static_ip(cfg["static_ip_base"], 1)  # validates format early
            if not devices:
                raise ValueError("no devices selected")
            start_flash(devices, cfg)
            self._json({"ok": True, "count": len(devices)})
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/profiles":
            return self._json({"error": "not found"}, 404)
        name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
        profiles = load_profiles()
        if name in profiles:
            del profiles[name]
            save_profiles(profiles)
        self._json({"ok": True})


if __name__ == "__main__":
    addr = ("127.0.0.1", PORT)
    print(f"Serving on http://{addr[0]}:{addr[1]}  (root: {os.geteuid() == 0})")
    if os.geteuid() != 0:
        print("WARNING: not root — device detection works, flashing will be refused.")
    ThreadingHTTPServer(addr, Handler).serve_forever()
