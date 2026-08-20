"""Configuration and per-camera lens calibration."""
import json
import os
import secrets
from pathlib import Path

DATA = Path(os.environ.get("ORBIT_DATA", "/data"))
ORIGINALS = DATA / "originals"      # never modified after ingest
DERIVED = DATA / "derived"          # regenerable: stitched, proxies, thumbs
CONFIG = DATA / "config"
TMP = DATA / "tmp"

for d in (ORIGINALS, DERIVED, CONFIG, TMP):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA / "orbit.db"

# Secret persists across restarts so sessions and share tokens survive.
_secret_file = CONFIG / "secret.key"
if not _secret_file.exists():
    _secret_file.write_text(secrets.token_urlsafe(48))
SECRET = _secret_file.read_text().strip()

PUBLIC_BASE_URL = os.environ.get("ORBIT_PUBLIC_URL", "").rstrip("/")
ADMIN_USER = os.environ.get("ORBIT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ORBIT_ADMIN_PASSWORD", "")

WORKER_CONCURRENCY = int(os.environ.get("ORBIT_WORKER_CONCURRENCY", "1"))
FFMPEG = os.environ.get("ORBIT_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("ORBIT_FFPROBE", "ffprobe")
EXIFTOOL = os.environ.get("ORBIT_EXIFTOOL", "exiftool")

# Video proxy ladder. 360 video needs ~4x the pixels of flat video to look
# equivalent, but browsers choke above 4K H.264, so 3840x1920 is the ceiling.
VIDEO_RENDITIONS = [
    {"name": "2160", "width": 3840, "height": 1920, "crf": 24, "maxrate": "24M"},
    {"name": "1080", "width": 1920, "height": 960, "crf": 23, "maxrate": "8M"},
]
# GoPro MAX stabilisation, applied from the camera's own telemetry.
#   horizon  level every frame, keep intentional panning (best handheld)
#   full     lock the world completely (best on a pole or mount)
#   none     fastest; skips the per-frame rotation pass entirely
MAX360_STABILISE = os.environ.get("ORBIT_MAX360_STABILISE", "horizon")

PHOTO_MAX_WIDTH = 8000      # equirect master
PHOTO_PREVIEW_WIDTH = 3000  # what the viewer loads first
THUMB_WIDTH = 720

# ---------------------------------------------------------------------------
# Lens calibration
#
# Insta360 files carry no lens geometry, so it has to be supplied. Rather than
# guessing a field of view (the usual approach, and the reason naive stitches
# ghost), Orbit stores solved calibrations: circle centres, radius, true FOV,
# and the rotation of the rear lens relative to the front.
#
# Shipped profiles live in app/calibrations/. Profiles solved on this machine
# are written to /data/config/calibrations/ and take precedence, so a new
# camera model calibrates itself once on first import and is fast thereafter.
# ---------------------------------------------------------------------------
BUILTIN_CALIBRATIONS = Path(__file__).resolve().parent / "calibrations"
USER_CALIBRATIONS = CONFIG / "calibrations"
USER_CALIBRATIONS.mkdir(parents=True, exist_ok=True)

# Field of view used only when no solved calibration exists and solving is
# disabled or fails. Deliberately conservative.
FALLBACK_FOV_DEG = 200.0
AUTO_SOLVE = os.environ.get("ORBIT_AUTO_SOLVE", "1") != "0"


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def calibration_path(make: str, model: str) -> Path:
    """Where a solved calibration for this camera would be written."""
    return USER_CALIBRATIONS / f"{_slug(f'{make} {model}') or 'unknown'}.json"


def load_calibration(make: str = "", model: str = "") -> dict | None:
    """Best matching solved calibration, user profiles winning over shipped."""
    haystack = f"{make} {model}".lower().replace("-", " ")
    best, best_len = None, -1
    for folder in (BUILTIN_CALIBRATIONS, USER_CALIBRATIONS):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            try:
                prof = json.loads(f.read_text())
            except Exception:
                continue
            keys = prof.get("match") or [prof.get("camera", "")]
            for key in keys:
                key = str(key).lower()
                if key and key in haystack and len(key) >= best_len:
                    best, best_len = prof, len(key)
    return best
