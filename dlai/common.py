"""Shared paths, constants and small helpers for dl_ai_detector."""

import datetime
import json
import os
import sys
from pathlib import Path

APP_NAME = "dl_ai_detector"
APP_VERSION = "1.4.0"
RELEASE_ASSET = "dl_ai_detector-windows.zip"

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "gif", "heic", "heif", "avif", "jfif"}
VIDEO_EXT = {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "mts", "m2ts", "3gp", "mpg",
             "mpeg", "ogv"}

FROZEN = getattr(sys, "frozen", False)
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent          # folder holding the .exe (Mac: .app/Contents/MacOS)
else:
    APP_DIR = Path(__file__).resolve().parent.parent         # repo root when run from source

# Per-user data on this computer only: downloaded models, settings, log.
# (Not %TEMP%: Windows Storage Sense / Disk Cleanup empties it, which would silently delete GBs of models.)
if IS_WIN:
    SUP = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
elif IS_MAC:
    SUP = Path.home() / "Library" / "Application Support" / APP_NAME
else:
    SUP = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
MODELS_DIR = SUP / "models"
LOG = SUP / f"{APP_NAME}.log"


def kind_of(path):
    ext = Path(path).suffix[1:].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return None


def ensure_dirs():
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def log(msg):
    ensure_dirs()
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def load_settings():
    try:
        return json.loads((SUP / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    ensure_dirs()
    try:
        (SUP / "settings.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def fmt_size(mb):
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB" if mb >= 1 else f"{mb * 1000:.0f} KB"


def fmt_t(sec):
    sec = max(0.0, float(sec or 0))
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}"


def resource(rel):
    """Path to a bundled file (assets/...) both from source and inside the PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    p = base / rel
    return p if p.exists() else APP_DIR / rel


def open_path(path):
    """Open a file or folder with the system (Explorer / Finder)."""
    import subprocess
    if IS_WIN:
        os.startfile(str(path))
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
