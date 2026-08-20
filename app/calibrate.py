"""Solve and inspect stitch calibration for a camera.

Stitch quality is dominated by lens geometry, and none of it is recorded in the
file. Orbit solves it rather than guessing: circle centres, radius, true field
of view, and the rotation of the rear lens relative to the front, all fitted by
maximising agreement across the seam.

This normally happens automatically the first time you import from a new
camera. Run it by hand to redo a calibration or to check one:

    docker exec -it orbit-worker python -m app.calibrate solve /data/originals/12/IMG.insp
    docker exec -it orbit-worker python -m app.calibrate show
    docker exec -it orbit-worker python -m app.calibrate inspect /data/originals/12/IMG.insp

`solve` writes to /data/config/calibrations/, which takes precedence over the
profiles shipped with Orbit.
"""
import json
import sys
from pathlib import Path

from .config import USER_CALIBRATIONS, BUILTIN_CALIBRATIONS, calibration_path
from .formats import probe


def cmd_solve(path: Path) -> int:
    from .pipeline import solve_calibration
    p = probe(path)
    print(f"camera : {p.camera_make} {p.camera_model}".strip())
    print(f"image  : {p.width}x{p.height}  ({p.source_format})")
    if p.projection != "dfisheye":
        print("This file is already equirectangular; nothing to calibrate.")
        return 1
    print("solving (a few minutes) ...")
    prof = solve_calibration(path, p)
    dest = calibration_path(p.camera_make, p.camera_model)
    dest.write_text(json.dumps(prof, indent=2))
    print(f"\n  field of view : {prof['fov_deg']:.2f} deg")
    print(f"  circle radius : {prof['r']:.1f} px")
    print(f"  rear lens     : yaw {prof['yaw_deg']:+.2f}  pitch {prof['pitch_deg']:+.2f}"
          f"  roll {prof['roll_deg']:+.2f} deg")
    print(f"  seam match    : {prof['seam_ncc']:.3f}  (1.0 is perfect)")
    print(f"\nwrote {dest}")
    print("Reprocess affected items to rebuild them with this calibration.")
    return 0


def cmd_show() -> int:
    for label, folder in (("shipped", BUILTIN_CALIBRATIONS), ("solved here", USER_CALIBRATIONS)):
        files = sorted(folder.glob("*.json")) if folder.is_dir() else []
        print(f"\n{label} ({folder}):")
        if not files:
            print("  none")
        for f in files:
            try:
                j = json.loads(f.read_text())
            except Exception:
                print(f"  {f.name}: unreadable")
                continue
            imu = "yes" if j.get("imu_mount") else "no"
            print(f"  {f.name}: {j.get('camera','?')}  fov={j.get('fov_deg',0):.2f} deg  "
                  f"seam={j.get('seam_ncc','n/a')}  horizon levelling={imu}")
    return 0


def cmd_inspect(path: Path) -> int:
    from . import insp360
    p = probe(path)
    print(f"camera   : {p.camera_make} {p.camera_model}".strip())
    print(f"image    : {p.width}x{p.height}  projection={p.projection}")
    print(f"pipeline : {p.pipeline}")
    print(f"Insta360 trailer: {p.insta360_trailer}   embedded preview: {p.has_embedded_preview}")
    if p.insta360_trailer:
        data = path.read_bytes()
        t, acc, gyr = insp360.read_imu(data)
        import numpy as np
        print(f"IMU      : {len(t):,} records, {(t[-1]-t[0])/1e6:.2f} s "
              f"@ {1e6/np.median(np.diff(t)):.0f} Hz")
        print(f"           |acceleration| = {np.linalg.norm(acc,axis=1).mean():.3f} g "
              f"(should sit near 1.0)")
        print(f"gravity  : {np.round(insp360.gravity_at_shutter(acc), 4)}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "show":
        return cmd_show()
    if cmd in ("solve", "inspect"):
        if len(args) < 2:
            print(f"usage: python -m app.calibrate {cmd} <file>")
            return 2
        path = Path(args[1])
        if not path.exists():
            print(f"no such file: {path}")
            return 1
        return cmd_solve(path) if cmd == "solve" else cmd_inspect(path)
    print(f"unknown command '{cmd}'")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
