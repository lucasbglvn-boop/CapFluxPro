import asyncio
import base64
import hashlib
import json
import logging
import os
import platform as _platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
import ipaddress
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import bigo_engine
except ImportError:
    bigo_engine = None

# curl_cffi lets requests carry a real browser TLS fingerprint, which is what
# gets us past TikTok's WAF at all.  `requests` is the already-present fallback.
# (base64/hashlib are here for the TikTok challenge solver below, not for auth.)
try:
    from curl_cffi import requests as _curl_cffi
except ImportError:
    _curl_cffi = None

try:
    import requests as _requests
except ImportError:
    _requests = None

# ── Logging ───────────────────────────────────────────────────────────────────
# Windows consoles default to a legacy code page (cp437/cp1252), which cannot
# encode Vietnamese.  Log lines such as "[TOOL] CẢNH BÁO…" would raise
# UnicodeEncodeError on every emit, so force UTF-8 with a replace fallback
# first.  start.bat sets PYTHONUTF8=1 too, but this keeps things working when
# the server is launched directly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tool")

# ── Platform detection ────────────────────────────────────────────────────────
IS_WINDOWS = _platform.system() == "Windows"
PI_MODE = (os.environ.get("TOOL_PI_MODE") or os.environ.get("STREAMREC_PI_MODE", "0")) == "1"

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# Machine-level config lives in config.json next to main.py.  Every key has a
# default below, so deleting config.json can never stop the app from starting.
CONFIG_PATH = Path(os.environ.get("TOOL_CONFIG_PATH") or (BASE_DIR / "config.json"))
DEFAULT_CONFIG = {
    "y_storage": r"D:\y-storage",   # root folder that holds finished videos
    "host":      "127.0.0.1",
    "port":      5000,
}

_app_config_cache: dict = {}
_app_config_mtime: float = -1.0


def _load_app_config(force: bool = False) -> dict:
    """Read config.json, cached until the file's mtime changes.

    Re-reading on mtime change means the storage folder can be changed from the
    UI (or by hand) and takes effect immediately, with no restart.
    """
    global _app_config_cache, _app_config_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    if not force and _app_config_cache and mtime == _app_config_mtime:
        return _app_config_cache

    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        try:
            CONFIG_PATH.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not create config.json: %s", e)
    except Exception as e:
        logger.warning("config.json is unreadable (%s) — using defaults", e)

    _app_config_cache = cfg
    _app_config_mtime = mtime
    return cfg


def _ensure_dir(p: Path) -> Path:
    """mkdir -p that never raises — a missing drive must not kill startup."""
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Cannot create folder %s: %s", p, e)
    return p


def get_y_storage() -> Path:
    """Root folder holding finished videos (D:\\y-storage by default)."""
    raw = _load_app_config().get("y_storage") or DEFAULT_CONFIG["y_storage"]
    return Path(raw)


# Video output is grouped by platform: <y_storage>\bigo, <y_storage>\tiktok.
PLATFORM_FOLDERS = {
    "bigo":   "bigo",
    "tiktok": "tiktok",
}


def platform_folder(platform: str) -> str:
    """Map a platform label to its sub-folder under the storage root."""
    key = (platform or "").strip().lower()
    for needle, folder in PLATFORM_FOLDERS.items():
        if needle in key:
            return folder
    safe = re.sub(r"[^\w\-]", "_", key) or "other"
    return safe


def get_platform_dir(platform: str) -> Path:
    return _ensure_dir(get_y_storage() / platform_folder(platform))


def _ensure_storage_layout() -> None:
    r"""Create <y_storage>\{bigo,tiktok,_archived} so the layout is visible
    before the first recording instead of appearing out of nowhere later."""
    root = get_y_storage()
    _ensure_dir(root)
    for folder in set(PLATFORM_FOLDERS.values()) | {"_archived"}:
        _ensure_dir(root / folder)


def get_archive_dir() -> Path:
    return _ensure_dir(get_y_storage() / "_archived")


# App data (state / cookies) stays next to the code so the video folder holds
# nothing but media.  Overridable to keep old installs working.
DATA_DIR = Path(os.environ.get("TOOL_DATA_DIR") or (BASE_DIR / "data"))
_ensure_dir(DATA_DIR)
STATE_FILE   = DATA_DIR / "state.json"
COOKIES_DIR  = _ensure_dir(DATA_DIR / "cookies")

# Static files (index.html) live next to main.py
_static_dir = os.environ.get("STATIC_DIR", str(BASE_DIR))

# ── Filename helpers ─────────────────────────────────────────────────────────
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_name(value: str, fallback: str = "unknown", maxlen: int = 80) -> str:
    """Turn arbitrary text into something Windows/macOS/Linux all accept.

    Bigo nicknames routinely contain emoji, slashes and stray spaces, any of
    which would otherwise create folders or make the file unwritable.
    """
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value or ""))
    s = re.sub(r"\s+", " ", s).strip(" ._")
    s = re.sub(r"_{2,}", "_", s)
    if not s or s.upper() in _WIN_RESERVED or s.upper().split(".")[0] in _WIN_RESERVED:
        s = fallback
    return s[:maxlen] or fallback


def _unique_stem(folder: Path, stem: str) -> str:
    """Return `stem`, or `stem_2`/`stem_3`… if a file with that stem exists.

    Guards against two recordings starting in the same wall-clock second.
    """
    try:
        existing = {f.name.split(".", 1)[0] for f in folder.iterdir() if f.is_file()}
    except Exception:
        return stem
    if stem not in existing:
        return stem
    for n in range(2, 1000):
        candidate = f"{stem}_{n}"
        if candidate not in existing:
            return candidate
    return f"{stem}_{int(time.time())}"

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
START_TIME = time.time()

# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_state()
    _ensure_storage_layout()
    n = _sweep_orphan_recorders()
    if n:
        logger.warning("Killed %d orphaned recording process(es) from a previous run", n)
    logger.info("TOOL %s starting — %d channels loaded", VERSION, len(channels))
    task = asyncio.create_task(_supervised_monitor())
    yield
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    # Drain any active recordings so we don't leave zombie yt-dlp children behind
    for rec in list(recordings.values()):
        if rec.get("status") in ("recording", "starting"):
            if rec.get("pid"):
                _stop_rec(rec, force=False)
            else:
                # Recording was starting but subprocess hadn't spawned yet
                rec["status"] = "completed"
                rec["ended_at"] = time.time()
                if (ch_id := rec.get("channel_id")) and ch_id in channels:
                    channels[ch_id]["recording_id"] = None
    # Wait briefly for run_recording tasks to reap their subprocesses, so the
    # final state flush reflects post-cleanup status (and children don't
    # outlive the server and write orphaned files).
    deadline = time.time() + 8
    while time.time() < deadline and any(
        r.get("status") in ("recording", "starting") for r in recordings.values()
    ):
        await asyncio.sleep(0.25)
    _save_state_sync()
    logger.info("TOOL shutting down")


async def _supervised_monitor():
    """Keep the monitor loop alive even if a single tick raises unexpectedly."""
    while True:
        try:
            await monitor_loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("monitor_loop crashed: %s — restarting in 10s", e)
            await asyncio.sleep(10)

app = FastAPI(title="TOOL API", version=VERSION, lifespan=lifespan)


def _sweep_orphan_recorders() -> int:
    """Kill yt-dlp/ffmpeg leftovers from a previous crashed/stopped instance.
    On startup no legit recording children exist yet, so any recorder process
    whose command line references our recordings dir is an orphan."""
    killed = 0
    rec_str = str(get_y_storage())
    try:
        import psutil  # optional dependency
    except ImportError:
        psutil = None
    if psutil is not None:
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (p.info["name"] or "").lower()
                if name not in ("yt-dlp.exe", "yt-dlp", "ffmpeg", "ffmpeg.exe"):
                    continue
                cmdline = " ".join(p.info["cmdline"] or [])
                if rec_str in cmdline:
                    p.kill()
                    killed += 1
            except Exception:
                continue
    elif IS_WINDOWS:
        # Fallback: CIM query (startup-only, so the cost is fine)
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.Name -match '^(yt-dlp|ffmpeg)' } | "
                 "Select-Object ProcessId, CommandLine | ConvertTo-Json"],
                capture_output=True, text=True, timeout=20,
            )
            data = json.loads(out.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for entry in data or []:
                if rec_str in (entry.get("CommandLine") or ""):
                    subprocess.run(["taskkill", "/F", "/PID", str(entry["ProcessId"])],
                                   capture_output=True, check=False)
                    killed += 1
        except Exception as e:
            logger.debug("orphan sweep fallback failed: %s", e)
    else:
        try:
            out = subprocess.run(
                ["pgrep", "-af", "yt-dlp|ffmpeg"],
                capture_output=True, text=True, timeout=10,
            )
            for line in (out.stdout or "").splitlines():
                if rec_str in line:
                    subprocess.run(["kill", "-9", line.split(None, 1)[0]],
                                   capture_output=True, check=False)
                    killed += 1
        except Exception as e:
            logger.debug("orphan sweep fallback failed: %s", e)
    return killed


# ── Access model ─────────────────────────────────────────────────────────────
# No login.  The app binds to 127.0.0.1 by default, so only this machine can
# reach it — that is the security boundary.  If you ever change "host" in
# config.json to 0.0.0.0, anyone on your network can drive the app; put it
# behind a firewall or a reverse proxy with its own auth in that case.


# Caps how many SHORT-LIVED helper processes run at once: yt-dlp metadata/live
# probes, curl for avatars and webhooks, and the remux/clip ffmpeg jobs.
# NOTE: the actual long-running recording process does NOT go through here, so
# this does not limit how many rooms can be recorded simultaneously.
#
# Two pre-sized semaphores instead of one, because limits cannot be resized on a
# live asyncio.Semaphore.  The old single `Semaphore(3 if PI_MODE else 6)` read
# the PI_MODE env var once at import, so flipping "Chế độ máy yếu" in the UI did
# nothing — unlike every other low-power knob, which reads settings at call time.
_sem_normal = asyncio.Semaphore(6)
_sem_low_power = asyncio.Semaphore(3)

PROC_LIMIT_NORMAL = 6
PROC_LIMIT_LOW_POWER = 3


def _proc_slot() -> asyncio.Semaphore:
    """The subprocess semaphore for the currently active power mode.

    Resolved per call so the setting takes effect immediately, with no restart.
    """
    return _sem_low_power if _is_pi() else _sem_normal


def get_proc_limit() -> int:
    return PROC_LIMIT_LOW_POWER if _is_pi() else PROC_LIMIT_NORMAL

channels:   dict[str, dict] = {}
recordings: dict[str, dict] = {}
_recording_locks: dict[str, asyncio.Lock] = {}
_lock_cleanup_counter = 0


def _get_recording_lock(ch_id: str) -> asyncio.Lock:
    """Return a per-channel lock to prevent double-recording races."""
    global _lock_cleanup_counter
    if ch_id not in _recording_locks:
        _recording_locks[ch_id] = asyncio.Lock()
    # Periodically remove locks for deleted channels
    _lock_cleanup_counter += 1
    if _lock_cleanup_counter % 50 == 0:
        for cid in list(_recording_locks):
            if cid not in channels:
                del _recording_locks[cid]
    return _recording_locks[ch_id]

_disk_cache:    dict  = {}
_disk_cache_ts: float = 0

# ── Tuning helpers (Pi vs. normal) ───────────────────────────────────────────
# These read from `settings["pi_mode"]` at call time so the toggle takes
# effect immediately without a restart.

def _is_pi() -> bool:
    """True when Potato / Pi mode is active (env var OR settings toggle)."""
    return settings.get("pi_mode", PI_MODE)

def get_size_poll_interval() -> int:
    return 8 if _is_pi() else 3

def get_log_limits() -> tuple[int, int]:
    """Return (max_lines, trim_to) for the recording log buffer."""
    return (60, 30) if _is_pi() else (100, 50)

def get_frontend_poll_hint() -> int:
    return 10 if _is_pi() else 5

def get_disk_cache_ttl() -> int:
    return 60 if _is_pi() else 30

def get_ffmpeg_threads() -> str:
    return "1" if _is_pi() else "2"

# Streaming / preview constants — large chunks reduce HTTP round-trips and
# I/O syscalls, critical on Raspberry Pi / slow SD cards.
PREVIEW_RANGE_CHUNK  = 10 * 1024 * 1024   # 10 MB per HTTP range response
PREVIEW_READ_CHUNK   = 512 * 1024         # 512 KB per file read()

# Remux timeout — 5 min to handle large files on slow Pi SD cards
REMUX_TIMEOUT        = 300

# ── Debounced state persistence ──────────────────────────────────────────────
_save_state_pending = False
_save_state_task:   Optional[asyncio.Task] = None

def _schedule_save_state():
    """Debounce state saves — collapse rapid writes into one disk write."""
    global _save_state_pending, _save_state_task
    _save_state_pending = True
    if _save_state_task and not _save_state_task.done():
        return  # already scheduled
    _save_state_task = asyncio.get_running_loop().create_task(_debounced_save())

async def _debounced_save():
    """Wait for writes to settle, then flush once."""
    global _save_state_pending
    while True:
        _save_state_pending = False
        await asyncio.sleep(2)  # coalesce window
        if not _save_state_pending:
            break                # no new writes arrived — safe to flush
    _save_state_sync()

settings: dict = {
    "pi_mode":          PI_MODE,
    "monitor_interval": 120 if PI_MODE else 60,
    "default_quality":  "best",
    "default_format":   "mp4",
    "auto_convert_mp4": False,
    "delete_original":  False,
    "auto_retry":       True,
    "max_retries":      5,
    "retry_delay":      15,
    "proxy":            "",
    "cookies_file":     "",
    "extra_args":       "",
    "retention_days":   0,   # 0 = keep forever
    "max_duration":     0,   # 0 = no limit, in minutes
    "webhook_url":      "",
    "auto_stop_stalled": False,
}

# ── Cross-platform process helpers ────────────────────────────────────────────

def _subprocess_kwargs() -> dict:
    """
    Extra kwargs for asyncio.create_subprocess_exec so we can kill the whole
    process tree later.
    - Windows: CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK_EVENT to the group.
    - Unix:    setsid creates a new session; killpg kills the whole group.
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"preexec_fn": os.setsid}


def _kill_proc(pid: int, force: bool = False) -> None:
    """Terminate or kill a process (and its children) cross-platform."""
    try:
        if IS_WINDOWS:
            # First attempt graceful CTRL_BREAK_EVENT
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
            if force:
                # Force kill the entire process tree (/T) on Windows
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        else:
            sig = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass


_ytdlp_cached: Optional[str] = None


def _ytdlp_exe() -> str:
    """Absolute path to the yt-dlp binary, falling back to a bare name.

    start.bat activates the venv so `yt-dlp` normally resolves through PATH, but
    that breaks if the server is ever launched some other way.  Resolve it once
    and remember the answer.
    """
    global _ytdlp_cached
    if _ytdlp_cached:
        return _ytdlp_cached
    found = shutil.which("yt-dlp")
    if not found:
        sibling = Path(sys.executable).parent / ("yt-dlp.exe" if IS_WINDOWS else "yt-dlp")
        if sibling.exists():
            found = str(sibling)
    _ytdlp_cached = found or "yt-dlp"
    return _ytdlp_cached


def _safe_replace_file(src: Path, dst: Path, max_retries: int = 5, delay: float = 0.5) -> bool:
    """Safely replace dst with src, handling transient Windows file locks."""
    for attempt in range(max_retries):
        try:
            if IS_WINDOWS:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
            else:
                src.replace(dst)
            return True
        except (PermissionError, OSError) as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                logger.warning("Failed to replace %s with %s after %d attempts: %s", src, dst, max_retries, e)
                return False
    return False


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state_sync():
    """Write state to disk immediately (internal — prefer _save_state)."""
    saved_channels = {}
    for cid, ch in list(channels.items()):
        c = dict(ch)
        for k in ("recording_id", "is_live", "last_checked"):
            c.pop(k, None)
        # Strip sensitive credentials from disk-persisted state
        for k in ("ch_password",):
            c.pop(k, None)
        saved_channels[cid] = c
    # Persist finished recordings (completed/error) so they survive restarts
    saved_recordings = {}
    for rid, rec in list(recordings.items()):
        if rec.get("status") in ("completed", "error", "archived"):
            saved_recordings[rid] = rec
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({
                "channels": saved_channels,
                "settings": dict(settings),
                "recordings": saved_recordings,
            }, indent=2),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)


def _save_state():
    """Save state — debounced in async context, immediate otherwise."""
    try:
        asyncio.get_running_loop()  # check if we're in an async context
        _schedule_save_state()
    except RuntimeError:
        # No running event loop (startup / sync context) — write immediately
        _save_state_sync()


def _load_state():
    global settings
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for cid, ch in data.get("channels", {}).items():
            ch["recording_id"] = None
            ch["is_live"]      = False
            ch["last_checked"] = None
            # Re-detect platform for any channels saved as Unknown
            if ch.get("platform") in ("Unknown", "", None) and ch.get("url"):
                ch["platform"] = detect_platform(ch["url"])
            # Fill display_name from URL if blank
            if not ch.get("display_name") and ch.get("url"):
                ch["display_name"] = _username_from_url(ch["url"])
            if not ch.get("username") and ch.get("url"):
                ch["username"] = _username_from_url(ch["url"])
            channels[cid] = ch
        for k, v in data.get("settings", {}).items():
            if k in settings:
                settings[k] = v
        for rid, rec in data.get("recordings", {}).items():
            # Strip any in-progress state fields that no longer apply
            rec.pop("pid", None)
            rec.pop("stopping", None)
            rec.pop("speed", None)
            recordings[rid] = rec
    except Exception as e:
        logger.warning("Failed to load state: %s", e)


# ── Platform detection ────────────────────────────────────────────────────────

PLATFORM_MAP = [
    (r"youtube\.com|youtu\.be",  "YouTube"),
    (r"twitch\.tv",              "Twitch"),
    (r"tiktok\.com",             "TikTok"),
    (r"kick\.com",               "Kick"),
    (r"bilibili\.com",           "Bilibili"),
    (r"douyin\.com",             "Douyin"),
    (r"afreecatv\.com",          "Afreeca"),
    (r"sooplive\.co",            "Sooplive"),
    (r"naver\.com",              "Naver"),
    (r"weibo\.com",              "Weibo"),
    (r"huya\.com",               "Huya"),
    (r"douyu\.com",              "Douyu"),
    (r"nicovideo\.jp",           "Niconico"),
    (r"dailymotion\.com",        "Dailymotion"),
    (r"facebook\.com|fb\.watch", "Facebook"),
    (r"instagram\.com",          "Instagram"),
    (r"twitter\.com|x\.com",     "Twitter/X"),
    (r"vimeo\.com",              "Vimeo"),
    (r"rumble\.com",             "Rumble"),
    (r"stripchat\.com",          "Stripchat"),
    (r"twitcasting\.tv",         "Twitcasting"),
    (r"pandalive\.co",           "Pandalive"),
    (r"bigo\.tv|bigolive\.tv|bigoapp\.tv", "Bigo"),
    (r"chaturbate\.com",         "Chaturbate"),
    (r"cam4\.com",               "Cam4"),
    (r"myfreecams\.com",         "MyFreeCams"),
    (r"camsoda\.com",            "CamSoda"),
    (r"bongacams\.com",          "BongaCams"),
    (r"cammodels\.com",          "CamModels"),
    (r"streamate\.com",          "Streamate"),
    (r"flirt4free\.com",         "Flirt4Free"),
]

def detect_platform(url: str) -> str:
    for pattern, name in PLATFORM_MAP:
        if re.search(pattern, url, re.I):
            return name
    return "Unknown"


# ── Room-ID normalization ─────────────────────────────────────────────────────
# A room can be identified by many shapes of ID and the user should not have to
# hunt for the full URL first.  Whatever was typed is kept verbatim (it drives
# the card label and the filename); only the canonical URL gets synthesised.
#
# Accepted forms:
#   123456789, 093725729              pure digits
#   d325323, anubdn2s                 letter/digit mix
#   numwarn12345678                    long alphanumeric
#   it.352344, some.user               with a dot
#   _ptthuytien, .abc, -abc            leading . _ - (legal in Bigo IDs)
#   nguyễn_văn_a, 用户123               non-ASCII (passed through, URL-encoded)
#   @handle                            TikTok paste; @ stripped, and switches
#                                      the platform to TikTok on "auto"
#   bigo.tv/abc, www.bigo.tv/abc       protocol hidden by the browser bar
#   bigolive.tv/abc, bigoapp.tv/abc    other Bigo domains -> rewritten to bigo.tv
#   https://www.bigo.tv/abc            full URL
#
# Rejected: whitespace, control chars, and / \ ? # % & = + — characters that
# would actually break the URL.  Everything else is accepted on purpose: a real
# but unusual ID should reach the API and come back as "room not found", which
# is far clearer than a validation error.
#
# Nothing here can reach a shell.  The id becomes a URL path segment and a
# `requests` query-parameter value (percent-encoded), and filenames go through
# _safe_name() separately.
#
# Important: bigo_engine.site_id() falls back to re.search(r"(\d{6,15})", text),
# which would silently truncate "d325323" to "325323" and "it.352344" to
# "352344".  Handing it a full https://www.bigo.tv/<id> URL avoids that branch
# altogether — site_id()'s URL path returns the path segment untouched.
_ID_SHAPE_RE  = re.compile(r"^[^/\\\s?#%&=\+\x00-\x1f]{1,64}$")
_BIGO_HOST_RE = re.compile(r"(?:^|\.)(?:bigo\.tv|bigolive\.tv|bigoapp\.tv)$", re.I)
_CANON_HOST_RE = re.compile(r"(?:^|\.)bigo\.tv$", re.I)

# Hosts accepted without a scheme, e.g. "bigo.tv/abc" pasted from an address bar
# that hid the protocol.  Deliberately a short allowlist — treating any
# "looks like a domain" string as a URL would misfire on IDs like "some.user".
# Each label must be dot-separated, so "xbigo.tv" is NOT taken as a URL.
_SCHEMELESS_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9-]+\.)*(?:bigo\.tv|bigolive\.tv|bigoapp\.tv|tiktok\.com)(?:[/?#]|$)",
    re.I,
)

PLATFORM_CHOICES = ("", "bigo", "tiktok")

_ID_HELP = (
    "Chỉ được dùng chữ (kể cả có dấu), số và các ký tự . _ - , "
    "không dấu cách và không có / \\ ? # % & = + . "
    "Nếu ID có ký tự đó, hãy dán link đầy đủ."
)


def _is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", text, re.I))


def _validate_id(handle: str, label: str) -> str:
    r"""Reject handles that would be unsafe in a URL path.

    Deliberately permissive — no letter/digit is required, and non-ASCII is
    allowed.  An odd-but-real ID should reach the API and come back as "room
    not found", which is far clearer than a validation error.  Only characters
    that would actually break the URL are refused.

    Nothing here can reach a shell: the id becomes a URL path segment and a
    query parameter value passed through `requests`, which percent-encodes it.
    Filenames go through _safe_name() separately.
    """
    if not _ID_SHAPE_RE.match(handle):
        raise ValueError(f"{label} không hợp lệ. {_ID_HELP}")
    return handle


def _tiktok_handle_from_url(path: str) -> str:
    """Pull the handle out of /@handle, /@handle/live, /@handle/video/123…"""
    for seg in path.strip("/").split("/"):
        if seg.startswith("@") and len(seg) > 1:
            return seg[1:]
    return ""


def _bigo_id_from_url(url: str) -> str:
    """Path segment identifying the room, on any Bigo-owned domain.

    Mirrors bigo_engine.site_id()'s URL branch, but works for every Bigo host
    instead of only the literal "bigo.tv", and skips a leading locale segment
    such as /vi/ or /en-US/.
    """
    seg = urlparse(url).path.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in seg.strip("/").split("/") if p]
    if len(parts) > 1 and re.fullmatch(r"[a-z]{2}(?:-[A-Za-z]{2})?", parts[0]):
        parts = parts[1:]
    return parts[0] if parts else ""


def normalize_target(raw: str, platform_hint: str = "") -> tuple[str, str, str]:
    """Turn user input into (url, platform, raw_id).

    Accepts a full URL, a scheme-less URL, or a bare room ID.  A bare ID means
    Bigo by default because that is the main use case here; pass
    platform_hint="tiktok" to build a TikTok URL instead.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Vui lòng nhập ID phòng hoặc link.")

    hint = (platform_hint or "").strip().lower()

    # "bigo.tv/abc" / "www.bigo.tv/abc" — protocol hidden by the browser bar
    if not _is_url(text) and _SCHEMELESS_HOST_RE.match(text):
        text = "https://" + text

    if _is_url(text):
        host = urlparse(text).hostname or ""
        platform = detect_platform(text)
        raw_id = ""
        if _BIGO_HOST_RE.search(host):
            platform = "Bigo"
            raw_id = _bigo_id_from_url(text)
            # Rebuild against the canonical host when needed: site_id() inside
            # bigo_engine only recognises the literal "bigo.tv", so a
            # bigolive.tv / bigoapp.tv link would fall through to its
            # numeric-only regex and come back as the entire URL string.
            if raw_id and not _CANON_HOST_RE.search(host):
                text = f"https://www.bigo.tv/{raw_id}"
        elif re.search(r"tiktok\.com$", host, re.I):
            raw_id = _tiktok_handle_from_url(urlparse(text).path)
            platform = "TikTok"
        return text, platform, raw_id

    # A leading @ is the TikTok handle convention and Bigo IDs cannot contain
    # '@' at all, so it unambiguously means TikTok — switch automatically when
    # the platform was left on "auto" rather than silently building a Bigo URL
    # out of a TikTok handle.
    if text.startswith("@") and not hint:
        hint = "tiktok"
    handle = text.lstrip("@").strip()

    if hint == "tiktok":
        handle = _validate_id(handle, "Tên TikTok")
        return f"https://www.tiktok.com/@{handle}/live", "TikTok", handle

    handle = _validate_id(handle, "ID")
    return f"https://www.bigo.tv/{handle}", "Bigo", handle


# ── Metadata fetch ────────────────────────────────────────────────────────────

def _username_from_url(url: str) -> str:
    """Best-effort username extraction directly from the URL path."""
    try:
        path = urlparse(url).path.strip("/")
        # Take first non-empty path segment
        part = path.split("/")[0] if path else ""
        # Ignore generic segments
        if part and part not in ("live", "channel", "watch", "user", "c", "videos"):
            return part
    except Exception:
        pass
    return ""


async def fetch_metadata(url: str) -> dict:
    # Always extract a fallback username from the URL itself
    url_username = _username_from_url(url)

    # Bigo: native engine path (yt-dlp cannot pass Bigo's web gate).
    # Use the fast ungated user-info endpoint; the token+retry stream fetch
    # only belongs in run_recording.
    if bigo_engine and re.search(r"bigo\.tv", url, re.I):
        try:
            sid = bigo_engine.site_id(url)
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: bigo_engine.fetch_user(sid))
            if info:
                return {
                    "display_name": info.get("nickname") or url_username,
                    "username":     sid,
                    "avatar":       info.get("thumbnail") or "",
                    "thumbnail":    info.get("thumbnail") or "",
                    "is_live":      bool(info.get("alive")),
                    "stream_title": info.get("topic") or "",
                }
        except Exception as e:
            logger.debug("bigo metadata failed for %s: %s", url, e)

    # TikTok: the same WAF challenge makes yt-dlp unable to read the page, which
    # left the card blank with "@handle", no avatar and a permanent offline
    # badge.  Resolve it natively instead.
    if re.search(r"tiktok\.com", url, re.I):
        try:
            info = await tiktok_room_info(url)
        except Exception as e:
            logger.debug("tiktok metadata failed for %s: %s", url, e)
            info = None
        if info:
            return {
                "display_name": info.get("nickname") or url_username,
                "username":     info.get("handle") or url_username,
                "avatar":       info.get("avatar") or "",
                "thumbnail":    info.get("avatar") or "",
                "is_live":      bool(info.get("live")),
                "stream_title": info.get("title") or "",
            }

    try:
        async with _proc_slot():
            proc = await asyncio.create_subprocess_exec(
                _ytdlp_exe(), "--dump-single-json", "--no-download",
                "--playlist-items", "1", "--socket-timeout", "15", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)

        # Even if yt-dlp returns non-zero (offline room), stdout may still have data
        if stdout:
            try:
                data = json.loads(stdout)
            except Exception:
                data = {}
        else:
            data = {}

        thumbnails = data.get("thumbnails") or []
        thumbnail  = data.get("thumbnail") or (thumbnails[-1]["url"] if thumbnails else "")
        avatar = ""
        for t in reversed(thumbnails):
            tid = (t.get("id") or "").lower()
            if "avatar" in tid or "profile" in tid:
                avatar = t.get("url", "")
                break
        if not avatar:
            avatar = await _try_scrape_avatar(url)

        # Prefer yt-dlp data, fall back to URL-parsed username
        yt_name = data.get("uploader") or data.get("channel") or data.get("creator") or ""
        yt_user = data.get("uploader_id") or data.get("channel_id") or ""

        display_name = yt_name or url_username
        username     = yt_user or url_username

        return {
            "display_name": display_name,
            "username":     username,
            "avatar":       avatar,
            "thumbnail":    thumbnail,
            "is_live":      bool(data.get("is_live")),
            "stream_title": (data.get("title") or "").strip(),
        }
    except Exception:
        # Total failure — still return URL-derived name so card isn't blank
        if url_username:
            return {
                "display_name": url_username,
                "username":     url_username,
                "avatar":       "",
                "thumbnail":    "",
                "is_live":      False,
                "stream_title": "",
            }
        return {}


async def _try_scrape_avatar(url: str) -> str:
    try:
        async with _proc_slot():
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sL", "--max-time", "10", "-A",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if not stdout:
            return ""
        html = stdout.decode("utf-8", errors="replace")
        for pat in (r'"avatarLarger"\s*:\s*"([^"]+)"',
                    r'"avatarMedium"\s*:\s*"([^"]+)"',
                    r'"avatarThumb"\s*:\s*"([^"]+)"'):
            m = re.search(pat, html)
            if m:
                return m.group(1).replace(r'\u002F', '/')
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\'>]+)["\']',
            html, re.I,
        )
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


# ── TikTok: native live check + stream resolution ─────────────────────────────
# TikTok now fronts tiktok.com with a "Slardar" WAF challenge: a plain HTTP
# client gets a ~1 KB "Please wait…" page instead of the room.  yt-dlp's
# tiktok:live extractor has no challenge solver on that code path, so it exits
# with "The channel is not currently live" for a room that is plainly live —
# which is why a TikTok room sat at "CHỜ LIVE" forever while Bigo (which goes
# through bigo_engine, not yt-dlp) kept working.
#
# The challenge is a SHA-256 proof of work: find the integer whose digest
# matches the one embedded in the page, then echo the whole blob back as the
# `_wafchallengeid` cookie.  Solving it here restores the live check AND
# recording: the solved session can read the room state from SIGI_STATE, and
# TikTok's webcast API then hands back the real HLS url, which yt-dlp downloads
# like any other m3u8 (the CDN url itself needs no cookies).
TIKTOK_COOKIE_DOMAIN = ".tiktok.com"
TIKTOK_LIVE_STATUS   = 2      # LiveRoom…user.status: 2 = live, 4 = offline
TIKTOK_WAF_TRIES     = 2      # TikTok sometimes re-issues a challenge immediately
_TIKTOK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# One shared, cookie-carrying session: a solved challenge is reused for every
# room and re-solved automatically whenever TikTok asks again.  HTTP sessions
# are not thread-safe, so all access is serialised by this lock.
_tiktok_lock: asyncio.Lock = asyncio.Lock()
_tiktok_session = None


def _tiktok_solve_challenge(html: str) -> Optional[tuple]:
    """Solve TikTok's Slardar WAF proof of work.

    Returns (cookie_name, cookie_value), or None when the page carries no
    challenge.  The answer is the number whose SHA-256 matches the digest the
    page publishes; it is normally tiny (single digits), and the search is
    capped at 1e6 exactly like yt-dlp does for the same challenge.
    """
    m = re.search(r'<p[^>]+id="cs"[^>]+class="([^"]+)"', html)
    n = re.search(r'<p[^>]+id="wci"[^>]+class="([^"]+)"', html)
    if not m or not n:
        return None
    try:
        data     = json.loads(base64.b64decode(m.group(1) + "==="))
        expected = base64.b64decode(data["v"]["c"])
        base     = hashlib.sha256(base64.b64decode(data["v"]["a"]))
    except Exception:
        return None
    for i in range(1_000_001):
        probe = base.copy()
        probe.update(str(i).encode())
        if probe.digest() == expected:
            data["d"] = base64.b64encode(str(i).encode()).decode()
            break
    else:
        return None
    value = base64.b64encode(
        json.dumps(data, separators=(",", ":")).encode()).decode()
    return n.group(1), value


def _tiktok_fetch_once(url: str, kwargs: dict) -> str:
    """One fetch against the current session, answering a challenge if asked."""
    def _get() -> str:
        try:
            return _tiktok_session.get(url, **kwargs).text or ""
        except Exception as e:
            logger.debug("tiktok GET failed for %s: %s", url, e)
            return ""

    html = _get()
    for _ in range(TIKTOK_WAF_TRIES):
        if "SIGI_STATE" in html or "__UNIVERSAL_DATA_FOR_REHYDRATION__" in html:
            return html                                  # real page
        solved = _tiktok_solve_challenge(html)
        if not solved:
            return html                                  # not a challenge page
        try:
            _tiktok_session.cookies.set(
                solved[0], solved[1], domain=TIKTOK_COOKIE_DOMAIN, path="/")
        except Exception:
            return html
        logger.debug("Solved TikTok WAF challenge for %s", url)
        html = _get()
    return html


def _tiktok_session_get(url: str, proxy: str = "") -> str:
    """GET a tiktok.com page, answering the WAF challenge when one is served.

    Blocking — call it from a worker thread only.  Returns "" on any failure.
    """
    global _tiktok_session
    kwargs: dict = {"timeout": 25}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}

    html = ""
    for _ in range(2):
        if _tiktok_session is None:
            if _curl_cffi is not None:
                _tiktok_session = _curl_cffi.Session(impersonate="chrome")
            elif _requests is not None:
                _tiktok_session = _requests.Session()
                _tiktok_session.headers.update({"User-Agent": _TIKTOK_UA})
            else:
                return ""
        html = _tiktok_fetch_once(url, kwargs)
        if "SIGI_STATE" in html or "__UNIVERSAL_DATA_FOR_REHYDRATION__" in html:
            return html
        # Second chance on a clean session: a consumed/stale challenge cookie or
        # a blocked body is the usual cause, and a fresh ttwid clears it.
        _tiktok_session = None
        logger.debug("TikTok retry with a fresh session for %s (len=%d)",
                     url, len(html))
    return html


def _tiktok_page_state(html: str) -> Optional[dict]:
    """Live-room state out of a TikTok room page.

    Returns {"user": …, "live_room": …} or None when the page carries no such
    payload (challenge page, deleted account, unknown layout).
    """
    m = re.search(r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
        except Exception:
            data = {}
        info = ((data.get("LiveRoom") or {}).get("liveRoomUserInfo")) or {}
        if info.get("user"):
            return {"user": info["user"], "live_room": info.get("liveRoom") or {}}
    # Other page variants carry the same user object — with the same roomId /
    # status semantics — in the rehydration blob (this is the shape yt-dlp
    # reads from /@handle and from some /live responses).
    m = re.search(
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html, re.S)
    if m:
        try:
            scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__") or {}
        except Exception:
            scope = {}
        info = ((scope.get("webapp.user-detail") or {}).get("userInfo")) or {}
        if info.get("user"):
            return {"user": info["user"], "live_room": {}}
    return None


def _tiktok_hls_from_room_info(data: dict) -> str:
    """Best stream url out of a webcast room/info payload."""
    stream_url = data.get("stream_url") or {}
    if stream_url.get("hls_pull_url"):
        return stream_url["hls_pull_url"]
    # Per-quality SDK blob — the same data yt-dlp's live extractor parses.
    try:
        raw = stream_url["live_core_sdk_data"]["pull_data"]["stream_data"]
        blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
        streams = blob.get("data") or {}
        for quality in ("origin", "ORIGION", "FULL_HD1", "HD1", "uhd", "hd"):
            main = ((streams.get(quality) or {}).get("main")) or {}
            if main.get("hls"):
                return main["hls"]
    except Exception:
        pass
    for url in (stream_url.get("flv_pull_url") or {}).values():
        if url:
            return url
    return ""


def _tiktok_room_info_blocking(url: str, proxy: str = "",
                               want_stream: bool = False) -> Optional[dict]:
    """Resolve a TikTok room's live state, and its HLS url when asked.

    Returns None only when the state could not be determined at all, so callers
    can tell "definitely offline" apart from "could not tell".
    """
    handle = ""
    try:
        handle = _tiktok_handle_from_url(urlparse(url).path)
    except Exception:
        pass
    if not handle:
        m = re.search(r"tiktok\.com/@([^/?#\s]+)", url or "")
        handle = m.group(1) if m else ""
    if not handle:
        return None

    html  = _tiktok_session_get(f"https://www.tiktok.com/@{handle}/live", proxy)
    state = _tiktok_page_state(html)
    if state is None:
        if "SIGI_STATE" in html:
            # A real room page without a LiveRoom block: the account is offline
            # or does not exist.  That is a definite answer, so report it
            # instead of making the caller fall back to the broken yt-dlp path.
            return {
                "live": False, "room_id": "", "handle": handle, "nickname": "",
                "avatar": "", "title": "", "hls": "",
            }
        return None

    user      = state.get("user") or {}
    live_room = state.get("live_room") or {}
    status    = user.get("status")
    room_id   = str(user.get("roomId") or "")
    # `status` is authoritative: an offline account still reports a roomId, so
    # a non-empty roomId on its own does NOT mean the room is live.
    is_live   = (status == TIKTOK_LIVE_STATUS) if status is not None else bool(room_id)
    info = {
        "live":     is_live,
        "room_id":  room_id,
        "handle":   user.get("uniqueId") or handle,
        "nickname": user.get("nickname") or "",
        "avatar":   (user.get("avatarLarger") or user.get("avatarMedium")
                     or user.get("avatarThumb") or ""),
        "title":    (live_room.get("title") or "").strip(),
        "hls":      "",
    }
    if not (want_stream and is_live and room_id):
        return info

    kwargs: dict = {"params": {"aid": "1988", "room_id": room_id}, "timeout": 25}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    try:
        data = (_tiktok_session.get(
            "https://webcast.tiktok.com/webcast/room/info/", **kwargs).json()
            or {}).get("data") or {}
    except Exception as e:
        logger.debug("tiktok room/info failed for %s: %s", handle, e)
        return info
    if data:
        if data.get("status") is not None:
            try:
                info["live"] = int(data["status"]) == TIKTOK_LIVE_STATUS
            except (TypeError, ValueError):
                pass
        info["hls"] = _tiktok_hls_from_room_info(data)
        if not info["title"]:
            info["title"] = (data.get("title") or "").strip()
    return info


async def tiktok_room_info(url: str, proxy: str = "",
                           want_stream: bool = False) -> Optional[dict]:
    """Async front end for the blocking TikTok resolver above."""
    async with _tiktok_lock:
        return await asyncio.get_running_loop().run_in_executor(
            None, _tiktok_room_info_blocking, url, proxy, want_stream)


async def _check_chaturbate_live(url: str, proxy: str = "") -> Optional[bool]:
    """Scrape Chaturbate page directly — returns True/False/None (None = inconclusive)."""
    try:
        curl_cmd = [
            "curl", "-sL", "--max-time", "10", "-A",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        if proxy:
            curl_cmd += ["--proxy", proxy]
        curl_cmd.append(url)
        async with _proc_slot():
            proc = await asyncio.create_subprocess_exec(
                *curl_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if not stdout:
            return None
        html = stdout.decode("utf-8", errors="replace")
        # Chaturbate embeds room status in the page
        if '"room_status": "public"' in html or '"room_status":"public"' in html:
            return True
        if 'offline' in html.lower() and 'room_status' in html:
            return False
        return None
    except Exception:
        return None


async def check_is_live(url: str, proxy: str = "") -> bool:
    # Bigo: native engine live check (fast, no yt-dlp)
    if bigo_engine and re.search(r"bigo\.tv", url, re.I):
        try:
            sid = bigo_engine.site_id(url)
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: bigo_engine.check_live(sid, proxy=proxy))
        except Exception as e:
            logger.debug("bigo live check failed for %s: %s", url, e)
            return False

    # For Chaturbate, try a fast HTTP scrape first before invoking yt-dlp
    if re.search(r"chaturbate\.com", url, re.I):
        result = await _check_chaturbate_live(url, proxy=proxy)
        if result is not None:
            return result

    # TikTok: the room page is behind a WAF challenge that yt-dlp cannot solve,
    # so ask natively first — otherwise a live room is reported as offline.
    if re.search(r"tiktok\.com", url, re.I):
        try:
            info = await tiktok_room_info(url, proxy=proxy)
        except Exception as e:
            logger.debug("tiktok live check failed for %s: %s", url, e)
            info = None
        if info is not None:
            return bool(info.get("live"))
        logger.debug("tiktok live check inconclusive for %s — yt-dlp fallback", url)

    cmd = [_ytdlp_exe(), "--simulate", "--no-warnings",
           "--socket-timeout", "20", "--playlist-items", "1"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)
    proc = None
    try:
        # Hold the semaphore across the full wait so the process cap is real.
        async with _proc_slot():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=35)
            except asyncio.TimeoutError:
                _kill_proc(proc.pid, force=True)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                return False
        return proc.returncode == 0
    except Exception as e:
        logger.debug("check_is_live error for %s: %s", url, e)
        if proc and proc.returncode is None:
            try:
                _kill_proc(proc.pid, force=True)
            except Exception:
                pass
        return False


# ── Recording ─────────────────────────────────────────────────────────────────

async def _start_recording_for_channel(ch_id: str) -> Optional[str]:
    # Per-channel lock prevents double-recording from concurrent calls
    lock = _get_recording_lock(ch_id)
    async with lock:
        ch = channels.get(ch_id)
        if not ch:
            return None
        existing = ch.get("recording_id")
        if existing and existing in recordings and recordings[existing]["status"] in ("recording", "starting"):
            return None
        rec_id = str(uuid.uuid4())[:8]
        recordings[rec_id] = {
            "id": rec_id, "channel_id": ch_id,
            "url": ch["url"], "platform": ch["platform"],
            "quality": ch.get("quality") or settings["default_quality"],
            "format":  ch.get("format")  or settings["default_format"],
            "status": "starting", "created_at": time.time(),
            "started_at": None, "ended_at": None,
            "bytes": 0, "speed": None,
            "filepath": None, "filename": None,
            "log": [], "stopping": False, "auto": False,
            "is_favorite": False,
        }
        channels[ch_id]["recording_id"] = rec_id
        asyncio.create_task(run_recording(rec_id))
        return rec_id


# ── Bigo stream resolution ────────────────────────────────────────────────────
# bigo_engine builds a token inside fetch_stream() and then reuses that single
# token for all ten of its internal retries.  When Bigo rejects the token
# ("needLogin") every one of those retries is futile — the token has to be
# re-issued, which only a fresh fetch_stream() call does.
#
# Seen in production: five consecutive monitor ticks (~60s apart) each burned
# the full ~7s retry budget and failed with needLogin, then the very next tick
# succeeded — the signature of a stale/limited token, not an offline room.
#
# The engine also holds ONE module-global requests.Session, and requests.Session
# is documented as not thread-safe.  Checking several rooms at once fired
# concurrent fetch_stream() calls into executor threads sharing that session;
# the lock below serialises them and removes the race.
_bigo_lock = asyncio.Lock()

BIGO_ATTEMPTS    = 3
BIGO_RETRY_DELAY = 3.0   # seconds; multiplied by the attempt number


async def _bigo_fetch_stream(sid: str, proxy: str,
                             rec: Optional[dict] = None) -> dict:
    """Resolve a Bigo room's HLS url, re-issuing the token when necessary.

    Retries only when the room reports itself live but no stream came back —
    that is the token/gate case.  A room reporting alive=False is genuinely
    offline and is returned straight away so nothing is wasted on it.
    """
    loop = asyncio.get_running_loop()
    info: dict = {}
    async with _bigo_lock:
        for attempt in range(1, BIGO_ATTEMPTS + 1):
            try:
                info = await loop.run_in_executor(
                    None, lambda: bigo_engine.fetch_stream(sid, proxy=proxy)) or {}
            except Exception as e:
                logger.warning("Bigo fetch_stream attempt %d failed for %s: %s",
                               attempt, sid, e)
                info = {}

            if info.get("hls_src"):
                if attempt > 1:
                    logger.info("Bigo %s: got stream on attempt %d", sid, attempt)
                return info
            if info and not info.get("alive"):
                logger.info("Bigo %s: room reports offline", sid)
                return info          # genuinely offline — retrying cannot help
            if attempt < BIGO_ATTEMPTS:
                wait = BIGO_RETRY_DELAY * attempt
                if rec is not None:
                    rec.setdefault("log", []).append(
                        f"[TOOL] Bigo chưa cấp link stream (lần {attempt}/{BIGO_ATTEMPTS})"
                        f" — lấy token mới và thử lại sau {wait:.0f}s…"
                    )
                logger.info("Bigo %s: no hls on attempt %d, retrying in %.0fs",
                            sid, attempt, wait)
                await asyncio.sleep(wait)
    return info


async def run_recording(rec_id: str):
    rec = recordings[rec_id]
    ch  = channels.get(rec["channel_id"], {})
    quality      = rec.get("quality") or settings["default_quality"]
    fmt          = rec.get("format")  or settings["default_format"]
    url          = rec["url"]
    platform_raw = rec.get("platform") or "Unknown"
    platform     = platform_raw.lower()

    # Bigo: native engine — resolve HLS via bigo_engine, record with ffmpeg directly.
    # (yt-dlp cannot pass Bigo's web anti-bot gate.)
    _bigo_hls = None
    _engine_nickname = ""
    if bigo_engine and platform == "bigo":
        _bigo_proxy = ch.get("proxy") or settings.get("proxy", "")
        try:
            sid = bigo_engine.site_id(url)
            info = await _bigo_fetch_stream(sid, _bigo_proxy, rec)
            _bigo_hls = info.get("hls_src") or ""
            if info.get("nickname"):
                # Upstream assigned this to `username`, which was then
                # overwritten by the ch.get("display_name") line below — so the
                # nickname the engine just resolved was thrown away.  Keep it in
                # its own variable and let it win.
                _engine_nickname = info["nickname"]
                # keep channel card fresh too
                if (ch_id := rec.get("channel_id")) and ch_id in channels:
                    channels[ch_id]["display_name"] = info["nickname"]
                    if info.get("thumbnail"):
                        channels[ch_id]["thumbnail"] = info["thumbnail"]
                    if info.get("topic"):
                        channels[ch_id]["stream_title"] = info["topic"]
            if not _bigo_hls:
                rec["status"] = "error"
                rec["ended_at"] = time.time()
                if info.get("alive"):
                    # The room IS live, so the failure was the token/gate, not
                    # the broadcaster going offline.  Say so plainly instead of
                    # the old catch-all "offline or gated".
                    rec["error"] = (
                        f"Bigo: phòng đang live nhưng không lấy được link stream "
                        f"sau {BIGO_ATTEMPTS} lần thử (token bị từ chối). "
                        f"Thử quay lại sau ít phút."
                    )
                else:
                    rec["error"] = "Bigo: phòng đang offline."
                logger.warning(
                    "Bigo recording %s aborted — no HLS url (alive=%s, attempts=%d)",
                    rec_id, info.get("alive"), BIGO_ATTEMPTS)
                if ch_id := rec.get("channel_id"):
                    ch_entry = channels.get(ch_id)
                    if ch_entry and ch_entry.get("recording_id") == rec_id:
                        ch_entry["recording_id"] = None
                        ch_entry["is_live"] = bool(info.get("alive"))
                _save_state()
                return
        except Exception as e:
            rec["status"] = "error"
            rec["ended_at"] = time.time()
            rec["error"] = f"Bigo engine error: {e}"
            logger.warning("Bigo recording %s engine failure: %s", rec_id, e)
            if ch_id := rec.get("channel_id"):
                ch_entry = channels.get(ch_id)
                if ch_entry and ch_entry.get("recording_id") == rec_id:
                    ch_entry["recording_id"] = None
                    ch_entry["is_live"] = False
            _save_state()
            return

    # TikTok: the room page (and therefore yt-dlp's live extractor) sits behind
    # a WAF challenge, so resolve the real HLS url natively and record that —
    # the CDN url needs no cookies and yt-dlp treats it as a plain m3u8.
    if platform == "tiktok":
        _tt_proxy = ch.get("proxy") or settings.get("proxy", "")
        try:
            _tt_info = await tiktok_room_info(url, proxy=_tt_proxy, want_stream=True)
        except Exception as e:
            _tt_info = None
            logger.warning("TikTok resolve failed for %s: %s", rec_id, e)
        _tt_hls = (_tt_info or {}).get("hls") or ""
        if _tt_info and _tt_info.get("nickname"):
            if (ch_id := rec.get("channel_id")) and ch_id in channels:
                channels[ch_id]["display_name"] = _tt_info["nickname"]
                if _tt_info.get("title"):
                    channels[ch_id]["stream_title"] = _tt_info["title"]
        if not _tt_hls:
            rec["status"]   = "error"
            rec["ended_at"] = time.time()
            if _tt_info is None:
                rec["error"] = ("TikTok: không đọc được trạng thái phòng "
                                "(TikTok chặn truy cập). Thử quay lại sau.")
            elif _tt_info.get("live"):
                rec["error"] = ("TikTok: phòng đang live nhưng không lấy được "
                                "link stream. Thử quay lại sau ít phút.")
            else:
                rec["error"] = "TikTok: phòng đang offline."
            logger.warning("TikTok recording %s aborted — no HLS url (info=%s)",
                           rec_id, bool(_tt_info))
            if ch_id := rec.get("channel_id"):
                ch_entry = channels.get(ch_id)
                if ch_entry and ch_entry.get("recording_id") == rec_id:
                    ch_entry["recording_id"] = None
                    ch_entry["is_live"] = bool(_tt_info and _tt_info.get("live"))
            _save_state()
            return
        url = _tt_hls

    # Filename = <username>_<YYYY-MM-DD>_<HH-MM-SS>, saved flat under
    # <y_storage>\<platform>.  The nickname the engine just resolved wins over
    # whatever the card was created with; raw_id is the last resort so a file is
    # never left called "unknown".
    username = (
        _engine_nickname
        or ch.get("display_name")
        or ch.get("username")
        or ch.get("raw_id")
        or rec_id
    )
    safe_user = _safe_name(username, fallback=rec_id, maxlen=60)
    # Local time — the filename is user-facing, so it should match the clock.
    now       = datetime.now()
    date_str  = now.strftime("%Y-%m-%d")
    time_str  = now.strftime("%H-%M-%S")
    rec_dir   = get_platform_dir(platform_raw)
    stem      = _unique_stem(
        rec_dir, _safe_name(f"{safe_user}_{date_str}_{time_str}", fallback=rec_id)
    )
    output_path = rec_dir / f"{stem}.%(ext)s"

    _no_live_from_start = (
        "twitch", "tiktok", "kick", "stripchat", "bigo", "pandalive",
        "chaturbate", "cam4", "myfreecams", "camsoda", "bongacams",
        "cammodels", "streamate", "flirt4free",
    )
    _cam_platforms = (
        "tiktok", "kick", "stripchat", "bigo", "pandalive",
        "chaturbate", "cam4", "myfreecams", "camsoda", "bongacams",
        "cammodels", "streamate", "flirt4free",
    )

    # Cam/HLS sites use numeric format IDs — "best" keyword alone fails.
    # Build a fallback chain that works for both cam sites and regular streams.
    _height_re = re.match(r'^(\d+)p?$', quality)
    if quality in ("best", ""):
        if platform in _cam_platforms:
            effective_quality = "bestvideo+bestaudio/best"
        else:
            effective_quality = "bestvideo+bestaudio/bestvideo/best"
    elif quality == "bestvideo+bestaudio":
        effective_quality = "bestvideo+bestaudio/best"
    elif _height_re and platform in _cam_platforms:
        # e.g. "720p" or "720" on cam sites — use height filter with fallback
        h = _height_re.group(1)
        effective_quality = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/bestvideo+bestaudio/best"
    else:
        # User specified explicit quality (1080p, 720p, etc.) for non-cam platforms
        effective_quality = quality

    if _bigo_hls:
        # Route through the local descrambling proxy (Bigo scrambles segment
        # headers server-side via EXT-X-BIGO-WEB-PROTECTION).
        _bigo_proxy = ch.get("proxy") or settings.get("proxy", "")
        try:
            _bigo_hls = bigo_engine.proxy_hls(_bigo_hls, proxy=_bigo_proxy)
        except Exception as e:
            logger.debug("bigo proxy_hls failed, using raw url: %s", e)
        out_file = str(output_path).replace("%(ext)s", "mp4" if fmt == "mp4" else "ts")
        cmd = ["ffmpeg", "-y",
               "-headers", f"Referer: https://www.bigo.tv/\r\nUser-Agent: {bigo_engine.UA}\r\n",
               "-i", _bigo_hls,
               "-c", "copy",
               "-timeout", "30", "-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_delay_max", "10"]
        max_dur = int(ch.get("max_duration") or settings.get("max_duration", 0) or 0)
        if max_dur > 0:
            cmd += ["-t", str(max_dur * 60)]
        if fmt == "mp4":
            cmd += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
        else:
            cmd += ["-f", "mpegts"]
        cmd += [out_file]

    # Limit ffmpeg thread count — critical for Pi / low-power CPUs
    ffmpeg_threads = get_ffmpeg_threads()

    if not _bigo_hls:
        cmd = [_ytdlp_exe(), "--no-part"]
        if platform not in _no_live_from_start:
            cmd += ["--live-from-start", "--hls-use-mpegts"]

        cmd += [
            "--retries", "infinite", "--fragment-retries", "infinite",
            "--retry-sleep", "5", "--socket-timeout", "30",
            "--no-warnings", "--newline",
            "--concurrent-fragments", "1",
            # force-fixup is MP4-specific; for mkv/ts it warns and can abort on
            # recoverable errors.  "warn" is the safe mode for other containers.
            "--fixup", "force" if fmt == "mp4" else "warn",
            # -stats + -loglevel info override yt-dlp's default ffmpeg silence so
            # live HLS recordings produce visible progress in the log viewer.
            "--downloader-args",
            f"ffmpeg:-threads {ffmpeg_threads} -fflags +genpts+discardcorrupt -stats -loglevel info",
            "-f", effective_quality,
            "--merge-output-format", fmt,
            # NOTE: no --print here — it implies --quiet and silences the entire
            # log for live recordings.  The final filepath is resolved via the
            # output glob after the process exits.
            "--progress",
        ]
        # On Pi / low-memory systems, cap the download buffer to reduce RAM usage
        if _is_pi():
            cmd += ["--buffer-size", "32K"]
        # Chaturbate (and similar HLS cam sites) have audio segments that start
        # exactly 1 second ahead of video — delay audio by 1s to compensate.
        if platform in _cam_platforms:
            cmd += ["--postprocessor-args", "ffmpeg:-c:v copy -c:a aac -af adelay=1000|1000"]

        # Proxy (channel > global)
        proxy = ch.get("proxy") or settings.get("proxy", "")
        if proxy:
            cmd += ["--proxy", proxy]

        # Cookies file (channel > global)
        cf_name = ch.get("cookies_file") or settings.get("cookies_file", "")
        if cf_name:
            cf = Path(cf_name) if Path(cf_name).is_absolute() else COOKIES_DIR / cf_name
            if cf.exists():
                cmd += ["--cookies", str(cf)]

        # Username / password for age-restricted sites
        ch_user = ch.get("ch_username", "")
        ch_pass = ch.get("ch_password", "")
        if ch_user:
            cmd += ["--username", ch_user]
        if ch_pass:
            cmd += ["--password", ch_pass]

        # Extra yt-dlp args (channel > global) — hardened against injection
        extra = ch.get("extra_args") or settings.get("extra_args", "")
        if extra:
            try:
                parts = shlex.split(extra, posix=not IS_WINDOWS)
                # Block dangerous yt-dlp flags that could execute arbitrary commands or manipulate filesystem
                _DANGEROUS_FLAGS = {
                    "--exec", "-X", "--exec-before-download", "--config-location", "--config-locations",
                    "--print", "--print-to-file", "--output-na-placeholder",
                    "--downloader", "--external-downloader", "--downloader-args", "--external-downloader-args",
                    "--postprocessor-args", "--ppa", "--exec-after-download",
                    "--proxy", "--cookies", "--cookies-from-browser",
                    "--ffmpeg-location", "--prefer-ffmpeg", "-P", "--paths",
                    "-a", "--batch-file", "--load-info-json", "--plugin-dirs",
                    "-o", "--output", "--alias",
                }
                for part in parts:
                    flag = part.split("=")[0].strip() if "=" in part else part.strip()
                    flag_lower = flag.lower()
                    if (
                        flag_lower in _DANGEROUS_FLAGS
                        or flag_lower.startswith("--exec")
                        or flag_lower.startswith("--config")
                        or flag_lower.startswith("--paths")
                        or flag_lower.startswith("-P")
                    ):
                        logger.warning("Blocked dangerous extra_args flag: %s", flag)
                        raise ValueError(f"Blocked dangerous yt-dlp flag: {flag}")
                cmd += parts
            except ValueError:
                raise  # re-raise our own validation errors
            except Exception as e:
                logger.warning("Failed to parse extra_args %r: %s", extra, e)

        cmd += ["-o", str(output_path), url]

    rec["status"]     = "recording"
    rec["started_at"] = time.time()
    rec["log"]        = []
    size_task         = None
    proc              = None
    duration_task     = None
    file_captured     = False  # pre-init so `finally` can read it even if we fail early
    logger.info("Recording started: %s for %s (%s)", rec_id, username, url)

    # If a stop/kill was requested before the task got scheduled, bail out now.
    if rec.get("stopping"):
        rec["status"]   = "completed"
        rec["ended_at"] = time.time()
        if (ch_id := rec.get("channel_id")) and ch_id in channels:
            channels[ch_id]["recording_id"] = None
        _save_state()
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_subprocess_kwargs(),
        )
        rec["pid"] = proc.pid
        # Race: stop was requested between starting and pid assignment — kill now.
        if rec.get("stopping"):
            _kill_proc(proc.pid, force=False)

        async def _poll_size():
            last_bytes = 0
            last_time = time.time()
            last_change = time.time()
            while True:
                await asyncio.sleep(get_size_poll_interval())
                for f in rec_dir.glob(f"{stem}.*"):
                    try:
                        sz = f.stat().st_size
                        if sz > 0:
                            now = time.time()
                            delta_bytes = sz - last_bytes
                            delta_time = now - last_time
                            if delta_time > 0 and last_bytes > 0 and delta_bytes >= 0:
                                bps = delta_bytes / delta_time
                                if bps >= 1024**2:
                                    rec["speed"] = f"{bps/1024**2:.1f}MiB/s"
                                elif bps >= 1024:
                                    rec["speed"] = f"{bps/1024:.1f}KiB/s"
                                else:
                                    rec["speed"] = f"{bps:.0f}B/s"
                            if sz != last_bytes:
                                last_change = now
                            last_bytes = sz
                            last_time = now
                            rec["bytes"] = sz
                    except Exception:
                        pass
                    break
                # Stalled detection: no data written for 120s (240s in Pi mode)
                stall_timeout = 240 if _is_pi() else 120
                if time.time() - last_change > stall_timeout:
                    rec["log"].append(
                        f"[TOOL] CẢNH BÁO — không nhận được dữ liệu trong {stall_timeout}s. "
                        "Có thể stream đã kết thúc."
                    )
                    if settings.get("auto_stop_stalled", False) and not rec.get("stopping"):
                        rec["log"].append(
                            "[TOOL] Đã bật tự dừng khi treo — đang dừng quay."
                        )
                        _stop_rec(rec, force=False)

        size_task = asyncio.create_task(_poll_size())

        # Max duration enforcement
        max_dur_mins = int(ch.get("max_duration") or settings.get("max_duration", 0))
        if max_dur_mins > 0:
            async def _enforce_duration():
                await asyncio.sleep(max_dur_mins * 60)
                if rec.get("status") == "recording" and not rec.get("stopping"):
                    rec["log"].append(
                        f"[TOOL] Đã đạt giới hạn thời lượng ({max_dur_mins} phút) — dừng êm"
                    )
                    _stop_rec(rec, force=False)
            duration_task = asyncio.create_task(_enforce_duration())

        rec_dir_str = str(rec_dir)
        async for raw_line in proc.stdout:
            # ffmpeg uses \r to overwrite progress; split on both \r and \n
            for line in raw_line.decode("utf-8", errors="replace").replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                rec["log"].append(line)
                log_max, log_trim = get_log_limits()
                if len(rec["log"]) > log_max:
                    rec["log"] = rec["log"][-log_trim:]
                # ffmpeg progress line (HLS via ffmpeg downloader):
                # "frame= 49 fps=0.0 size=  256KiB time=00:00:01 bitrate=1279.8kbits/s speed= 3.2x"
                if "frame=" in line and "bitrate=" in line:
                    m_size = re.search(r"size=\s*(\d+\.?\d*)\s*(GiB|MiB|KiB|B)\b", line)
                    if m_size:
                        v = float(m_size.group(1))
                        u = {"GiB": 1024**3, "MiB": 1024**2, "KiB": 1024, "B": 1}[m_size.group(2)]
                        rec["bytes"] = int(v * u)
                    m_br = re.search(r"bitrate=\s*(\d+\.?\d*\s*\w+bits/s)", line)
                    if m_br:
                        rec["speed"] = m_br.group(1)
                # yt-dlp download progress line (HTTP downloads)
                elif "[download]" in line or "[hlsnative]" in line:
                    m = re.search(r"(\d+\.?\d*)\s*(GiB|MiB|KiB|B)\b", line)
                    if m:
                        v = float(m.group(1))
                        u = {"GiB": 1024**3, "MiB": 1024**2, "KiB": 1024, "B": 1}[m.group(2)]
                        rec["bytes"] = int(v * u)
                    m2 = re.search(r"(\d+\.?\d*\s*(?:GiB|MiB|KiB|B)/s)", line)
                    if m2:
                        rec["speed"] = m2.group(1)
                # Detect final filepath printed by --print after_move:filepath
                # (exclude ffmpeg banner/progress noise that also contains the dir)
                if (not line.startswith("[") and not line.startswith("ERROR")
                        and not line.startswith("Output")
                        and len(line) > 4 and rec_dir_str in line):
                    p = Path(line.strip("'"))
                    if p.suffix and p.is_absolute():
                        rec["filepath"] = str(p)
                        rec["filename"] = p.name

        await proc.wait()
        rc = proc.returncode
        # Resolve filepath now (before status check) in case --print didn't fire
        if not rec.get("filepath"):
            for f in rec_dir.glob(f"{stem}.*"):
                rec["filepath"] = str(f)
                rec["filename"] = f.name
                break
        # Treat as completed if manually stopped OR if a file with real content was captured
        # (yt-dlp exits non-zero when broadcaster goes offline, even after a full capture)
        file_captured = bool(rec.get("filepath") and Path(rec["filepath"]).exists()
                             and Path(rec["filepath"]).stat().st_size > 0)
        rec["status"] = "completed" if (rc == 0 or rec.get("stopping") or file_captured) else "error"
        if rc != 0 and not rec.get("stopping") and not file_captured:
            rec["error"] = f"Exit code {rc}"
            logger.warning("Recording %s failed with exit code %d", rec_id, rc)
        else:
            logger.info("Recording %s completed", rec_id)

    except asyncio.CancelledError:
        rec["status"]   = "completed" if rec.get("stopping") else "error"
        if not rec.get("stopping"):
            rec["error"] = "Cancelled"
        raise
    except Exception as e:
        rec["status"] = "error"
        rec["error"]  = str(e)
        logger.error("Recording %s exception: %s", rec_id, e)

    finally:
        if size_task:
            size_task.cancel()
        if duration_task:
            duration_task.cancel()
        # Make sure we never leave an orphaned yt-dlp subprocess running
        if proc is not None and proc.returncode is None:
            try:
                _kill_proc(proc.pid, force=False)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    _kill_proc(proc.pid, force=True)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                pass
        rec["ended_at"] = time.time()
        rec.pop("pid", None)

        # Fall back to file glob if yt-dlp didn't print the path
        if not rec.get("filepath"):
            for f in rec_dir.glob(f"{stem}.*"):
                rec["filepath"] = str(f)
                rec["filename"] = f.name
                break

        if fp := rec.get("filepath"):
            try:
                rec["bytes"] = Path(fp).stat().st_size
            except Exception:
                pass

        # Remux to fix containers and move moov atom to file start for
        # smooth playback (faststart).  Run on ALL completed recordings —
        # not just manual stops — so files play without lag on any device.
        fp = rec.get("filepath", "")
        if fp and Path(fp).exists():
            suffix    = Path(fp).suffix
            fixed_path = str(Path(fp).with_name(Path(fp).stem + "_fixed" + suffix))
            try:
                async with _proc_slot():
                    fix_proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-i", fp, "-c", "copy", "-movflags", "+faststart",
                        "-threads", ffmpeg_threads, fixed_path, "-y",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(fix_proc.wait(), timeout=REMUX_TIMEOUT)
                if fix_proc.returncode == 0 and Path(fixed_path).exists():
                    if _safe_replace_file(Path(fixed_path), Path(fp)):
                        try:
                            rec["bytes"] = Path(fp).stat().st_size
                        except Exception:
                            pass
                    else:
                        Path(fixed_path).unlink(missing_ok=True)
                else:
                    Path(fixed_path).unlink(missing_ok=True)
            except Exception:
                try:
                    Path(fixed_path).unlink(missing_ok=True)
                except Exception:
                    pass

        # Optional MP4 conversion
        auto_convert = ch.get("auto_convert_mp4", settings["auto_convert_mp4"])
        delete_orig  = ch.get("delete_original",  settings["delete_original"])
        fp = rec.get("filepath", "")
        if auto_convert and fp and not fp.endswith(".mp4") and Path(fp).exists():
            mp4_path = str(Path(fp).with_suffix(".mp4"))
            try:
                async with _proc_slot():
                    conv = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-i", fp, "-c", "copy",
                        "-movflags", "+faststart",
                        "-threads", ffmpeg_threads,
                        mp4_path, "-y",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(conv.wait(), timeout=REMUX_TIMEOUT)
                if conv.returncode == 0:
                    if delete_orig:
                        Path(fp).unlink(missing_ok=True)
                    rec["filepath"] = mp4_path
                    rec["filename"] = Path(mp4_path).name
                    try:
                        rec["bytes"] = Path(mp4_path).stat().st_size
                    except Exception:
                        pass
            except Exception:
                pass

        # Only clear the channel's recording pointer if it still points at US —
        # a subsequent Record click could have already started a new rec_id.
        if ch_id := rec.get("channel_id"):
            ch_entry = channels.get(ch_id)
            if ch_entry and ch_entry.get("recording_id") == rec_id:
                ch_entry["recording_id"] = None
                ch_entry["is_live"]      = False

        _save_state()

        if ch_id := rec.get("channel_id"):
            _prune_channel_recordings(ch_id)

        # Webhook notification on recording completion
        if rec.get("status") in ("completed", "error"):
            ch_data = channels.get(rec.get("channel_id", ""), {})
            asyncio.create_task(_send_webhook("recording_complete", {
                "recording_id": rec.get("id"),
                "channel_id": rec.get("channel_id"),
                "name": ch_data.get("display_name") or ch_data.get("username", ""),
                "stream_title": ch_data.get("stream_title", ""),
                "status": rec.get("status"),
                "filename": rec.get("filename", ""),
                "bytes": rec.get("bytes", 0),
                "error": rec.get("error", ""),
                "platform": rec.get("platform", ""),
                "avatar": ch_data.get("avatar", ""),
                "thumbnail": ch_data.get("thumbnail", ""),
                "duration_seconds": int((rec.get("ended_at") or time.time()) - (rec.get("started_at") or time.time())),
            }))

        # Auto-retry on unexpected disconnect
        retry_ch_id = rec.get("channel_id")
        # Don't retry if stream ran for more than 30s — that's a natural end, not a crash
        run_duration = (rec.get("ended_at") or time.time()) - (rec.get("started_at") or time.time())
        natural_end  = run_duration > 30 and file_captured
        should_retry = (
            not rec.get("stopping")
            and not natural_end
            and rec.get("status") == "error"
            and settings.get("auto_retry", True)
            and retry_ch_id
            and retry_ch_id in channels
            and channels[retry_ch_id].get("monitoring", False)
        )
        if should_retry:
            attempt    = rec.get("_retry_attempt", 0) + 1
            max_retries = settings.get("max_retries", 5)
            delay      = settings.get("retry_delay", 15)
            if attempt <= max_retries:
                rec["log"].append(
                    f"[TOOL] Mất kết nối. Thử lại sau {delay}s "
                    f"(lần {attempt}/{max_retries})…"
                )
                await asyncio.sleep(delay)
                if retry_ch_id in channels and not channels[retry_ch_id].get("recording_id"):
                    new_id = await _start_recording_for_channel(retry_ch_id)
                    if new_id:
                        recordings[new_id]["_retry_attempt"] = attempt
                        recordings[new_id]["auto"] = True


# ── Monitor loop ──────────────────────────────────────────────────────────────

async def _send_webhook(event: str, payload: dict):
    """Send a JSON POST to the configured webhook URL (with native Discord embeds if Discord)."""
    url = settings.get("webhook_url", "").strip()
    if not url:
        return
    # SSRF guard: only http(s), never internal hosts (webhook URL is set by
    # the admin, but validate anyway since it auto-fires on every recording).
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.warning("Webhook skipped — invalid URL scheme: %r", url[:60])
        return
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254"):
        logger.warning("Webhook skipped — internal host: %r", hostname)
        return
    try:
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            # Build clean Discord embed
            color_map = {
                "stream_live": 0x34D399,          # Green
                "recording_complete": 0x818CF8,   # Indigo/Purple
            }
            title_map = {
                "stream_live": f"🔴 Đang live: {payload.get('name') or 'Phòng'}",
                "recording_complete": f"📼 Quay xong: {payload.get('name') or 'Phòng'}",
            }
            embed = {
                "title": title_map.get(event, f"TOOL: {event}"),
                "color": color_map.get(event, 0x818CF8),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if payload.get("url"):
                embed["url"] = payload["url"]
            if payload.get("stream_title"):
                embed["description"] = payload["stream_title"]
            fields = []
            if payload.get("platform"):
                fields.append({"name": "Platform", "value": payload["platform"], "inline": True})
            if payload.get("status"):
                fields.append({"name": "Status", "value": payload["status"].capitalize(), "inline": True})
            if payload.get("duration_seconds"):
                mins = payload["duration_seconds"] // 60
                secs = payload["duration_seconds"] % 60
                fields.append({"name": "Duration", "value": f"{mins}m {secs}s", "inline": True})
            if payload.get("bytes"):
                b = payload["bytes"]
                sz = f"{b/(1024**3):.2f} GiB" if b >= 1024**3 else f"{b/(1024**2):.1f} MiB"
                fields.append({"name": "Size", "value": sz, "inline": True})
            if fields:
                embed["fields"] = fields
            if payload.get("thumbnail"):
                embed["image"] = {"url": payload["thumbnail"]}
            elif payload.get("avatar"):
                embed["thumbnail"] = {"url": payload["avatar"]}
            post_data = {"embeds": [embed]}
        else:
            post_data = {"event": event, **payload}

        async with _proc_slot():
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
                "-d", json.dumps(post_data, default=str),
                "--max-time", "10", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=12)
    except Exception:
        pass


def _prune_channel_recordings(ch_id: str):
    """Enforce max_streams per channel: keep the N newest recordings (never deleting favorites)."""
    ch = channels.get(ch_id)
    if not ch:
        return
    max_s = int(ch.get("max_streams", 0) or 0)
    if max_s <= 0:
        return
    # Find all completed/error/archived recordings for this channel, newest first
    ch_recs = [
        (rid, r) for rid, r in recordings.items()
        if r.get("channel_id") == ch_id and r.get("status") in ("completed", "error", "archived")
    ]
    ch_recs.sort(key=lambda x: x[1].get("created_at", 0), reverse=True)
    
    kept = 0
    changed = False
    for rid, r in ch_recs:
        if r.get("is_favorite"):
            continue  # Favorites are shielded from max_streams pruning
        kept += 1
        if kept > max_s:
            if fp := r.get("filepath"):
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass
            recordings.pop(rid, None)
            changed = True
    if changed:
        _save_state()


async def _run_retention_cleanup():
    """Delete recordings older than retention_days (shielding favorites)."""
    days = int(settings.get("retention_days", 0))
    if days <= 0:
        return
    cutoff = time.time() - (days * 86400)
    for rec_id, rec in list(recordings.items()):
        if rec.get("status") not in ("completed", "error"):
            continue
        if rec.get("is_favorite"):
            continue  # Favorites are shielded from retention cleanup
        if rec.get("created_at", time.time()) > cutoff:
            continue
        if fp := rec.get("filepath"):
            try:
                Path(fp).unlink(missing_ok=True)
            except Exception:
                pass
        recordings.pop(rec_id, None)
    _save_state()

async def _check_one_channel(ch_id: str) -> None:
    """Poll one watched room; start recording as soon as it is live.

    Only rooms the user explicitly started watching are polled — a room that is
    merely displayed is never contacted.  While watching, the room is re-checked
    every `monitor_interval` seconds, so a room that goes offline now is picked
    up again the moment it comes back.

    Per-channel error handling so one bad URL can't take down the tick.
    """
    ch = channels.get(ch_id)
    if not ch or not ch.get("monitoring", False):
        return
    existing = ch.get("recording_id")
    if existing and existing in recordings:
        r = recordings[existing]
        if r.get("status") in ("recording", "starting"):
            # Still recording — no need to re-check, just mark live
            if ch_id in channels:
                channels[ch_id]["is_live"]      = True
                channels[ch_id]["last_checked"] = time.time()
            return

    proxy = ch.get("proxy") or settings.get("proxy", "")
    try:
        if re.search(r"tiktok\.com", ch["url"], re.I):
            # TikTok: one native call answers both questions — the room page we
            # fetch for the live check also carries the fresh nickname, avatar
            # and title, so a card saved before the WAF work-around was known
            # ("@handle", no avatar) repairs itself here.
            info = await tiktok_room_info(ch["url"], proxy=proxy)
            if info is None:
                # Could not read the room at all — fall back rather than
                # reporting a live room as offline.
                is_live = await check_is_live(ch["url"], proxy=proxy)
            else:
                is_live = bool(info.get("live"))
                if ch_id in channels:
                    if info.get("nickname"):
                        channels[ch_id]["display_name"] = info["nickname"]
                    if info.get("handle"):
                        channels[ch_id]["username"] = info["handle"]
                    if info.get("avatar"):
                        channels[ch_id]["avatar"] = info["avatar"]
                    if info.get("title"):
                        channels[ch_id]["stream_title"] = info["title"]
        else:
            is_live = await check_is_live(ch["url"], proxy=proxy)
    except Exception as e:
        logger.warning("Live check failed for %s (%s): %s", ch_id, ch.get("url"), e)
        return

    # Channel could have been deleted while we were awaiting
    if ch_id not in channels:
        return
    channels[ch_id]["is_live"]      = is_live
    channels[ch_id]["last_checked"] = time.time()

    if is_live and not channels[ch_id].get("recording_id"):
        logger.info("Watched room %s is live — starting recording", ch_id)
        try:
            rec_id = await _start_recording_for_channel(ch_id)
        except Exception as e:
            logger.exception("Failed to start auto-record for %s: %s", ch_id, e)
            return
        if rec_id and rec_id in recordings:
            recordings[rec_id]["auto"] = True
            asyncio.create_task(_send_webhook("stream_live", {
                "channel_id": ch_id,
                "name": channels[ch_id].get("display_name", ""),
                "url": channels[ch_id].get("url", ""),
                "platform": channels[ch_id].get("platform", ""),
                "recording_id": rec_id,
            }))


async def monitor_loop():
    _last_retention_check = 0
    while True:
        interval = max(5, int(settings.get("monitor_interval", 60)))
        await asyncio.sleep(interval)
        # Only rooms the user started watching are polled.  A room that is only
        # displayed is never contacted, so launching the app does not reach out
        # to every saved room.
        ids = [cid for cid, c in channels.items() if c.get("monitoring", False)]
        if not ids:
            continue
        logger.debug("Monitor tick — checking %d watched channels", len(ids))
        # Run per-channel checks concurrently; gather never raises because
        # each coroutine traps its own exceptions.
        await asyncio.gather(
            *(_check_one_channel(cid) for cid in ids),
            return_exceptions=True,
        )
        # Retention cleanup runs once per hour
        now = time.time()
        if now - _last_retention_check > 3600:
            _last_retention_check = now
            await _run_retention_cleanup()


# ── Request models ────────────────────────────────────────────────────────────

class AddChannelRequest(BaseModel):
    url:              str
    platform_hint:    str  = ""   # "" = auto, "bigo", "tiktok"
    quality:          str  = ""
    format:           str  = ""
    # Watching is opt-in: a newly added room is displayed but not polled until
    # the user presses "Bắt đầu theo dõi".
    monitoring:       bool = False
    auto_convert_mp4: bool = False
    delete_original:  bool = False
    record_now:       bool = False
    proxy:            str  = ""
    cookies_file:     str  = ""
    username:         str  = ""
    password:         str  = ""
    extra_args:       str  = ""
    max_duration:     int  = 0
    max_streams:      int  = 0
    notes:            str  = ""

class UpdateChannelRequest(BaseModel):
    monitoring:       Optional[bool] = None
    quality:          Optional[str]  = None
    format:           Optional[str]  = None
    auto_convert_mp4: Optional[bool] = None
    delete_original:  Optional[bool] = None
    proxy:            Optional[str]  = None
    cookies_file:     Optional[str]  = None
    username:         Optional[str]  = None
    password:         Optional[str]  = None
    extra_args:       Optional[str]  = None
    max_duration:     Optional[int]  = None
    max_streams:      Optional[int]  = None
    notes:            Optional[str]  = None

class CutRecordingRequest(BaseModel):
    start_time:       float  # in seconds
    end_time:         float  # in seconds
    title:            Optional[str] = None

class UpdateSettingsRequest(BaseModel):
    pi_mode:          Optional[bool] = None
    monitor_interval: Optional[int]  = None
    default_quality:  Optional[str]  = None
    default_format:   Optional[str]  = None
    auto_convert_mp4: Optional[bool] = None
    delete_original:  Optional[bool] = None
    auto_retry:       Optional[bool] = None
    max_retries:      Optional[int]  = None
    retry_delay:      Optional[int]  = None
    proxy:            Optional[str]  = None
    cookies_file:     Optional[str]  = None
    extra_args:       Optional[str]  = None
    retention_days:   Optional[int]  = None
    max_duration:     Optional[int]  = None
    webhook_url:      Optional[str]  = None
    auto_stop_stalled: Optional[bool] = None

class ReorderRequest(BaseModel):
    order: list[str]

class BulkActionRequest(BaseModel):
    ids:    list[str]
    action: str  # "record" | "stop" | "delete"

class BulkEditRequest(BaseModel):
    ids:     list[str]
    quality: Optional[str]  = None
    format:  Optional[str]  = None

class ImportRequest(BaseModel):
    channels: dict = {}
    settings: dict = {}
    merge:    bool = True


# ── Helper: stop a recording by rec dict ─────────────────────────────────────

def _stop_rec(rec: dict, force: bool = False):
    rec["stopping"] = True
    if pid := rec.get("pid"):
        _kill_proc(pid, force=force)


# ── Channel endpoints ─────────────────────────────────────────────────────────

@app.post("/api/channels")
async def add_channel(req: AddChannelRequest):
    # Accept a bare room ID just as happily as a full URL — normalise first,
    # then run every SSRF check against the URL we actually intend to fetch.
    try:
        url, platform, raw_id = normalize_target(req.url, req.platform_hint)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Validate URL — must be http/https to prevent SSRF
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Invalid URL — must start with http:// or https://")
    # Block private/internal IPs to prevent SSRF
    _BLOCKED_HOSTS = (
        "localhost", "127.0.0.1", "::1", "0.0.0.0",
        "169.254.169.254",  # AWS metadata
    )
    hostname = parsed.hostname or ""
    if not hostname:
        raise HTTPException(400, "Invalid URL — missing host")
    if hostname in _BLOCKED_HOSTS:
        raise HTTPException(400, "URL rejected: private/internal host")
    
    # Resolve domain names to verify target IPs are not internal / private.
    # getaddrinfo is blocking — run it in a worker thread so the event loop
    # doesn't stall on slow/unresponsive DNS servers.
    try:
        resolved_addrs = await asyncio.get_running_loop().getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in resolved_addrs:
            ip_str = sockaddr[0]
            addr = ipaddress.ip_address(ip_str)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
                raise HTTPException(400, f"URL rejected: host resolves to private/internal IP ({ip_str})")
    except socket.gaierror:
        # DNS resolution failure will be rejected early
        raise HTTPException(400, "URL rejected: unable to resolve host")

    # Reject duplicates — adding the same room twice would double-record it
    for existing in channels.values():
        if existing.get("url") == url:
            raise HTTPException(400, "Phòng này đã có trong danh sách rồi.")

    ch_id = str(uuid.uuid4())[:8]
    ch = {
        "id": ch_id, "url": url, "platform": platform,
        "raw_id":           raw_id or platform_folder(platform),
        "quality": req.quality, "format": req.format,
        "monitoring":       req.monitoring,
        "auto_convert_mp4": req.auto_convert_mp4,
        "delete_original":  req.delete_original,
        "proxy":            req.proxy,
        "cookies_file":     req.cookies_file,
        "ch_username":      req.username,
        "ch_password":      req.password,
        "extra_args":       req.extra_args,
        "max_duration":     req.max_duration,
        "max_streams":      req.max_streams,
        "notes":            req.notes,
        "created_at":  time.time(),
        "display_name": "", "username": "", "avatar": "", "thumbnail": "",
        "stream_title": "",
        "is_live": False, "last_checked": None, "recording_id": None,
    }
    channels[ch_id] = ch

    async def _fetch_and_record():
        meta = await fetch_metadata(url)
        if meta and ch_id in channels:
            channels[ch_id].update({
                "display_name": meta.get("display_name", ""),
                "username":     meta.get("username", ""),
                "avatar":       meta.get("avatar", ""),
                "thumbnail":    meta.get("thumbnail", ""),
                "stream_title": meta.get("stream_title", ""),
                "is_live":      meta.get("is_live", False),
                "last_checked": time.time(),
            })
            _save_state()

    asyncio.create_task(_fetch_and_record())
    if req.record_now:
        asyncio.create_task(_start_recording_for_channel(ch_id))
    _save_state()
    logger.info("Channel added: %s (%s) [%s]", ch_id, platform, url)
    return {"id": ch_id, "platform": platform, "raw_id": ch["raw_id"]}


@app.get("/api/channels")
async def list_channels():
    result = []
    for ch in channels.values():
        c = dict(ch)
        rec_id = c.get("recording_id")
        if rec_id and rec_id in recordings:
            r = recordings[rec_id]
            c["rec_status"]  = r["status"]
            c["rec_bytes"]   = r.get("bytes", 0)
            c["rec_speed"]   = r.get("speed")
            c["rec_started"] = r.get("started_at")
            c["rec_id"]      = rec_id
        else:
            c["rec_status"] = None; c["rec_bytes"] = 0
            c["rec_speed"]  = None; c["rec_started"] = None; c["rec_id"] = None
            # Expose the most recent finished session so the UI log viewer
            # can show its log after a recording ends.
            last = max(
                (r for r in recordings.values()
                 if r.get("channel_id") == ch.get("id")
                 and r.get("status") in ("completed", "error")),
                key=lambda r: r.get("ended_at") or r.get("created_at", 0),
                default=None,
            )
            if last:
                c["last_rec_id"] = last["id"]
        result.append(c)
    return sorted(result, key=lambda x: (x.get("sort_order", 9999), -x["created_at"]))


@app.patch("/api/channels/{ch_id}")
async def update_channel(ch_id: str, req: UpdateChannelRequest):
    if ch_id not in channels:
        raise HTTPException(404, "Not found")
    for field, val in req.model_dump(exclude_none=True).items():
        # username/password map to ch_username/ch_password to avoid clash with metadata field
        if field == "username":
            channels[ch_id]["ch_username"] = val
        elif field == "password":
            channels[ch_id]["ch_password"] = val
        else:
            channels[ch_id][field] = val
    _save_state()
    return channels[ch_id]


@app.post("/api/channels/{ch_id}/watch")
async def watch_channel(ch_id: str):
    """Start watching a room: poll it for live status and record when it goes live.

    Kicks off an immediate check instead of making the user wait up to a full
    interval, so pressing the button gives instant feedback — and records right
    away if the room is already live.
    """
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    if ch.get("monitoring") and ch.get("is_live"):
        # Already watching and known live — make sure a recording is running
        # rather than just re-checking.
        existing = ch.get("recording_id")
        if not (existing and existing in recordings
                and recordings[existing]["status"] in ("recording", "starting")):
            asyncio.create_task(_start_recording_for_channel(ch_id))
        return {"ok": True, "monitoring": True, "is_live": True}

    ch["monitoring"] = True
    _save_state()
    asyncio.create_task(_check_one_channel(ch_id))
    logger.info("Watching started for %s", ch_id)
    return {"ok": True, "monitoring": True}


@app.post("/api/channels/{ch_id}/unwatch")
async def unwatch_channel(ch_id: str):
    """Stop watching a room, ending any recording it currently has."""
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    ch["monitoring"] = False
    rec_id = ch.get("recording_id")
    if rec_id and rec_id in recordings and recordings[rec_id]["status"] in ("recording", "starting"):
        _stop_rec(recordings[rec_id], force=False)
    _save_state()
    logger.info("Watching stopped for %s", ch_id)
    return {"ok": True, "monitoring": False}


@app.post("/api/channels/{ch_id}/record")
async def record_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    existing = ch.get("recording_id")
    if existing and existing in recordings and recordings[existing]["status"] in ("recording", "starting"):
        raise HTTPException(400, "Already recording")
    # Recording implies watching — otherwise the room would be recorded once and
    # silently dropped from the monitor loop.
    ch["monitoring"] = True
    rec_id = await _start_recording_for_channel(ch_id)
    if not rec_id:
        raise HTTPException(400, "Already recording")
    return {"rec_id": rec_id}


@app.post("/api/channels/{ch_id}/stop")
async def stop_channel(ch_id: str):
    """Stop recording AND stop watching, returning the card to its idle state.

    Stopping only the process used to leave monitoring=True, so the very next
    monitor tick saw a live room with no active recording and started a fresh
    one — which is why the button had to be pressed two or three times before
    it actually stuck.  Clearing monitoring here makes one press final, and
    matches what "Dừng quay" implies: the card goes back to "Chưa theo dõi".
    """
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Not found")
    # Clear monitoring first: the monitor loop reads it, so this must be set
    # before the process is reaped or a tick could sneak a new recording in.
    ch["monitoring"] = False
    rec_id = ch.get("recording_id")
    if rec_id and rec_id in recordings:
        _stop_rec(recordings[rec_id], force=False)
    _save_state()
    logger.info("Recording stopped and watching disabled for %s", ch_id)
    return {"ok": True, "monitoring": False}


@app.post("/api/channels/{ch_id}/kill")
async def kill_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Not found")
    rec_id = ch.get("recording_id")
    if not rec_id or rec_id not in recordings:
        raise HTTPException(400, "Not recording")
    rec = recordings[rec_id]
    _stop_rec(rec, force=True)
    # Give run_recording's finally block a moment to reap the process tree,
    # otherwise a monitor tick can start a new recording while the old
    # yt-dlp/ffmpeg processes are still dying.
    for _ in range(20):
        await asyncio.sleep(0.25)
        if rec.get("status") not in ("recording", "starting"):
            break
    if rec.get("status") in ("recording", "starting"):
        rec["status"]   = "completed"
        rec["ended_at"] = time.time()
        if ch_id in channels and channels[ch_id].get("recording_id") == rec_id:
            channels[ch_id]["recording_id"] = None
    return {"ok": True}


@app.post("/api/channels/{ch_id}/refresh")
async def refresh_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Not found")
    # Re-detect platform in case it was saved as Unknown
    if ch.get("platform") == "Unknown" or not ch.get("platform"):
        channels[ch_id]["platform"] = detect_platform(ch["url"])
    meta = await fetch_metadata(ch["url"])
    if meta:
        channels[ch_id].update({
            "display_name": meta.get("display_name") or ch.get("display_name") or "",
            "username":     meta.get("username")     or ch.get("username") or "",
            "avatar":       meta.get("avatar")       or ch.get("avatar", ""),
            "thumbnail":    meta.get("thumbnail")    or ch.get("thumbnail", ""),
            "stream_title": meta.get("stream_title") or ch.get("stream_title", ""),
            "is_live":      meta.get("is_live", False),
            "last_checked": time.time(),
        })
    _save_state()
    return channels[ch_id]


@app.delete("/api/channels/{ch_id}")
async def delete_channel(ch_id: str):
    ch = channels.pop(ch_id, None)
    if not ch:
        raise HTTPException(404, "Not found")
    rec_id = ch.get("recording_id")
    if rec_id and rec_id in recordings:
        _stop_rec(recordings[rec_id], force=False)
        # Brief wait for recording cleanup so the finally block in run_recording
        # can still find the channel entry to clear recording_id
        await asyncio.sleep(0.5)
    _save_state()
    logger.info("Channel deleted: %s", ch_id)
    return {"ok": True}


@app.post("/api/channels/reorder")
async def reorder_channels(req: ReorderRequest):
    for i, ch_id in enumerate(req.order):
        if ch_id in channels:
            channels[ch_id]["sort_order"] = i
    _save_state()
    return {"ok": True}


@app.post("/api/channels/bulk")
async def bulk_action(req: BulkActionRequest):
    results = []
    for ch_id in req.ids:
        try:
            if req.action == "record":
                rid = await _start_recording_for_channel(ch_id)
                results.append({"id": ch_id, "ok": bool(rid)})
            elif req.action == "stop":
                ch = channels.get(ch_id)
                if ch and ch.get("recording_id"):
                    rec = recordings.get(ch["recording_id"])
                    if rec:
                        _stop_rec(rec, force=False)
                results.append({"id": ch_id, "ok": True})
            elif req.action == "delete":
                ch = channels.pop(ch_id, None)
                if ch:
                    rec_id = ch.get("recording_id")
                    if rec_id and rec_id in recordings:
                        _stop_rec(recordings[rec_id], force=False)
                results.append({"id": ch_id, "ok": True})
        except Exception:
            results.append({"id": ch_id, "ok": False})
    _save_state()
    return {"results": results}


@app.post("/api/channels/bulk-edit")
async def bulk_edit(req: BulkEditRequest):
    updated = 0
    for ch_id in req.ids:
        if ch_id not in channels:
            continue
        if req.quality is not None:
            channels[ch_id]["quality"] = req.quality
        if req.format is not None:
            channels[ch_id]["format"] = req.format
        updated += 1
    _save_state()
    return {"ok": True, "updated": updated}


# ── Recording endpoints ───────────────────────────────────────────────────────

@app.get("/api/recordings")
async def list_recordings():
    result = []
    for rec in recordings.values():
        r = dict(rec)
        r.pop("log", None)
        r.pop("pid", None)
        # Include channel info for richer frontend display
        if ch_id := rec.get("channel_id"):
            ch = channels.get(ch_id, {})
            r["channel_thumbnail"]  = ch.get("thumbnail", "")
            r["channel_display_name"] = ch.get("display_name", "")
            r["channel_avatar"]     = ch.get("avatar", "")
        result.append(r)
    return sorted(result, key=lambda x: x["created_at"], reverse=True)


@app.get("/api/recordings/{rec_id}/log")
async def get_log(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    return {"log": rec.get("log", [])}


@app.get("/api/download/{rec_id}")
async def download_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    fp = rec.get("filepath")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(fp, filename=rec.get("filename", f"{rec_id}.mp4"))


@app.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    # Stop active recording process before deleting
    if rec.get("status") in ("recording", "starting"):
        _stop_rec(rec, force=True)
        # Wait for run_recording's finally block to release the file handles
        for _ in range(20):
            await asyncio.sleep(0.25)
            if rec.get("status") not in ("recording", "starting"):
                break
        if rec.get("status") in ("recording", "starting"):
            rec["status"] = "completed"
            rec["ended_at"] = time.time()
            if (ch_id := rec.get("channel_id")) and ch_id in channels \
                    and channels[ch_id].get("recording_id") == rec_id:
                channels[ch_id]["recording_id"] = None
    recordings.pop(rec_id, None)
    if fp := rec.get("filepath"):
        try:
            Path(fp).unlink(missing_ok=True)
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/recordings/{rec_id}/archive")
async def archive_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    if rec.get("status") not in ("completed", "error"):
        raise HTTPException(400, "Only completed recordings can be archived")
    fp = rec.get("filepath")
    if fp and Path(fp).exists():
        try:
            dest = get_archive_dir() / Path(fp).name
            shutil.move(fp, str(dest))
            rec["filepath"] = str(dest)
            rec["filename"] = dest.name
        except Exception as e:
            raise HTTPException(500, f"Failed to archive: {e}")
    rec["status"] = "archived"
    _save_state()
    return {"ok": True}


@app.post("/api/recordings/{rec_id}/restore")
async def restore_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    if rec.get("status") != "archived":
        raise HTTPException(400, "Only archived recordings can be restored")
    fp = rec.get("filepath")
    if fp and Path(fp).exists():
        try:
            ch_id = rec.get("channel_id")
            ch = channels.get(ch_id, {}) if ch_id else {}
            plat = ch.get("platform") or rec.get("platform") or "Unknown"
            rec_dir = get_platform_dir(plat)
            dest = rec_dir / Path(fp).name
            shutil.move(fp, str(dest))
            rec["filepath"] = str(dest)
            rec["filename"] = dest.name
        except Exception as e:
            raise HTTPException(500, f"Failed to restore: {e}")
    rec["status"] = "completed"
    _save_state()
    return {"ok": True}


@app.get("/api/preview/{rec_id}")
async def preview_recording(rec_id: str, request: Request):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    fp = rec.get("filepath")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "File not found")
    file_path = Path(fp)
    file_size = file_path.stat().st_size
    suffix    = file_path.suffix.lower()
    content_type = (
        "video/mp4"        if suffix == ".mp4" else
        "video/x-matroska" if suffix == ".mkv" else
        "video/mp2t"
    )

    range_header = request.headers.get("range")
    if range_header:
        try:
            spec = range_header.replace("bytes=", "").strip()
            first, _, last = spec.partition("-")
            if first:
                start = int(first)
                end   = min(int(last) if last else start + PREVIEW_RANGE_CHUNK - 1,
                            file_size - 1)
            else:
                # suffix range: bytes=-N -> last N bytes
                n = int(last)
                start = max(0, file_size - n)
                end   = file_size - 1
        except (ValueError, TypeError):
            raise HTTPException(416, "Invalid Range header")
        if start > end or start >= file_size:
            raise HTTPException(416, "Requested range not satisfiable")
        length = end - start + 1

        def iterfile():
            f = open(file_path, "rb")
            try:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(PREVIEW_READ_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                f.close()

        return StreamingResponse(
            iterfile(), status_code=206, media_type=content_type,
            headers={
                "Content-Range":  f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges":  "bytes",
                "Content-Length": str(length),
                "Cache-Control":  "private, max-age=3600",
                "Connection":     "keep-alive",
            },
        )
    return FileResponse(fp, media_type=content_type, headers={
        "Accept-Ranges": "bytes",
        "Cache-Control":  "private, max-age=3600",
    })


@app.post("/api/recordings/{rec_id}/favorite")
async def toggle_favorite(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    rec["is_favorite"] = not rec.get("is_favorite", False)
    _save_state()
    return {"ok": True, "is_favorite": rec["is_favorite"]}


@app.post("/api/recordings/{rec_id}/cut")
async def cut_recording(rec_id: str, req: CutRecordingRequest):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    fp = rec.get("filepath")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "Source file not found")
    if req.start_time < 0 or req.end_time <= req.start_time:
        raise HTTPException(400, "Invalid start or end time")

    src_path = Path(fp)
    clip_id  = str(uuid.uuid4())[:8]
    clip_name = (req.title or f"{src_path.stem}_clip_{int(req.start_time)}-{int(req.end_time)}").strip()
    safe_clip = re.sub(r'[^\w\-]', '_', clip_name)
    dst_path  = src_path.parent / f"{safe_clip}_{clip_id}{src_path.suffix}"

    duration = req.end_time - req.start_time
    ffmpeg_threads = get_ffmpeg_threads()

    try:
        async with _proc_slot():
            # Fast stream-copy cut without re-encoding
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-ss", str(req.start_time), "-i", str(src_path),
                "-t", str(duration), "-c", "copy", "-movflags", "+faststart",
                "-threads", ffmpeg_threads, str(dst_path), "-y",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=120)

        if proc.returncode != 0 or not dst_path.exists() or dst_path.stat().st_size == 0:
            if dst_path.exists():
                dst_path.unlink(missing_ok=True)
            raise HTTPException(500, "Failed to cut video segment")

        clip_rec_id = str(uuid.uuid4())[:8]
        recordings[clip_rec_id] = {
            "id": clip_rec_id,
            "channel_id": rec.get("channel_id"),
            "url": rec.get("url"),
            "platform": rec.get("platform"),
            "quality": rec.get("quality"),
            "format": rec.get("format"),
            "status": "completed",
            "created_at": time.time(),
            "started_at": rec.get("started_at"),
            "ended_at": time.time(),
            "bytes": dst_path.stat().st_size,
            "speed": None,
            "filepath": str(dst_path),
            "filename": dst_path.name,
            "log": [f"[TOOL] Đã tạo clip từ {rec_id} ({int(duration)}s)"],
            "stopping": False,
            "auto": False,
            "is_favorite": True,  # Auto-favorite user clips
        }
        _save_state()
        return {"ok": True, "clip_id": clip_rec_id, "filename": dst_path.name, "bytes": dst_path.stat().st_size}

    except asyncio.TimeoutError:
        if dst_path.exists():
            dst_path.unlink(missing_ok=True)
        raise HTTPException(504, "Video cutting timed out")
    except HTTPException:
        raise
    except Exception as e:
        if dst_path.exists():
            dst_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Error creating clip: {e}")


# ── Settings endpoints ────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return settings


@app.patch("/api/settings")
async def update_settings(req: UpdateSettingsRequest):
    for field, val in req.model_dump(exclude_none=True).items():
        settings[field] = val
    _save_state()
    return settings


class AppConfigRequest(BaseModel):
    y_storage: Optional[str] = None
    host:      Optional[str] = None
    port:      Optional[int] = None


@app.get("/api/config")
async def get_app_config():
    """Machine-level config (storage folder / host / port) from config.json."""
    cfg  = _load_app_config(force=True)
    _ensure_storage_layout()
    root = get_y_storage()
    try:
        free = shutil.disk_usage(str(_ensure_dir(root))).free
    except Exception:
        free = None
    return {
        **cfg,
        "y_storage": str(root),
        "platform_dirs": {k: str(root / v) for k, v in PLATFORM_FOLDERS.items()},
        "free": free,
    }


@app.patch("/api/config")
async def update_app_config(req: AppConfigRequest):
    cfg = _load_app_config(force=True)
    updates = req.model_dump(exclude_none=True)

    if "y_storage" in updates:
        raw = str(updates["y_storage"]).strip()
        if not raw:
            raise HTTPException(400, "Thư mục lưu không được để trống.")
        try:
            target = Path(raw).expanduser()
            updates["y_storage"] = str(target.absolute())
        except Exception:
            raise HTTPException(400, "Đường dẫn không hợp lệ.")

    cfg.update(updates)
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"Không ghi được config.json: {e}")

    _load_app_config(force=True)
    # Create the per-platform folders straight away so the change is visible.
    _ensure_storage_layout()
    logger.info("Storage folder changed to %s", cfg.get("y_storage"))
    return {"ok": True, **cfg}


# Set by desktop.py: callable(initial_dir: str) -> chosen folder or "".
FOLDER_PICKER_HOOK = None


@app.post("/api/folder-picker")
async def pick_storage_folder():
    """Open the native Windows folder dialog (tkinter) and return the choice.

    Only meaningful when the server runs on this machine — which it does, since
    the default bind address is 127.0.0.1.
    """
    # Desktop shell (desktop.py) installs a native dialog here; it is the only
    # option inside a frozen .exe, where sys.executable is the app itself.
    if FOLDER_PICKER_HOOK is not None:
        try:
            folder = await asyncio.get_running_loop().run_in_executor(
                None, FOLDER_PICKER_HOOK, str(get_y_storage()))
        except Exception as e:
            raise HTTPException(500, f"Không mở được hộp thoại chọn thư mục: {e}")
        if not folder:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "folder": folder}
    if IS_WINDOWS:
        script = BASE_DIR / "pick_folder.py"
        if not script.exists():
            raise HTTPException(500, "Thiếu file pick_folder.py")
        try:
            initial = str(get_y_storage())
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), initial,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            folder = stdout.decode("utf-8", errors="replace").strip()
            if not folder:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "folder": folder}
        except asyncio.TimeoutError:
            return {"ok": False, "cancelled": True}
        except Exception as e:
            raise HTTPException(500, f"Không mở được hộp thoại chọn thư mục: {e}")
    raise HTTPException(400, "Chọn thư mục bằng hộp thoại chỉ hỗ trợ trên Windows.")


@app.post("/api/settings/test-webhook")
async def test_webhook():
    url = settings.get("webhook_url", "").strip()
    if not url:
        raise HTTPException(400, "No webhook URL configured in settings")
    test_payload = {
        "name": "TOOL Test",
        "url": "http://127.0.0.1:5000",
        "platform": "Bigo",
        "stream_title": "🔔 Đây là thông báo thử từ TOOL! Kết nối webhook đã hoạt động.",
        "status": "online",
        "duration_seconds": 125,
        "bytes": 52428800,
    }
    await _send_webhook("stream_live", test_payload)
    return {"ok": True, "message": "Test notification sent"}


# ── Cookies endpoints ─────────────────────────────────────────────────────────

MAX_COOKIES_SIZE = 5 * 1024 * 1024   # 5 MiB — cookies files are tiny in practice


@app.post("/api/cookies/upload")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_COOKIES_SIZE:
        raise HTTPException(
            413, f"File too large — max {MAX_COOKIES_SIZE // 1024} KiB"
        )
    safe_name = re.sub(r'[^\w\-\.]', '_', file.filename or "cookies.txt")
    dest      = COOKIES_DIR / safe_name
    dest.write_bytes(content)
    return {"ok": True, "filename": safe_name, "size": len(content)}


@app.get("/api/cookies")
async def list_cookies():
    files = []
    for f in sorted(COOKIES_DIR.iterdir()):
        if f.is_file():
            files.append({"filename": f.name, "size": f.stat().st_size})
    return files


@app.delete("/api/cookies/{filename}")
async def delete_cookies(filename: str):
    safe = re.sub(r'[^\w\-\.]', '_', filename)
    dest = COOKIES_DIR / safe
    if not dest.exists():
        raise HTTPException(404, "Not found")
    dest.unlink()
    return {"ok": True}


# ── Platforms endpoint ────────────────────────────────────────────────────────

SUPPORTED_PLATFORMS = [
    {"name": "YouTube",     "domain": "youtube.com / youtu.be",  "color": "#FF0000", "emoji": "▶",  "notes": "Livestreams, premieres, scheduled streams"},
    {"name": "Twitch",      "domain": "twitch.tv",               "color": "#6441a5", "emoji": "🎮", "notes": "Live channels, VOD replay"},
    {"name": "TikTok",      "domain": "tiktok.com",              "color": "#010101", "emoji": "♪",  "notes": "Live streams"},
    {"name": "Kick",        "domain": "kick.com",                "color": "#53fc18", "emoji": "⚡", "notes": "Live channels"},
    {"name": "Bilibili",    "domain": "bilibili.com",            "color": "#00a1d6", "emoji": "B",  "notes": "Live streams"},
    {"name": "Douyin",      "domain": "douyin.com",              "color": "#010101", "emoji": "♪",  "notes": "Chinese TikTok live"},
    {"name": "Huya",        "domain": "huya.com",                "color": "#ff8c00", "emoji": "🎮", "notes": "Chinese game streaming"},
    {"name": "Douyu",       "domain": "douyu.com",               "color": "#ff6600", "emoji": "🐟", "notes": "Chinese game streaming"},
    {"name": "Afreeca TV",  "domain": "afreecatv.com",           "color": "#006aff", "emoji": "🔵", "notes": "Korean streaming platform"},
    {"name": "Sooplive",    "domain": "sooplive.co.kr",          "color": "#1a6cff", "emoji": "🔵", "notes": "Korean streaming (formerly AfreecaTV)"},
    {"name": "Naver",       "domain": "naver.com",               "color": "#03c75a", "emoji": "N",  "notes": "Korean portal live streams"},
    {"name": "Niconico",    "domain": "nicovideo.jp",            "color": "#888888", "emoji": "⚪", "notes": "Japanese video / live"},
    {"name": "Facebook",    "domain": "facebook.com / fb.watch", "color": "#1877f2", "emoji": "f",  "notes": "Live videos — use cookies for age-restricted"},
    {"name": "Instagram",   "domain": "instagram.com",           "color": "#e1306c", "emoji": "📷", "notes": "Live, Reels — requires cookies for private"},
    {"name": "Twitter/X",   "domain": "twitter.com / x.com",    "color": "#1da1f2", "emoji": "🐦", "notes": "Spaces, live video"},
    {"name": "Vimeo",       "domain": "vimeo.com",               "color": "#1ab7ea", "emoji": "V",  "notes": "Live events"},
    {"name": "Rumble",      "domain": "rumble.com",              "color": "#85c742", "emoji": "🟢", "notes": "Live streams"},
    {"name": "Stripchat",   "domain": "stripchat.com",           "color": "#ff4500", "emoji": "🔞", "notes": "Adult live — use cookies for age verification"},
    {"name": "Twitcasting", "domain": "twitcasting.tv",          "color": "#2f6db5", "emoji": "📡", "notes": "Japanese mobile streaming"},
    {"name": "Bigo Live",   "domain": "bigo.tv",                 "color": "#00c73c", "emoji": "🎤", "notes": "Global live streaming"},
    {"name": "Pandalive",   "domain": "pandalive.co.kr",         "color": "#ff6699", "emoji": "🐼", "notes": "Korean streaming"},
    {"name": "Weibo",       "domain": "weibo.com",               "color": "#e6162d", "emoji": "微",  "notes": "Chinese social media live"},
    {"name": "Dailymotion", "domain": "dailymotion.com",         "color": "#0066dc", "emoji": "D",  "notes": "Live & VOD"},
    {"name": "Chaturbate", "domain": "chaturbate.com",           "color": "#f47321", "emoji": "🔞", "notes": "Adult live — cookies for age verification"},
    {"name": "Cam4",       "domain": "cam4.com",                 "color": "#e8403a", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "MyFreeCams", "domain": "myfreecams.com",           "color": "#009b77", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "CamSoda",    "domain": "camsoda.com",              "color": "#ff6b35", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "BongaCams",  "domain": "bongacams.com",            "color": "#f5a623", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "CamModels",  "domain": "cammodels.com",            "color": "#7b2d8b", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "Streamate",  "domain": "streamate.com",            "color": "#c0392b", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "Flirt4Free", "domain": "flirt4free.com",           "color": "#e91e8c", "emoji": "🔞", "notes": "Adult live streaming"},
]

@app.get("/api/platforms")
async def list_platforms():
    return SUPPORTED_PLATFORMS


# ── Misc endpoints ────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    active_recs = sum(1 for r in recordings.values() if r.get("status") in ("recording", "starting"))
    watched = sum(1 for c in channels.values() if c.get("monitoring"))
    return {
        "ok": True,
        "channels": len(channels),
        "recordings": len(recordings),
        # Rooms actually recording right now — this is what the header badge
        # shows.  It has no denominator: the old "N/6" implied a hard cap of 6
        # concurrent recordings, but the semaphore it borrowed only limits
        # short-lived helper processes.
        "active": active_recs,
        "watched": watched,
        "uptime": int(time.time() - START_TIME),
        "pi_mode": _is_pi(),
        "proc_limit": get_proc_limit(),
    }


@app.get("/api/version")
async def version():
    return {
        "version": VERSION,
        "pi_mode": _is_pi(),
        # poll_interval = how often the browser should refresh its view.
        # monitor_interval = how often the server actually checks a watched room
        # for live status.  These differ (5s vs 60s), so the UI needs both —
        # showing the browser cadence as the live-check cadence misleads the user.
        "poll_interval": get_frontend_poll_hint(),
        "monitor_interval": max(5, int(settings.get("monitor_interval", 60))),
        "storage": str(get_y_storage()),
    }


@app.get("/api/disk")
async def disk_usage():
    global _disk_cache, _disk_cache_ts
    now = time.time()
    if _disk_cache and now - _disk_cache_ts < get_disk_cache_ttl():
        return _disk_cache
    try:
        root      = _ensure_dir(get_y_storage())
        usage     = shutil.disk_usage(str(root))
        rec_bytes = sum(
            f.stat().st_size for f in root.rglob("*")
            if f.is_file() and not f.name.startswith(".")
        )
        _disk_cache = {
            "total": usage.total, "used": usage.used,
            "free":  usage.free,  "recordings_bytes": rec_bytes,
            "path":  str(root),
        }
        _disk_cache_ts = now
        return _disk_cache
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "recordings_bytes": 0,
                "path": str(get_y_storage())}


@app.get("/api/export")
async def export_config():
    saved_channels = {}
    for cid, ch in channels.items():
        c = dict(ch)
        for k in ("recording_id", "is_live", "last_checked"):
            c.pop(k, None)
        c.pop("ch_password", None)  # never export credentials
        saved_channels[cid] = c
    return {"channels": saved_channels, "settings": settings}


@app.post("/api/import")
async def import_config(req: ImportRequest):
    imported = 0
    # Known safe channel keys — reject anything else to prevent state corruption
    _SAFE_CH_KEYS = {
        "id", "url", "platform", "quality", "format", "monitoring",
        "auto_convert_mp4", "delete_original", "proxy", "cookies_file",
        "ch_username", "extra_args", "max_duration", "max_streams", "notes",
        "created_at", "display_name", "username", "avatar", "thumbnail",
        "stream_title", "sort_order",
    }
    for cid, ch in req.channels.items():
        if req.merge and cid in channels:
            continue
        # Sanitize: only keep known-safe keys
        ch = {k: v for k, v in ch.items() if k in _SAFE_CH_KEYS}
        ch["recording_id"] = None
        ch["is_live"]      = False
        ch["last_checked"] = None
        ch.setdefault("id",         cid)
        ch.setdefault("created_at", time.time())
        # Type-coerce numeric fields
        try:
            ch["max_duration"] = int(ch.get("max_duration", 0) or 0)
        except (ValueError, TypeError):
            ch["max_duration"] = 0
        channels[cid] = ch
        imported += 1
    # Validate settings types before applying
    _TYPE_MAP = {
        "pi_mode": bool, "monitor_interval": (int, float),
        "default_quality": str, "default_format": str,
        "auto_convert_mp4": bool, "delete_original": bool,
        "auto_retry": bool, "max_retries": int, "retry_delay": (int, float),
        "proxy": str, "extra_args": str, "cookies_file": str,
        "retention_days": int, "max_duration": int,
        "webhook_url": str, "auto_stop_stalled": bool,
    }
    for k, v in req.settings.items():
        if k in settings:
            expected = _TYPE_MAP.get(k)
            if expected and not isinstance(v, expected):
                logger.warning("Import: skipping %s (expected %s, got %s)", k, expected, type(v).__name__)
                continue
            # Clamp numeric values
            if k in ("monitor_interval", "max_retries", "retry_delay", "retention_days", "max_duration"):
                try:
                    v = max(0, int(v) if k != "retry_delay" else float(v))
                except (ValueError, TypeError):
                    v = settings[k]  # keep default
            settings[k] = v
    _save_state()
    return {"ok": True, "imported_channels": imported}


# ── yt-dlp management ────────────────────────────────────────────────────────

@app.get("/api/ytdlp-version")
async def get_ytdlp_version():
    """Current yt-dlp version plus the latest published release."""
    current = "unknown"
    try:
        proc = await asyncio.create_subprocess_exec(
            _ytdlp_exe(), "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0:
            current = stdout.decode("utf-8", errors="replace").strip() or "unknown"
    except Exception as e:
        logger.warning("Failed to get yt-dlp version: %s", e)

    latest = await asyncio.get_running_loop().run_in_executor(None, _fetch_latest_ytdlp)
    return {"current": current, "latest": latest}


def _fetch_latest_ytdlp() -> Optional[str]:
    """Ask GitHub for the newest yt-dlp tag.  Blocking — run it in an executor.

    Upstream shelled out to `curl` here; Windows does not ship a trustworthy
    curl on every build, so use the requests library we already depend on.
    """
    try:
        import requests as _rq
        r = _rq.get(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            timeout=10, headers={"User-Agent": "TOOL"},
        )
        if r.ok:
            return (r.json().get("tag_name") or "").lstrip("v") or None
    except Exception as e:
        logger.debug("Failed to fetch latest yt-dlp version: %s", e)
    return None


@app.post("/api/ytdlp-update")
async def update_ytdlp():
    """Update yt-dlp through pip.

    Upstream also fell back to `curl -o /usr/local/bin/yt-dlp` + chmod, which is
    meaningless on Windows.  yt-dlp is installed from requirements.txt as a pip
    package here, so pip is the correct (and only) update path.
    """
    global _ytdlp_cached
    logger.info("Updating yt-dlp…")

    try:
        # Frozen .exe has no pip: the bundled standalone yt-dlp updates itself.
        upd_cmd = ([_ytdlp_exe(), "-U"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
        proc = await asyncio.create_subprocess_exec(
            *upd_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.warning("pip update failed: %s", output[-400:])
            return {"success": False, "error": output[-400:] or "pip install thất bại"}

        # pip rewrote the script on disk — drop the cached path so the next
        # lookup re-resolves it.
        _ytdlp_cached = None

        new_version = "unknown"
        try:
            chk = await asyncio.create_subprocess_exec(
                _ytdlp_exe(), "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(chk.communicate(), timeout=20)
            if chk.returncode == 0:
                new_version = out.decode("utf-8", errors="replace").strip() or "unknown"
        except Exception:
            pass

        logger.info("yt-dlp updated to %s", new_version)
        return {"success": True, "version": new_version}

    except asyncio.TimeoutError:
        logger.warning("yt-dlp update timed out")
        return {"success": False, "error": "Quá thời gian cập nhật"}
    except Exception as e:
        logger.exception("yt-dlp update error")
        return {"success": False, "error": str(e)}


# ── Static files (must be last) ──────────────────────────────────────────────
class _SafeStatic(StaticFiles):
    """Serve only the web UI.

    _static_dir defaults to the project folder, so a plain StaticFiles mount
    would also hand out main.py, config.json, data/state.json and logs to
    anything that can reach the port.  Whitelist by extension instead.
    """
    _ALLOWED = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg",
                ".ico", ".webp", ".woff", ".woff2", ".map"}

    async def get_response(self, path, scope):
        p = path.strip("/")
        if p not in ("", ".") and Path(p).suffix.lower() not in self._ALLOWED:
            raise HTTPException(404)
        return await super().get_response(path, scope)


app.mount("/", _SafeStatic(directory=_static_dir, html=True), name="static")
