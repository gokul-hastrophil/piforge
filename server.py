#!/usr/bin/env python3
"""
PiForge — mass SD-card installer for Raspberry Pi OS.

Serves index.html, auto-detects removable SD cards, decompresses the OS
image once and writes every selected card in parallel straight from the
page cache (dd), then injects Imager-style firstrun.sh customization
(hostname, user, password, SSH keys, Wi-Fi, country, timezone, keyboard,
static IP). Also serves named config profiles and a flash history log.

Run:  sudo python3 server.py       then open http://127.0.0.1:47823
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

PORT = 47823
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
PARTITION_PROFILES_PATH = resolve_data_path("partition_profiles.json")

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


def load_partition_profiles():
    """Named partition-layout presets, e.g. {"two-way-split": {"table": "gpt",
    "partitions": [...]}}. No secrets here (unlike profiles.json) but kept
    in the same per-user data dir for consistency."""
    try:
        with open(PARTITION_PROFILES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_partition_profiles(profiles):
    tmp = PARTITION_PROFILES_PATH + ".part"
    with open(tmp, "w") as f:
        json.dump(profiles, f, indent=2)
    os.replace(tmp, PARTITION_PROFILES_PATH)


def load_full_profiles():
    """One named profile = a flash config and a partition layout saved
    together — the UI has a single profile picker, not two. Storage stays
    as the two separate files above (profiles.json/partition_profiles.json,
    only one of which holds secrets) merged here on read; either half can
    be absent for a given name."""
    configs = load_profiles()
    partitions = load_partition_profiles()
    names = set(configs) | set(partitions)
    return {name: {"config": configs.get(name), "partition": partitions.get(name)} for name in names}


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


BUSY_STATES = {"queued", "downloading", "writing", "verifying", "configuring",
                "partitioning", "formatting"}


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


# ---------------------------------------------------------------- partitioning

# parted's mkpart FS-TYPE hint (alignment/partition-type only; the real
# filesystem is created by mkfs below) and the mkfs command to actually
# format each partition. "none" leaves a raw, unformatted partition.
FSTYPES = {
    "fat32": {"parted": "fat32", "mkfs": ["mkfs.vfat", "-F", "32"], "label_flag": "-n"},
    "fat16": {"parted": "fat16", "mkfs": ["mkfs.vfat", "-F", "16"], "label_flag": "-n"},
    "ext4": {"parted": "ext4", "mkfs": ["mkfs.ext4", "-F"], "label_flag": "-L"},
    "ext3": {"parted": "ext3", "mkfs": ["mkfs.ext3", "-F"], "label_flag": "-L"},
    "exfat": {"parted": "", "mkfs": ["mkfs.exfat"], "label_flag": "-n"},
    "ntfs": {"parted": "ntfs", "mkfs": ["mkfs.ntfs", "-f"], "label_flag": "-L"},
    "linux-swap": {"parted": "linux-swap", "mkfs": ["mkswap"], "label_flag": "-L"},
    "none": {"parted": "", "mkfs": None, "label_flag": None},
}


def read_partition_table(dev):
    """Current partition table of dev, via `parted -m -s unit MiB print`.
    Confirmed field layout against a real (loopback) device:
      BYT;
      /dev/loopN:200MiB:loopback:512:512:gpt:Loopback device:;
      1:1.00MiB:21.0MiB:20.0MiB:fat32:boot:boot, esp;
    i.e. header line then one line per partition:
      number:start:end:size:fstype:name:flags;
    Raises RuntimeError (not CalledProcessError) with parted's own message
    when the device has no recognised label yet — that's a normal,
    expected state for a blank card, not a real error."""
    out = subprocess.run(["parted", "-m", "-s", dev, "unit", "MiB", "print"],
                          capture_output=True, text=True)
    lines = [l for l in out.stdout.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        raise RuntimeError((out.stderr or "no partition table").strip())
    disk_fields = lines[1].rstrip(";").split(":")
    table = disk_fields[5] if len(disk_fields) > 5 else "unknown"
    size_mib = float(disk_fields[1].rstrip("MiBGiBkB"))
    partitions = []
    for line in lines[2:]:
        f = line.rstrip(";").split(":")
        partitions.append({
            "number": int(f[0]),
            "start_mib": float(f[1].rstrip("MiBGiBkB")),
            "end_mib": float(f[2].rstrip("MiBGiBkB")),
            "size_mib": float(f[3].rstrip("MiBGiBkB")),
            "fstype": f[4] if len(f) > 4 else "",
            "name": f[5] if len(f) > 5 else "",
            "flags": [s.strip() for s in f[6].split(",")] if len(f) > 6 and f[6] else [],
        })
    return {"table": table, "size_mib": size_mib, "partitions": partitions}


def part_path(dev, index):
    """Partition device node for a given 1-based index.

    Not a guess-both-and-see-what-exists: 'sdX' + '1' for /dev/sdX-style
    names, but 'mmcblkX' + 'p1' for names that already end in a digit
    (mmcblk/nvme/loop). The naive "try dev+str(index), else dev+'p'+
    str(index)" approach is unsafe for /dev/loopN devices — dev+str(index)
    can collide with an unrelated, already-existing device of a different
    number (e.g. /dev/loop5 + "1" = /dev/loop51, a real, different loop
    device, not a partition of loop5) — confirmed by testing against a
    real loopback device, where this produced a wrong-device match."""
    base = os.path.basename(dev)
    sep = "p" if base[-1:].isdigit() else ""
    return dev + sep + str(index)


def wait_for_part(dev, index, timeout=15):
    """Device nodes for newly-created partitions don't always exist the
    instant partprobe returns — confirmed by testing against a loopback
    device, where a flat sleep(2) intermittently missed a node that
    appeared a few hundred ms later. Poll instead of guessing a fixed
    delay, re-issuing partprobe periodically in case the first rescan
    didn't fully propagate. udevadm settle is best-effort (absent on
    some minimal images)."""
    path = part_path(dev, index)
    deadline = time.time() + timeout
    tries = 0
    while time.time() < deadline:
        if os.path.exists(path):
            return path
        if tries % 5 == 0:
            subprocess.run(["partprobe", dev], capture_output=True)
        try:
            subprocess.run(["udevadm", "settle", "--timeout=1"], capture_output=True)
        except FileNotFoundError:
            pass  # not every distro/image ships udevadm — fall back to plain polling
        time.sleep(0.3)
        tries += 1
    return path if os.path.exists(path) else None


def plan_partitions(dev, profile):
    """Compute where every NEW partition will land — pure calculation, no
    disk writes — so the actual creation loop just executes a plan instead
    of interleaving math with mutation. Handles three things Phase 1 added
    beyond the original percent-only/wipe-everything model:
      - keep_existing: read the card's real current table and start placing
        new partitions right after the last kept one, instead of always
        wiping the whole disk.
      - absolute sizing ("size_mib") alongside percent, and a bare "no size
        given" entry meaning "fill whatever's left".
      - MBR primary vs logical, auto-inserting the one extended container
        a logical run needs, numbered exactly the way parted itself numbers
        primaries/extended (sequential from the next free slot) and
        logicals (always from 5) — confirmed against a real loopback device.
    """
    parts_spec = profile["partitions"]
    keep_existing = int(profile.get("keep_existing", 0) or 0)
    label_type = "gpt" if profile.get("table", "gpt") == "gpt" else "msdos"
    disk_mib = device_size_bytes(dev) / 1024 / 1024

    to_remove = []
    if keep_existing > 0:
        existing = read_partition_table(dev)
        if len(existing["partitions"]) < keep_existing:
            raise RuntimeError(
                f"card has {len(existing['partitions'])} partition(s), "
                f"need at least {keep_existing} to keep")
        if (existing["table"] == "gpt") != (label_type == "gpt"):
            raise RuntimeError("existing table type doesn't match this layout's table type")
        kept = [p for p in existing["partitions"] if p["number"] <= keep_existing]
        cursor = max(p["end_mib"] for p in kept)
        # Partitions past the kept count are still physically occupying
        # that space in the real table — leaving them in place makes every
        # new partition collide with them (confirmed against a real
        # loopback device: parted refused the new partition outright,
        # "closest location we can manage" landing at the disk's end).
        # Remove them first; highest number first so an MBR extended
        # container isn't deleted out from under its own logicals.
        to_remove = sorted((p["number"] for p in existing["partitions"] if p["number"] > keep_existing), reverse=True)
    else:
        cursor = 1.0  # standard 1 MiB alignment for a fresh table

    # 1 MiB end slack for every table type, not just GPT (whose backup
    # partition table needs it) — confirmed against a real loopback device
    # that parted rejects a partition reaching the literal disk-size
    # boundary even for MBR, always leave a small margin.
    usable_end = disk_mib - 1.0
    prim_num, log_num = keep_existing + 1, 5
    extended_created = False
    plan = []

    for i, p in enumerate(parts_spec):
        is_remainder = "size_mib" not in p and "percent" not in p
        if "size_mib" in p:
            end = cursor + float(p["size_mib"])
        elif "percent" in p:
            end = cursor + disk_mib * float(p["percent"]) / 100.0
        else:
            end = usable_end  # remainder — only sensible on the last entry
        if is_remainder:
            end = min(end, usable_end)
        elif end > usable_end + 1e-6:
            # An explicit size_mib/percent that doesn't fit is a user
            # mistake (typo'd size, wrong card) — fail loudly instead of
            # silently shrinking it to whatever's left, which would hide
            # the mistake and hand back a smaller partition than asked for.
            raise RuntimeError(
                f"partition {i + 1} ({p.get('label', '?')}) needs "
                f"{end - cursor:.0f}MiB but only {usable_end - cursor:.0f}MiB is left on this card")
        if end <= cursor:
            raise RuntimeError(f"partition {i + 1} ({p.get('label', '?')}) has no room left on this card")

        is_logical = label_type == "msdos" and p.get("type") == "logical"
        if is_logical and not extended_created:
            plan.append({"kind": "extended", "number": prim_num,
                         "start_mib": cursor, "end_mib": usable_end})
            prim_num += 1
            extended_created = True
            cursor += 1.0  # EBR overhead before the first logical

        number = log_num if is_logical else prim_num
        if is_logical:
            log_num += 1
        else:
            prim_num += 1
        plan.append({"kind": "logical" if is_logical else "primary",
                     "number": number, "start_mib": cursor, "end_mib": end, "spec": p})
        cursor = end + (1.0 if is_logical else 0.0)

    return label_type, plan, to_remove


def partition_device(dev, profile):
    start_time = time.time()
    try:
        check_cancelled(dev)
        set_job(dev, state="partitioning", percent=0, message="Unmounting…")
        subprocess.run(f"umount {dev}?* 2>/dev/null", shell=True)

        keep_existing = int(profile.get("keep_existing", 0) or 0)

        # Validate the layout against this card's real size before wiping
        # anything — a plan_partitions() failure (e.g. a size that doesn't
        # fit) should leave a fresh-table card untouched, not wiped-then-
        # errored. (keep_existing>0 reads the current table instead, which
        # is unaffected either way since nothing's wiped in that branch.)
        check_cancelled(dev)
        label_type, plan, to_remove = plan_partitions(dev, profile)
        new_parts = [e for e in plan if e["kind"] != "extended"]

        if keep_existing == 0:
            check_cancelled(dev)
            set_job(dev, state="partitioning", percent=5, message="Wiping old signatures…")
            subprocess.run(["wipefs", "-a", dev], capture_output=True)
            requested_type = "gpt" if profile.get("table", "gpt") == "gpt" else "msdos"
            subprocess.run(["parted", "--script", dev, "mklabel", requested_type],
                            check=True, capture_output=True)
        else:
            for number in to_remove:
                check_cancelled(dev)
                set_job(dev, state="partitioning", percent=5,
                        message=f"Removing old partition {number}…")
                subprocess.run(["parted", "--script", dev, "rm", str(number)],
                                check=True, capture_output=True)

        for idx, entry in enumerate(plan, start=1):
            check_cancelled(dev)
            set_job(dev, state="partitioning", percent=5 + round(40 * idx / len(plan)),
                    message=f"Creating partition {idx}/{len(plan)}…")
            # Integer MiB only — this parted build mis-parses a decimal
            # point in a location argument (e.g. "33.3%" reads as two
            # arguments "33" "3%" and fails with a cryptic "invalid syntax
            # for locations" error); rounding whole MiB values sidesteps it
            # entirely and is far more precise than percent for exact sizes.
            start_arg, end_arg = f"{round(entry['start_mib'])}MiB", f"{round(entry['end_mib'])}MiB"
            if entry["kind"] == "extended":
                subprocess.run(["parted", "--script", dev, "mkpart", "extended", start_arg, end_arg],
                                check=True, capture_output=True)
                continue
            p = entry["spec"]
            spec = FSTYPES.get(p["fstype"], FSTYPES["none"])
            if label_type == "gpt":
                cmd = ["parted", "--script", dev, "mkpart", (p.get("label") or f"part{idx}")[:36]]
            else:
                cmd = ["parted", "--script", dev, "mkpart", entry["kind"]]
            if spec["parted"]:
                cmd.append(spec["parted"])
            cmd += [start_arg, end_arg]
            subprocess.run(cmd, check=True, capture_output=True)
            for flag in (p.get("flags") or []):
                subprocess.run(["parted", "--script", dev, "set", str(entry["number"]), flag, "on"],
                                capture_output=True)

        check_cancelled(dev)
        subprocess.run(["partprobe", dev], capture_output=True)

        for idx, entry in enumerate(new_parts, start=1):
            check_cancelled(dev)
            path = wait_for_part(dev, entry["number"])
            if not path:
                raise RuntimeError(f"partition {entry['number']} not found after create")
            p = entry["spec"]
            spec = FSTYPES.get(p["fstype"], FSTYPES["none"])
            set_job(dev, state="formatting", percent=50 + round(45 * idx / len(new_parts)),
                    message=f"Formatting partition {idx}/{len(new_parts)} ({p['fstype']})…")
            if spec["mkfs"]:
                cmd = list(spec["mkfs"])
                label = (p.get("label") or "")[:16]
                if label and spec["label_flag"]:
                    cmd += [spec["label_flag"], label]
                cmd += [path]
                subprocess.run(cmd, check=True, capture_output=True)

        check_cancelled(dev)
        subprocess.run(["sync"])
        set_job(dev, state="done", percent=100,
                message=f"Done — {len(new_parts)} partition(s) created")
        log_history(dev, "", "partitioned", time.time() - start_time, "")
    except Cancelled:
        unregister_proc(dev)
        set_job(dev, state="cancelled", percent=0,
                message="Cancelled — partition table may be incomplete, do not use the card")
        log_history(dev, "", "cancelled", time.time() - start_time, "")
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:300] if isinstance(e.stderr, bytes) else str(e)[:300]
        set_job(dev, state="error", percent=0, message=err or str(e)[:300])
        log_history(dev, "", "error", time.time() - start_time, "")
    except Exception as e:
        set_job(dev, state="error", percent=0, message=str(e)[:300])
        log_history(dev, "", "error", time.time() - start_time, "")


def start_partition(devices, profile):
    all_present = sorted(d["device"] for d in list_devices())
    bad = [d for d in devices if d not in all_present]
    if bad:
        raise ValueError(f"not a removable/safe device: {', '.join(bad)}")
    parts = profile.get("partitions") or []
    if not parts:
        raise ValueError("layout has no partitions")
    if int(profile.get("keep_existing", 0) or 0) < 0:
        raise ValueError("keep_existing can't be negative")
    # Absolute (size_mib) entries can't be validated here — room depends on
    # each selected card's actual size, which varies. Only the part of the
    # layout expressed as percent can be sanity-checked up front; a bad
    # size_mib/keep_existing combination surfaces as a clean per-card error
    # from plan_partitions() instead.
    pct_sum = sum(float(p["percent"]) for p in parts if "percent" in p)
    if pct_sum > 100.5:
        raise ValueError("partition percentages must sum to 100 or less")

    # MBR only has 4 primary slots total, one of which an extended
    # container eats if any logical partition is requested. This is a
    # structural limit, not a sizing one — check it once here, independent
    # of any card's actual size, instead of letting it surface later as a
    # cryptic parted failure mid-way through a card that's already wiped.
    keep_existing = int(profile.get("keep_existing", 0) or 0)
    if profile.get("table", "gpt") != "gpt":
        needs_extended = any(p.get("type") == "logical" for p in parts)
        primary_count = sum(1 for p in parts if p.get("type") != "logical")
        used_slots = keep_existing + primary_count + (1 if needs_extended else 0)
        if used_slots > 4:
            raise ValueError(
                f"MBR supports at most 4 primary partitions (incl. the extended "
                f"container for logical ones) — this layout needs {used_slots}")

    threads = []
    for dev in devices:
        set_job(dev, state="queued", percent=0, message="Queued…", hostname=None)
        t = threading.Thread(target=partition_device, args=(dev, profile), daemon=True)
        threads.append(t)

    def runner():
        FLASH_ACTIVE.set()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        FLASH_ACTIVE.clear()

    threading.Thread(target=runner, daemon=True).start()


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
                # The app updates via package reinstall while the WebKit
                # view (or a plain browser tab) may keep running across
                # that reinstall — without this, its own cache can serve
                # a stale page indefinitely since nothing else here ever
                # sent a cache-control header to invalidate it.
                self.send_header("Cache-Control", "no-store")
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
        elif path == "/api/partition-profiles":
            self._json(load_partition_profiles())
        elif path == "/api/full-profiles":
            self._json(load_full_profiles())
        elif path == "/api/partitions":
            device = (query.get("device") or [""])[0]
            safe = {d["device"] for d in list_devices()}
            if device not in safe:
                self._json({"error": "not a removable/safe device"}, 400)
            else:
                try:
                    self._json(read_partition_table(device))
                except Exception as e:
                    # A blank card with no table yet is normal, not fatal —
                    # let the UI show "no partitions yet" instead of an alert.
                    self._json({"table": "unknown", "size_mib": 0, "partitions": [],
                                "error": str(e)[:200]})
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

        if parsed.path == "/api/partition-profiles":
            try:
                req = json.loads(body)
                name = (req.get("name") or "").strip()
                if not name:
                    raise ValueError("layout name is required")
                if not req.get("partitions"):
                    raise ValueError("layout has no partitions")
                profiles = load_partition_profiles()
                profiles[name] = {"table": req.get("table", "gpt"),
                                   "keep_existing": int(req.get("keep_existing", 0) or 0),
                                   "partitions": req["partitions"]}
                save_partition_profiles(profiles)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path == "/api/full-profiles":
            try:
                req = json.loads(body)
                name = (req.get("name") or "").strip()
                if not name:
                    raise ValueError("profile name is required")
                if req.get("config") is not None:
                    profiles = load_profiles()
                    profiles[name] = req["config"]
                    save_profiles(profiles)
                if req.get("partition") is not None:
                    part = req["partition"]
                    if not part.get("partitions"):
                        raise ValueError("layout has no partitions")
                    pprofiles = load_partition_profiles()
                    pprofiles[name] = {"table": part.get("table", "gpt"),
                                       "keep_existing": int(part.get("keep_existing", 0) or 0),
                                       "partitions": part["partitions"]}
                    save_partition_profiles(pprofiles)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path == "/api/partition":
            if os.geteuid() != 0:
                return self._json({"error": "server not running as root — restart with sudo"}, 403)
            if FLASH_ACTIVE.is_set():
                return self._json({"error": "an operation is already in progress"}, 409)
            try:
                req = json.loads(body)
                devices = req["devices"]
                profile = req["profile"]
                if not devices:
                    raise ValueError("no devices selected")
                start_partition(devices, profile)
                self._json({"ok": True, "count": len(devices)})
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
        name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
        if parsed.path == "/api/partition-profiles":
            profiles = load_partition_profiles()
            if name in profiles:
                del profiles[name]
                save_partition_profiles(profiles)
            return self._json({"ok": True})
        if parsed.path == "/api/full-profiles":
            profiles = load_profiles()
            if name in profiles:
                del profiles[name]
                save_profiles(profiles)
            pprofiles = load_partition_profiles()
            if name in pprofiles:
                del pprofiles[name]
                save_partition_profiles(pprofiles)
            return self._json({"ok": True})
        if parsed.path != "/api/profiles":
            return self._json({"error": "not found"}, 404)
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
