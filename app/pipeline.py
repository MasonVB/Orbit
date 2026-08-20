"""Turn vendor formats into equirectangular derivatives a browser can play.

Every pipeline writes into DERIVED/<item_id>/ and leaves the original alone.
Deleting DERIVED and re-queueing rebuilds everything.
"""
import json
import shutil
import subprocess
import numpy as np
from pathlib import Path

from .config import (
    MAX360_STABILISE, FFMPEG, EXIFTOOL, DERIVED, PHOTO_MAX_WIDTH, PHOTO_PREVIEW_WIDTH,
    THUMB_WIDTH, VIDEO_RENDITIONS, AUTO_SOLVE, FALLBACK_FOV_DEG,
    calibration_path, load_calibration,
)
from .formats import Probe


class PipelineError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Insta360 calibration
# --------------------------------------------------------------------------

def calibration_for(p: Probe, src: Path, log=lambda m: None):
    """Solved stitch calibration for this camera, solving once if needed.

    Returns (insp360.Calib, imu_mount or None, fov_degrees). Falls back to a
    plain assumed field of view if solving is off or fails, which is what
    ffmpeg would have done anyway.
    """
    import numpy as np
    from . import insp360

    prof = load_calibration(p.camera_make, p.camera_model)
    if prof is None and AUTO_SOLVE:
        log("no calibration for this camera - solving once (a few minutes)")
        try:
            prof = solve_calibration(src, p)
            dest = calibration_path(p.camera_make, p.camera_model)
            dest.write_text(json.dumps(prof, indent=2))
            log(f"calibration solved and cached as {dest.name}")
        except Exception as exc:
            log(f"calibration solve failed ({exc}); using fallback field of view")
            prof = None
    if prof is None:
        return None, None, FALLBACK_FOV_DEG

    # geometry is stored against one sensor size; rescale for other modes
    import cv2 as _cv2
    scale = 1.0
    ref = prof.get("ref_image_width")
    if ref and p.width:
        scale = (p.width / 2.0) / (ref / 2.0)
    cal = insp360.Calib(
        prof["cx0"] * scale, prof["cy0"] * scale,
        prof["cx1"] * scale, prof["cy1"] * scale,
        prof["r"] * scale, np.radians(prof["fov_deg"]),
        np.radians(prof["yaw_deg"]), np.radians(prof["pitch_deg"]),
        np.radians(prof["roll_deg"]), mirror=prof.get("mirror", False))
    mount = np.array(prof["imu_mount"]) if prof.get("imu_mount") else None
    return cal, mount, prof["fov_deg"]


def solve_calibration(src: Path, p: Probe) -> dict:
    """Fit stitch geometry by maximising agreement across the seam."""
    import numpy as np
    import cv2
    from scipy.optimize import minimize
    from . import insp360

    img = cv2.imread(str(src))
    if img is None:
        raise PipelineError("could not decode image for calibration")
    small = cv2.resize(img, (img.shape[1] // 4, img.shape[0] // 4),
                       interpolation=cv2.INTER_AREA)
    c = small.shape[1] / 4.0

    def cost(v, band):
        if not (0.5 * c < v[4] < 1.5 * c):
            return 10.0
        if not (np.radians(175) < v[5] < np.radians(235)):
            return 10.0
        if max(abs(v[6]), abs(v[7]), abs(v[8])) > 0.35:
            return 10.0
        if max(abs(v[i] - c) for i in range(4)) > 0.1 * c:
            return 10.0
        return insp360.seam_cost(small, insp360.Calib.from_vector(v, mirror=False),
                                 band_deg=band)

    v = np.array([c, c, c, c, c * 0.98, np.radians(205.0), 0.0, 0.0, 0.0])
    mask = np.array([0, 0, 0, 0, 1, 1, 1, 1, 1], bool)

    def merge(x):
        f = v.copy(); f[mask] = x; return f

    r = minimize(lambda x: cost(merge(x), 8.0), v[mask], method='Powell',
                 options=dict(xtol=1e-4, ftol=1e-6, maxiter=4000))
    v = merge(r.x)
    for band in (8.0, 4.0, 2.5):
        r = minimize(lambda x: cost(x, band), v, method='Powell',
                     options=dict(xtol=1e-5, ftol=1e-8, maxiter=8000))
        v = r.x
    if -r.fun < 0.5:
        raise PipelineError(f"seam correlation only {-r.fun:.2f}; refusing to trust it")
    v[:5] *= 4.0
    return dict(camera=f"{p.camera_make} {p.camera_model}".strip(),
                match=[p.camera_model.lower()] if p.camera_model else [],
                ref_image_width=img.shape[1],
                cx0=v[0], cy0=v[1], cx1=v[2], cy1=v[3], r=v[4],
                fov_deg=float(np.degrees(v[5])), yaw_deg=float(np.degrees(v[6])),
                pitch_deg=float(np.degrees(v[7])), roll_deg=float(np.degrees(v[8])),
                mirror=False, imu_mount=None, seam_ncc=float(-r.fun),
                note="solved by Orbit; imu_mount needs a level reference frame")


def run(cmd: list[str], timeout: int = 60 * 60 * 6) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PipelineError(f"{cmd[0]} is not installed in this image.") from None
    except subprocess.TimeoutExpired:
        raise PipelineError(f"{Path(cmd[0]).name} timed out after {timeout}s.") from None
    except OSError as exc:
        raise PipelineError(f"{Path(cmd[0]).name} could not run: {exc}") from None
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise PipelineError(f"{Path(cmd[0]).name} failed:\n{tail}")


def out_dir(item_id: int) -> Path:
    d = DERIVED / str(item_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

def extract_embedded_preview(src: Path, dst: Path) -> tuple[int, int] | None:
    """Pull the camera's own stitched equirect out of the trailer.

    Costs no reprojection at all, so an Insta360 photo becomes viewable almost
    immediately while the real stitch runs behind it.
    """
    import cv2
    from . import insp360
    try:
        im = insp360.read_preview(src.read_bytes())
    except Exception:
        return None
    cv2.imwrite(str(dst), im, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return im.shape[1], im.shape[0]


def photo_insta360(src: Path, dst: Path, p: Probe, log=lambda m: None) -> int:
    """Stitch an .insp using solved calibration, levelling from its own IMU.

    Returns the output width. Falls back to ffmpeg's v360 if no calibration is
    available, so an unknown camera still produces something usable.
    """
    import cv2
    from . import insp360

    cal, mount, fov_deg = calibration_for(p, src, log)
    width = min(p.width or PHOTO_MAX_WIDTH, PHOTO_MAX_WIDTH)
    width -= width % 2

    if cal is None:
        log(f"stitching with assumed {fov_deg:.0f} deg field of view")
        run([FFMPEG, "-y", "-i", str(src),
             "-vf", (f"v360=input=dfisheye:output=e:ih_fov={fov_deg}:"
                     f"iv_fov={fov_deg}:w={width}:h={width // 2}"),
             "-frames:v", "1", "-q:v", "2", str(dst)])
        return width

    world_R = None
    if mount is not None:
        try:
            _, acc, _ = insp360.read_imu(src.read_bytes())
            world_R, tilt = insp360.leveling_rotation(acc, mount)
            log(f"levelling horizon ({tilt:.1f} deg of camera tilt)")
        except Exception as exc:
            log(f"levelling skipped ({exc})")

    img = cv2.imread(str(src))
    if img is None:
        raise PipelineError("could not decode fisheye image")
    log(f"stitching at {fov_deg:.2f} deg field of view")
    out = insp360.render(img, cal, width, world_R=world_R, feather_deg=6.0)
    cv2.imwrite(str(dst), out, [cv2.IMWRITE_JPEG_QUALITY, 94])
    return width


def photo_dfisheye(src: Path, dst: Path, p: Probe) -> None:
    """Generic dual-fisheye fallback for non-Insta360 cameras."""
    width = min(p.width or PHOTO_MAX_WIDTH, PHOTO_MAX_WIDTH)
    width -= width % 2
    run([
        FFMPEG, "-y", "-i", str(src),
        "-vf", (f"v360=input=dfisheye:output=e:"
                f"ih_fov={FALLBACK_FOV_DEG}:iv_fov={FALLBACK_FOV_DEG}:"
                f"w={width}:h={width // 2}"),
        "-frames:v", "1", "-q:v", "2", str(dst),
    ])


def photo_passthrough(src: Path, dst: Path, p: Probe) -> None:
    width = min(p.width or PHOTO_MAX_WIDTH, PHOTO_MAX_WIDTH)
    width -= width % 2
    run([FFMPEG, "-y", "-i", str(src),
         "-vf", f"scale={width}:-2", "-frames:v", "1", "-q:v", "2", str(dst)])


def resize(src: Path, dst: Path, width: int) -> None:
    run([FFMPEG, "-y", "-i", str(src),
         "-vf", f"scale={width}:-2", "-frames:v", "1", "-q:v", "4", str(dst)])


def tag_gpano(path: Path, width: int, height: int) -> None:
    """Write the XMP block that makes the file open as a sphere in Google
    Photos, Facebook, VR headsets and anything else that reads GPano."""
    try:
        run([
            EXIFTOOL, "-overwrite_original",
            "-XMP-GPano:UsePanoramaViewer=True",
            "-XMP-GPano:ProjectionType=equirectangular",
            f"-XMP-GPano:FullPanoWidthPixels={width}",
            f"-XMP-GPano:FullPanoHeightPixels={height}",
            f"-XMP-GPano:CroppedAreaImageWidthPixels={width}",
            f"-XMP-GPano:CroppedAreaImageHeightPixels={height}",
            "-XMP-GPano:CroppedAreaLeftPixels=0",
            "-XMP-GPano:CroppedAreaTopPixels=0",
            str(path),
        ], timeout=120)
    except PipelineError:
        pass  # cosmetic; the app knows the projection from the database


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------

def _encode(input_args: list[str], vf: str, dst: Path, r: dict,
            copy_audio_from: Path | None = None) -> None:
    cmd = [FFMPEG, "-y", *input_args, "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", str(r["crf"]),
           "-maxrate", r["maxrate"], "-bufsize", "32M",
           "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart"]
    if copy_audio_from:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    cmd.append(str(dst))
    run(cmd)


def video_dfisheye(src: Path, dst: Path, p: Probe, r: dict) -> None:
    """Dual-fisheye video via ffmpeg, using the solved FOV when one exists.

    Video can't use the full solved model - ffmpeg's v360 has no way to express
    a per-lens rotation - but the correct field of view alone closes most of
    the gap over a guessed one.
    """
    prof = load_calibration(p.camera_make, p.camera_model)
    fov = prof["fov_deg"] if prof else FALLBACK_FOV_DEG
    vf = (f"v360=input=dfisheye:output=e:ih_fov={fov}:iv_fov={fov}:"
          f"w={r['width']}:h={r['height']}")
    _encode(["-i", str(src)], vf, dst, r, copy_audio_from=src)


def video_passthrough(src: Path, dst: Path, p: Probe, r: dict) -> None:
    vf = f"scale={r['width']}:{r['height']}"
    _encode(["-i", str(src)], vf, dst, r, copy_audio_from=src)


def gopro_frames(src: Path, index: int, w: int, h: int):
    """Decode one GoPro video track to BGR frames."""
    proc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", str(src), "-map", f"0:{index}",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 8)
    n = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(n)
            if len(buf) < n:
                return
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def video_max360(src: Path, outputs: list[tuple[Path, dict]], p: Probe,
                 log=lambda m: None) -> None:
    """GoPro MAX .360 -> equirectangular, all renditions in one pass.

    Pure numpy and OpenCV: no OpenCL, no patched ffmpeg. The frame is rendered
    once at the largest rendition and scaled down for the rest, since the
    projection is far more expensive than the scaling.
    """
    import numpy as np
    from . import max360, gpmf

    tracks = p.gopro_tracks
    if not tracks:
        raise PipelineError("not a GoPro MAX file (expected two GoPro video tracks)")
    w, h = tracks["width"], tracks["height"]
    fps = p.fps or 30000 / 1001
    nframes = p.frame_count or int((p.duration or 0) * fps) or 0

    # rotations from the camera's own telemetry
    rots = None
    mode = MAX360_STABILISE
    if mode != "none" and tracks["meta"] is not None and nframes:
        try:
            raw = subprocess.run(
                [FFMPEG, "-v", "error", "-i", str(src), "-map", f"0:{tracks['meta']}",
                 "-c", "copy", "-f", "rawvideo", "-"],
                capture_output=True, timeout=600).stdout
            tele, _ = gpmf.streams(raw)
            if "GRAV" in tele:
                if mode == "full" and "CORI" not in tele:
                    mode = "horizon"
                rots = max360.stabilise_rotations(tele.get("CORI"), tele["GRAV"],
                                                  nframes, mode=mode)
                log(f"stabilising ({mode}) from GoPro telemetry")
            else:
                log("no GRAV telemetry; stabilisation skipped")
        except Exception as exc:
            log(f"stabilisation skipped ({exc})")

    outputs = sorted(outputs, key=lambda x: -x[1]["width"])
    ow, oh = outputs[0][1]["width"], outputs[0][1]["height"]

    log(f"projecting {w}x{h*2} cubemap -> {ow}x{oh}")
    maps = max360.build_maps(ow, oh, w, h, blend=True)
    rotator = max360.Rotator(ow, oh) if rots else None

    procs = []
    for dst, r in outputs:
        cmd = [FFMPEG, "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x{oh}",
               "-r", f"{fps:.6f}", "-i", "-"]
        if tracks["audio"] is not None:
            cmd += ["-i", str(src), "-map", "0:v", "-map", f"1:{tracks['audio']}",
                    "-c:a", "aac", "-b:a", "160k"]
        if (r["width"], r["height"]) != (ow, oh):
            cmd += ["-vf", f"scale={r['width']}:{r['height']}"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(r["crf"]),
                "-maxrate", r["maxrate"], "-bufsize", "32M",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(dst)]
        procs.append(subprocess.Popen(cmd, stdin=subprocess.PIPE))

    fronts = gopro_frames(src, tracks["front"], w, h)
    rears = gopro_frames(src, tracks["rear"], w, h)
    done = 0
    try:
        for i, (fr, re) in enumerate(zip(fronts, rears)):
            if nframes and i >= nframes:
                break
            frame = max360.render(fr, re, maps)
            if rotator is not None and i < len(rots) and rots[i] is not None:
                frame = rotator.apply(frame, rots[i])
            buf = frame.tobytes()
            for proc in procs:
                proc.stdin.write(buf)
            done += 1
            if done % 30 == 0:
                log(f"projected {done}/{nframes or '?'} frames")
    finally:
        for proc in procs:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
    log(f"projected {done} frames")


def video_poster(src: Path, dst: Path, at: float = 1.0) -> None:
    run([FFMPEG, "-y", "-ss", str(at), "-i", str(src),
         "-vf", f"scale={THUMB_WIDTH}:-2", "-frames:v", "1", "-q:v", "4", str(dst)])


def tag_spherical_video(path: Path) -> None:
    """XMP-GSpherical so downloaded MP4s still open as 360 elsewhere."""
    try:
        run([
            EXIFTOOL, "-overwrite_original",
            "-XMP-GSpherical:Spherical=true",
            "-XMP-GSpherical:Stitched=true",
            "-XMP-GSpherical:ProjectionType=equirectangular",
            "-XMP-GSpherical:StitchingSoftware=Orbit",
            str(path),
        ], timeout=120)
    except PipelineError:
        pass


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def quick_preview(item_id: int, src: Path, p: Probe) -> list[dict]:
    """Derivatives available immediately, before any reprojection.

    Insta360 cameras embed their own stitched equirectangular, so a photo can
    be browsed within a second of upload while the real stitch queues behind
    it. Returns [] when there is nothing to extract.
    """
    if p.kind != "photo" or not p.has_embedded_preview:
        return []
    d = out_dir(item_id)
    preview, thumb = d / "preview.jpg", d / "thumb.jpg"
    size = extract_embedded_preview(src, preview)
    if not size:
        return []
    w, h = size
    resize(preview, thumb, THUMB_WIDTH)
    return [
        {"kind": "preview", "path": str(preview), "mime": "image/jpeg",
         "width": w, "height": h},
        {"kind": "thumb", "path": str(thumb), "mime": "image/jpeg",
         "width": THUMB_WIDTH, "height": THUMB_WIDTH // 2},
    ]


def build(item_id: int, src: Path, p: Probe, on_progress=lambda s: None) -> list[dict]:
    """Produce every derivative for one item. Returns rows for `derivatives`."""
    d = out_dir(item_id)
    made: list[dict] = []

    if p.kind == "photo":
        master = d / "master.jpg"
        on_progress("flattening to equirectangular")
        mw = min(p.width or PHOTO_MAX_WIDTH, PHOTO_MAX_WIDTH)
        if p.pipeline == "photo_insta360":
            mw = photo_insta360(src, master, p, log=on_progress)
        elif p.pipeline == "photo_dfisheye":
            photo_dfisheye(src, master, p)
        elif p.pipeline == "photo_passthrough":
            photo_passthrough(src, master, p)
        else:
            raise PipelineError(f"No photo pipeline for '{p.pipeline}'")

        tag_gpano(master, mw, mw // 2)
        made.append({"kind": "master", "path": str(master), "mime": "image/jpeg",
                     "width": mw, "height": mw // 2})

        on_progress("building preview and thumbnail")
        # overwrite any embedded-preview derivatives with ones cut from the
        # real stitch, which are sharper and correctly levelled
        preview, thumb = d / "preview.jpg", d / "thumb.jpg"
        resize(master, preview, PHOTO_PREVIEW_WIDTH)
        resize(master, thumb, THUMB_WIDTH)
        made.append({"kind": "preview", "path": str(preview), "mime": "image/jpeg",
                     "width": PHOTO_PREVIEW_WIDTH, "height": PHOTO_PREVIEW_WIDTH // 2})
        made.append({"kind": "thumb", "path": str(thumb), "mime": "image/jpeg",
                     "width": THUMB_WIDTH, "height": THUMB_WIDTH // 2})
        return made

    # video
    wanted = []
    for r in VIDEO_RENDITIONS:
        # don't upscale past the source sphere
        if p.projection == "equirectangular" and p.width and r["width"] > p.width:
            continue
        wanted.append(r)
    if not wanted:
        raise PipelineError("Source resolution too low for any rendition.")

    if p.pipeline == "video_max360":
        # one projection pass feeds every rendition
        targets = [(d / f"video_{r['name']}.mp4", r) for r in wanted]
        video_max360(src, targets, p, log=on_progress)
        for dst, r in targets:
            if not dst.exists():
                raise PipelineError(f"rendition {r['name']} was not written")
            tag_spherical_video(dst)
            made.append({"kind": f"video_{r['name']}", "path": str(dst),
                         "mime": "video/mp4", "width": r["width"], "height": r["height"]})
        first = targets[0][0]
        on_progress("grabbing poster frame")
        thumb = d / "thumb.jpg"
        video_poster(first, thumb, at=min(1.0, max(0.0, (p.duration or 2) / 4)))
        made.append({"kind": "thumb", "path": str(thumb), "mime": "image/jpeg",
                     "width": THUMB_WIDTH, "height": THUMB_WIDTH // 2})
        return made

    fn = {
        "video_dfisheye": video_dfisheye,
        "video_passthrough": video_passthrough,
    }.get(p.pipeline)
    if not fn:
        raise PipelineError(f"No video pipeline for '{p.pipeline}'")

    first: Path | None = None
    for r in wanted:
        dst = d / f"video_{r['name']}.mp4"
        on_progress(f"encoding {r['width']}x{r['height']} proxy")
        fn(src, dst, p, r)
        tag_spherical_video(dst)
        made.append({"kind": f"video_{r['name']}", "path": str(dst),
                     "mime": "video/mp4", "width": r["width"], "height": r["height"]})
        first = first or dst

    if not made:
        raise PipelineError("Source resolution too low for any rendition.")

    on_progress("grabbing poster frame")
    thumb = d / "thumb.jpg"
    video_poster(first, thumb, at=min(1.0, max(0.0, (p.duration or 2) / 4)))
    made.append({"kind": "thumb", "path": str(thumb), "mime": "image/jpeg",
                 "width": THUMB_WIDTH, "height": THUMB_WIDTH // 2})
    return made
