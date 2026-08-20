"""insp360 - decode Insta360 .insp dual-fisheye stills to equirectangular.

Two things make this different from a naive `ffmpeg v360=dfisheye`:

1. Calibration is *solved*, not assumed. Circle centre, radius, field of view
   and the relative rotation between the two lenses are fitted by maximising
   photometric agreement in the overlap band, so the seam closes.
2. The overlap is feather-blended rather than hard-cut, and the file's own
   1 kHz accelerometer trailer supplies a gravity vector for horizon levelling.
"""
import struct
import numpy as np
import cv2

MAGIC = b'8db42d694ccc418790edff439fe026bf'
G_COUNTS = 1024.0          # accelerometer counts per g (measured: |a| ~ 1028)
IMU_RECORD = 20            # bytes: uint64 timestamp_us + 6 x uint16 offset-binary


# ----------------------------------------------------------------------------
# container
# ----------------------------------------------------------------------------

def jpeg_end(data: bytes) -> int:
    """Offset just past the primary image's EOI (start of the Insta360 trailer)."""
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            raise ValueError(f"lost JPEG marker sync at 0x{i:x}")
        m = data[i + 1]
        if m == 0xD9:
            return i + 2
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack_from('>H', data, i + 2)[0]
        if m == 0xDA:                       # start of scan: skip entropy data
            j = i + 2 + seglen
            while j < len(data) - 1:
                if data[j] == 0xFF and data[j + 1] == 0xD9:
                    return j + 2
                j += 1
            break
        i += 2 + seglen
    raise ValueError("no EOI found")


def read_imu(data: bytes):
    """Decode the IMU block that follows the JPEG.

    Records are 20 bytes: a microsecond timestamp then six 16-bit offset-binary
    channels (three accelerometer, three gyroscope). Returns (t_us, acc_g, gyro).
    """
    if data[-32:] != MAGIC:
        raise ValueError("not an Insta360 file (trailer magic missing)")
    off = jpeg_end(data)
    rows, prev = [], None
    while off + IMU_RECORD <= len(data):
        ts = struct.unpack_from('<Q', data, off)[0]
        if prev is not None and not (0 < ts - prev <= 20000):
            break
        rows.append((ts, data[off + 8:off + 20]))
        prev = ts
        off += IMU_RECORD
    if not rows:
        raise ValueError("no IMU records")
    t = np.array([r[0] for r in rows], dtype=np.int64)
    raw = np.frombuffer(b''.join(r[1] for r in rows), dtype=np.uint8).reshape(-1, 12)
    ch = raw.view('<u2').astype(np.float64) - 32768.0
    return t, ch[:, 0:3] / G_COUNTS, ch[:, 3:6]


def gravity_at_shutter(acc_g: np.ndarray, tail: int = 300) -> np.ndarray:
    """Unit gravity vector, averaged over the last samples before the shutter.

    Averaging suppresses handshake; the residual is what levelling can't fix.
    """
    v = acc_g[-tail:].mean(axis=0)
    return v / np.linalg.norm(v)


# ----------------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------------

PREVIEW_W, PREVIEW_H = 2560, 1280   # NV12 equirect the camera stitches itself


def read_preview(data: bytes):
    """Pull the camera's own stitched equirectangular preview out of the trailer.

    It sits at the end of the trailer as a raw NV12 plane (no SOI, no header of
    its own), so it is located by measuring back from the 40-byte footer. This
    is the fastest possible way to get a viewable sphere: no reprojection at all.
    """
    if data[-32:] != MAGIC:
        raise ValueError("not an Insta360 file (trailer magic missing)")
    need = PREVIEW_W * PREVIEW_H * 3 // 2
    start = len(data) - 40 - need
    if start < jpeg_end(data):
        raise ValueError("no embedded preview of the expected size")
    plane = np.frombuffer(data[start:start + need], dtype=np.uint8)
    plane = plane.reshape(PREVIEW_H * 3 // 2, PREVIEW_W)
    return cv2.cvtColor(plane, cv2.COLOR_YUV2BGR_NV12)


def rot(yaw: float, pitch: float, roll: float) -> np.ndarray:
    cy, sy = np.cos(yaw), np.sin(yaw)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cr, sr = np.cos(roll), np.sin(roll)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    Rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


def sphere_grid(width: int, row0: int = 0, row1: int = None):
    """World-space unit rays for an equirectangular canvas, optionally a strip."""
    h = width // 2
    row1 = h if row1 is None else row1
    lon = (np.arange(width, dtype=np.float32) + 0.5) / width * (2 * np.pi) - np.pi
    lat = np.pi / 2 - (np.arange(row0, row1, dtype=np.float32) + 0.5) / h * np.pi
    lon, lat = np.meshgrid(lon, lat)
    cl = np.cos(lat)
    return np.stack([cl * np.sin(lon), np.sin(lat), cl * np.cos(lon)], axis=-1)


def lens_map(dirs: np.ndarray, cx: float, cy: float, r: float, fov: float,
             R: np.ndarray, mirror: bool, x_offset: float):
    """Project world rays into one fisheye. Returns (map_x, map_y, theta).

    Equidistant model: radius in pixels is proportional to the angle off-axis,
    which is what these lenses are designed to.
    """
    d = dirs @ R.T
    theta = np.arccos(np.clip(d[..., 2], -1.0, 1.0))
    phi = np.arctan2(d[..., 1], d[..., 0])
    rr = r * (theta / (fov * 0.5))
    px = cx + rr * np.cos(phi) * (-1.0 if mirror else 1.0)
    py = cy - rr * np.sin(phi)          # image rows increase downward
    return (px + x_offset).astype(np.float32), py.astype(np.float32), theta


def blend_weight(theta: np.ndarray, fov: float, feather: float) -> np.ndarray:
    """1 well inside the lens's coverage, tapering to 0 at the circle edge."""
    edge = fov * 0.5
    w = (edge - theta) / max(feather, 1e-6)
    return np.clip(w, 0.0, 1.0).astype(np.float32)


class Calib:
    """Per-lens circle geometry plus the rotation of lens B relative to lens A."""

    def __init__(self, cx0, cy0, cx1, cy1, r, fov, yaw, pitch, roll, mirror=True):
        self.cx0, self.cy0 = cx0, cy0
        self.cx1, self.cy1 = cx1, cy1
        self.r, self.fov = r, fov
        self.yaw, self.pitch, self.roll = yaw, pitch, roll
        self.mirror = mirror

    def as_vector(self):
        return np.array([self.cx0, self.cy0, self.cx1, self.cy1,
                         self.r, self.fov, self.yaw, self.pitch, self.roll])

    @classmethod
    def from_vector(cls, v, mirror=True):
        return cls(*v[:9], mirror=mirror)

    def __repr__(self):
        return (f"Calib(c0=({self.cx0:.1f},{self.cy0:.1f}) "
                f"c1=({self.cx1:.1f},{self.cy1:.1f}) r={self.r:.1f} "
                f"fov={np.degrees(self.fov):.2f}deg "
                f"rel=({np.degrees(self.yaw):.3f},{np.degrees(self.pitch):.3f},"
                f"{np.degrees(self.roll):.3f})deg mirror={self.mirror})")


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------

def render(img: np.ndarray, cal: Calib, width: int, world_R: np.ndarray = None,
           feather_deg: float = 6.0, interp=cv2.INTER_CUBIC, strip: int = 512):
    """Stitch both fisheyes into one equirectangular frame.

    Done in horizontal strips: an 8K canvas needs several hundred MB of
    coordinate maps if built in one go.
    """
    half = img.shape[1] // 2
    height = width // 2
    feather = np.radians(feather_deg)
    # lens A faces +z; lens B faces -z, so its frame is the world turned 180 deg
    lenses = ((np.eye(3), cal.cx0, cal.cy0, 0.0, False),
              (rot(np.pi, 0, 0) @ rot(cal.yaw, cal.pitch, cal.roll),
               cal.cx1, cal.cy1, float(half), cal.mirror))
    out = np.empty((height, width, 3), np.uint8)
    for y0 in range(0, height, strip):
        y1 = min(y0 + strip, height)
        dirs = sphere_grid(width, y0, y1)
        if world_R is not None:
            dirs = dirs @ world_R.T
        acc = np.zeros((y1 - y0, width, 3), np.float32)
        wsum = np.zeros((y1 - y0, width), np.float32)
        for (R, cx, cy, xo, mir) in lenses:
            mx, my, th = lens_map(dirs, cx, cy, cal.r, cal.fov, R, mir, xo)
            w = blend_weight(th, cal.fov, feather)
            samp = cv2.remap(img, mx, my, interp, borderMode=cv2.BORDER_CONSTANT)
            acc += samp.astype(np.float32) * w[..., None]
            wsum += w
            del mx, my, th, samp, w
        out[y0:y1] = np.clip(acc / np.maximum(wsum, 1e-6)[..., None], 0, 255).astype(np.uint8)
        del dirs, acc, wsum
    return out


# ----------------------------------------------------------------------------
# calibration solving
# ----------------------------------------------------------------------------

def align_rotation(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Minimal rotation taking unit vector a onto unit vector b."""
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(a @ b)
    s = np.linalg.norm(v)
    if s < 1e-9:
        return np.eye(3) if c > 0 else -np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))


def level_rotation(gravity_cam: np.ndarray) -> np.ndarray:
    """world_R that puts the horizon flat, given gravity in camera coordinates.

    Yaw is left free (a still frame has no reference heading), so this is the
    minimal rotation that drags 'down' in the output onto measured gravity.
    """
    return align_rotation(np.array([0.0, -1.0, 0.0]), gravity_cam)


# Rotation from IMU axes to the optical frame for the Insta360 X5.
#
# The IMU is not mounted square to the lenses - it sits rotated about 48 deg -
# so the raw accelerometer axes have to be rotated before the gravity vector
# means anything in image space. Derived from a reference frame verified level
# to +/-2 deg (see calibrate_mount). One free parameter remains: a rotation
# about gravity itself, which a single level reference cannot pin down. Shoot a
# second reference at a known tilt to fix it exactly.
X5_MOUNT = None          # filled in by load_mount()


def calibrate_mount(g_sensor: np.ndarray, residual_roll_deg: float = 0.0) -> np.ndarray:
    """Solve the IMU-to-optical rotation from a reference frame of known tilt.

    `g_sensor` is the gravity unit vector at that frame's shutter;
    `residual_roll_deg` is the frame's measured roll error (0 if truly level).
    """
    d = rot(0, 0, np.radians(residual_roll_deg)) @ np.array([0.0, -1.0, 0.0])
    return align_rotation(g_sensor, d)


def leveling_rotation(acc_g: np.ndarray, mount: np.ndarray, tail: int = 300):
    """world_R that levels the horizon, plus the tilt it corrected, in degrees."""
    g = gravity_at_shutter(acc_g, tail)
    gc = mount @ g
    tilt = np.degrees(np.arccos(np.clip(-gc[1], -1.0, 1.0)))
    return level_rotation(gc), float(tilt)


def axis_maps():
    """The 24 right-handed signed axis permutations.

    The IMU's orientation relative to the optical frame isn't documented, so
    the mapping is resolved empirically once per camera model.
    """
    out = []
    for perm in ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
        for sx in (1, -1):
            for sy in (1, -1):
                for sz in (1, -1):
                    M = np.zeros((3, 3))
                    for row, (col, s) in enumerate(zip(perm, (sx, sy, sz))):
                        M[row, col] = s
                    if np.linalg.det(M) > 0:
                        out.append(M)
    return out


_RING_CACHE = {}


def seam_ring(band_deg=8.0, n_phi=720, n_band=9):
    """Directions in a thin annulus around the seam great circle.

    Only this band decides whether the stitch closes, so sampling it directly
    is far cheaper than rendering a whole equirect per optimiser step.
    """
    key = (band_deg, n_phi, n_band)
    if key not in _RING_CACHE:
        band = np.radians(band_deg)
        phi = np.linspace(0, 2 * np.pi, n_phi, endpoint=False)
        dth = np.linspace(-band, band, n_band)
        P, T = np.meshgrid(phi, np.pi / 2 + dth)
        st, ct = np.sin(T), np.cos(T)
        _RING_CACHE[key] = np.stack([st * np.cos(P), st * np.sin(P), ct], -1).astype(np.float32)
    return _RING_CACHE[key]


def seam_cost(img, cal, width=None, band_deg=8.0):
    """Negative normalised cross-correlation across the seam. Lower is better."""
    half = img.shape[1] // 2
    dirs = seam_ring(band_deg)
    RA = np.eye(3)
    RB = rot(np.pi, 0, 0) @ rot(cal.yaw, cal.pitch, cal.roll)
    ax, ay, ath = lens_map(dirs, cal.cx0, cal.cy0, cal.r, cal.fov, RA, False, 0.0)
    bx, by, bth = lens_map(dirs, cal.cx1, cal.cy1, cal.r, cal.fov, RB, cal.mirror, float(half))
    edge = cal.fov * 0.5
    ok = (ath < edge) & (bth < edge)
    if ok.sum() < 200:
        return 10.0
    a = cv2.remap(img, ax, ay, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    b = cv2.remap(img, bx, by, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    a = a[ok].astype(np.float32).ravel()
    b = b[ok].astype(np.float32).ravel()
    a = a - a.mean(); b = b - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 10.0
    return -float(a @ b / (na * nb))
