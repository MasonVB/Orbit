"""Identify what a file actually is, and which pipeline can flatten it.

The hard part of 360 media is that the file extension tells you the vendor but
not the projection. An .insp may be dual-fisheye straight off the camera or an
already-stitched equirectangular export. The reliable signal is metadata:
XMP-GPano on stills, st3d/sv3d or XMP-GSpherical on video. Geometry is only a
fallback heuristic.
"""
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import FFPROBE, EXIFTOOL

INSTA360_MAGIC = b'8db42d694ccc418790edff439fe026bf'

PHOTO_EXT = {".insp", ".jpg", ".jpeg", ".png", ".dng", ".heic"}
VIDEO_EXT = {".insv", ".360", ".mp4", ".mov", ".lrv", ".m4v"}
SUPPORTED_EXT = PHOTO_EXT | VIDEO_EXT


def gopro_max_tracks(path: Path) -> dict | None:
    """Detect a GoPro MAX .360 by container shape rather than by extension.

    These files are frequently renamed to .mp4 (cameras and phone apps both do
    it), and nothing in the standard metadata says "360" - so the signature is
    two equal-sized video tracks plus GoPro's telemetry handler. Track indices
    are not fixed: 0 and 5 normally, 0 and 4 for TimeWarp, which has no audio.
    """
    info = ffprobe(path)
    streams = info.get("streams", [])
    if not streams:
        return None
    video = [s for s in streams if s.get("codec_type") == "video"]
    if len(video) < 2:
        return None
    a, b = video[0], video[-1]
    if (a.get("width"), a.get("height")) != (b.get("width"), b.get("height")):
        return None
    handlers = " ".join(s.get("tags", {}).get("handler_name", "") for s in streams)
    if "GoPro" not in handlers:
        return None
    meta = [s for s in streams
            if "GoPro MET" in s.get("tags", {}).get("handler_name", "")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "front": int(a["index"]), "rear": int(b["index"]),
        "meta": int(meta[0]["index"]) if meta else None,
        "audio": int(audio[0]["index"]) if audio else None,
        "width": int(a["width"]), "height": int(a["height"]),
    }


def insta360_trailer(path: Path) -> bool:
    """True if the file carries an Insta360 trailer.

    The extension lies in both directions: an .insp may be a stitched export,
    and a stitched export may be renamed .jpg. The 32-byte magic at the very
    end of the file is the only reliable signal.
    """
    try:
        with path.open('rb') as fh:
            if path.stat().st_size < 64:
                return False
            fh.seek(-32, 2)
            return fh.read(32) == INSTA360_MAGIC
    except OSError:
        return False


@dataclass
class Probe:
    kind: str = "unknown"            # photo | video
    source_format: str = "unknown"
    projection: str = "unknown"      # dfisheye | eac | equirectangular
    width: int = 0
    height: int = 0
    duration: float = 0.0
    camera_make: str = ""
    camera_model: str = ""
    captured_at: float = 0.0
    video_track_indices: list[int] = field(default_factory=list)
    pipeline: str = "unsupported"    # see pipeline.py
    note: str = ""
    insta360_trailer: bool = False   # has IMU + embedded preview
    has_embedded_preview: bool = False
    gopro_tracks: dict | None = None  # front/rear/meta stream indices
    fps: float = 0.0
    frame_count: int = 0


def _run(cmd: list[str]) -> str:
    """Never raise. A missing tool or a file exiftool dislikes should degrade
    detection, not abort the whole job."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ""


def exif(path: Path) -> dict:
    out = _run([EXIFTOOL, "-j", "-n", "-G0", str(path)])
    try:
        return json.loads(out)[0]
    except Exception:
        return {}


def ffprobe(path: Path) -> dict:
    out = _run([
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    try:
        return json.loads(out)
    except Exception:
        return {}


def _get(d: dict, *suffixes: str):
    """ExifTool keys are group-prefixed (EXIF:Model, XMP:ProjectionType)."""
    for key, value in d.items():
        tail = key.split(":")[-1]
        if tail in suffixes:
            return value
    return None


def _has_gpano(meta: dict) -> bool:
    proj = _get(meta, "ProjectionType", "UsePanoramaViewer")
    if isinstance(proj, str) and "equirect" in proj.lower():
        return True
    return _get(meta, "FullPanoWidthPixels") is not None


def _has_spherical_video(streams: list[dict], meta: dict) -> bool:
    for s in streams:
        for sd in s.get("side_data_list", []) or []:
            if "spherical" in str(sd.get("side_data_type", "")).lower():
                return True
    spherical = _get(meta, "Spherical", "ProjectionType")
    if isinstance(spherical, str) and spherical.lower() in ("true", "equirectangular"):
        return True
    return bool(spherical) if isinstance(spherical, bool) else False


def probe(path: Path) -> Probe:
    ext = path.suffix.lower()
    p = Probe()
    if ext not in SUPPORTED_EXT:
        p.note = f"Unrecognised extension {ext}"
        return p

    p.insta360_trailer = insta360_trailer(path)
    if p.insta360_trailer:
        try:
            from . import insp360
            data_tail_ok = True
            with path.open('rb') as fh:
                fh.seek(0, 2)
                size = fh.tell()
            need = insp360.PREVIEW_W * insp360.PREVIEW_H * 3 // 2
            p.has_embedded_preview = size > need + 40
        except Exception:
            p.has_embedded_preview = False

    meta = exif(path)
    p.camera_make = str(_get(meta, "Make") or "")
    p.camera_model = str(_get(meta, "Model") or "")
    ts = _get(meta, "DateTimeOriginal", "CreateDate", "MediaCreateDate")
    if isinstance(ts, (int, float)):
        p.captured_at = float(ts)

    # Insta360 files carry vendor strings in a trailing block rather than EXIF
    # on some models; fall back to the filename convention.
    if not p.camera_make and ext in (".insp", ".insv"):
        p.camera_make = "Insta360"

    if ext in PHOTO_EXT:
        return _probe_photo(path, ext, meta, p)
    return _probe_video(path, ext, meta, p)


def _probe_photo(path: Path, ext: str, meta: dict, p: Probe) -> Probe:
    p.kind = "photo"
    p.width = int(_get(meta, "ImageWidth") or 0)
    p.height = int(_get(meta, "ImageHeight") or 0)
    if not (p.width and p.height):
        # exiftool missing, or a container it won't parse. ffmpeg decodes .insp
        # and friends by content, so the extension doesn't matter here.
        streams = [s for s in ffprobe(path).get("streams", [])
                   if s.get("codec_type") == "video"]
        if streams:
            p.width = int(streams[0].get("width") or 0)
            p.height = int(streams[0].get("height") or 0)

    if _has_gpano(meta):
        p.projection = "equirectangular"
        p.source_format = f"equirect_{ext.lstrip('.')}"
        p.pipeline = "photo_passthrough"
        return p

    if p.insta360_trailer or ext == ".insp":
        # Straight off an Insta360 camera this is a JPEG holding two fisheye
        # circles side by side. Exports from Insta360 Studio carry GPano and
        # were caught above.
        p.projection = "dfisheye"
        p.source_format = "insp"
        p.pipeline = "photo_insta360"
        return p

    # No GPano. A 2:1 frame is almost certainly an equirect that lost its XMP.
    if p.width and p.height and abs((p.width / p.height) - 2.0) < 0.02:
        p.projection = "equirectangular"
        p.source_format = f"equirect_{ext.lstrip('.')}"
        p.pipeline = "photo_passthrough"
        p.note = "Assumed equirectangular from 2:1 aspect; no GPano metadata found."
        return p

    p.note = "Not a 360 photo (no panorama metadata, aspect ratio is not 2:1)."
    return p


def _probe_video(path: Path, ext: str, meta: dict, p: Probe) -> Probe:
    p.kind = "video"
    p.gopro_tracks = gopro_max_tracks(path)
    info = ffprobe(path)
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        p.note = "No video stream found."
        return p

    p.video_track_indices = [int(s["index"]) for s in streams]
    p.width = int(streams[0].get("width") or 0)
    p.height = int(streams[0].get("height") or 0)
    try:
        p.duration = float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        p.duration = 0.0
    try:
        num, den = (int(x) for x in str(streams[0].get("avg_frame_rate", "0/1")).split("/"))
        p.fps = num / den if den else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        p.fps = 0.0
    counts = [int(s.get("nb_frames") or 0) for s in streams]
    p.frame_count = min(c for c in counts) if counts and all(counts) else 0

    if ext == ".360" or p.gopro_tracks:
        # GoPro MAX: two HEVC tracks holding a 3x2 equi-angular cubemap.
        if not p.gopro_tracks:
            p.note = "Expected two GoPro video tracks in a .360 file."
            return p
        p.projection = "eac"
        p.source_format = "gopromax360"
        p.pipeline = "video_max360"
        p.width = p.gopro_tracks["width"]
        p.height = p.gopro_tracks["height"] * 2   # the stacked cubemap
        return p

    if _has_spherical_video(streams, meta):
        p.projection = "equirectangular"
        p.source_format = f"equirect_{ext.lstrip('.')}"
        p.pipeline = "video_passthrough"
        return p

    if ext == ".insv" or p.insta360_trailer:
        # Video stitching still goes through ffmpeg's v360; the solved
        # calibration is applied to it in pipeline.py.
        p.projection = "dfisheye"
        p.source_format = "insv"
        p.pipeline = "video_dfisheye"
        return p

    if p.width and p.height and abs((p.width / p.height) - 2.0) < 0.02:
        p.projection = "equirectangular"
        p.source_format = f"equirect_{ext.lstrip('.')}"
        p.pipeline = "video_passthrough"
        p.note = "Assumed equirectangular from 2:1 aspect; no spherical metadata found."
        return p

    p.note = "Not a 360 video (no spherical metadata, aspect ratio is not 2:1)."
    return p


def insv_role(name: str) -> str:
    """Insta360 writes two files per clip: _00_ is full resolution, _10_ is a
    low-res proxy the phone app uses for scrubbing. Keep the proxy as an
    instant preview, but derive everything real from _00_."""
    stem = Path(name).stem
    if "_10_" in stem:
        return "proxy"
    if "_00_" in stem:
        return "primary"
    return "primary"
