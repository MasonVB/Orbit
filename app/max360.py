"""max360 - decode GoPro MAX .360 to equirectangular.

A .360 holds two HEVC tracks. Stacked, they form a 3x2 equi-angular cubemap:

    row 0 (front track)   LEFT    FRONT   RIGHT
    row 1 (rear track)    DOWN    BACK    UP

and 64-pixel overlap bands are spliced into each row at x=688 and x=3344,
carrying duplicated content either side of the FRONT/BACK face so the joins can
be blended.

Three things here that the reference OpenCL filter doesn't do:

* The reference derives its cut/overlap constants from the EAC width rather
  than the source width, putting the splice at x=666 instead of 688. Measured
  against a real file the boundaries are at 688 and 3408, so the constants are
  used unscaled against the source.
* It discards the overlap bands. They are cross-faded here, which is what they
  are for, so the face joins stop being visible.
* It samples nearest-neighbour. This uses bicubic.

Plus stabilisation, from the CORI/GRAV telemetry in the GPMF track.

Axis convention follows the reference kernel: +y points DOWN, +z forward.
"""
import numpy as np
import cv2

CUT = 688           # source column where the first overlap band begins
OVERLAP = 64        # width of each overlap band
BASE_WIDTH = 4096   # the resolution those constants were measured against

# face index in the 3x2 grid
TOP_LEFT, TOP_MIDDLE, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_MIDDLE, BOTTOM_RIGHT = range(6)


def equirect_rays(width, height, world_R=None):
    """Unit rays for an equirectangular canvas, in the kernel's frame."""
    phi = ((2.0 * np.arange(width, dtype=np.float32) + 0.5) / width - 1.0) * np.pi
    theta = ((2.0 * np.arange(height, dtype=np.float32) + 0.5) / height - 1.0) * (np.pi / 2)
    phi, theta = np.meshgrid(phi, theta)
    ct = np.cos(theta)
    xyz = np.stack([ct * np.sin(phi), np.sin(theta), ct * np.cos(phi)], axis=-1)
    if world_R is not None:
        xyz = xyz @ np.asarray(world_R, dtype=np.float32).T
    return xyz


def xyz_to_cube(xyz):
    """Ray -> (face index, face-local u, v), matching GoPro's face assignment."""
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    phi = np.arctan2(x, z)
    theta = np.arcsin(np.clip(y, -1.0, 1.0))

    q = np.pi / 4
    m_front = (phi >= -q) & (phi < q)
    m_left = (phi >= -(np.pi / 2 + q)) & (phi < -q)
    m_right = (phi >= q) & (phi < np.pi / 2 + q)
    m_back = ~(m_front | m_left | m_right)

    phi_norm = np.where(m_front, phi, 0.0)
    phi_norm = np.where(m_left, phi + np.pi / 2, phi_norm)
    phi_norm = np.where(m_right, phi - np.pi / 2, phi_norm)
    phi_norm = np.where(m_back, phi + np.where(phi > 0, -np.pi, np.pi), phi_norm)

    thr = np.arctan(np.cos(phi_norm))
    m_down = theta > thr
    m_up = theta < -thr
    side = ~(m_down | m_up)

    m_front &= side
    m_left &= side
    m_right &= side
    m_back &= side

    eps = 1e-12
    sx = np.where(np.abs(x) < eps, eps, x)
    sy = np.where(np.abs(y) < eps, eps, y)
    sz = np.where(np.abs(z) < eps, eps, z)

    u = np.zeros_like(x)
    v = np.zeros_like(x)
    face = np.zeros(x.shape, np.int32)

    def put(mask, uu, vv, f):
        np.copyto(u, uu, where=mask)
        np.copyto(v, vv, where=mask)
        np.copyto(face, np.int32(f), where=mask)

    put(m_right, -sz / sx, sy / sx, TOP_RIGHT)
    put(m_left, -sz / sx, -sy / sx, TOP_LEFT)
    put(m_front, sx / sz, sy / sz, TOP_MIDDLE)
    # UP and DOWN are stored rotated 270 degrees, BACK rotated 90
    ux, uy = -sx / sy, -sz / sy
    put(m_up, uy, -ux, BOTTOM_RIGHT)
    dx, dy = sx / sy, -sz / sy
    put(m_down, dy, -dx, BOTTOM_LEFT)
    bx, by = sx / sz, -sy / sz
    put(m_back, -by, bx, BOTTOM_MIDDLE)
    return face, u, v


def cube_to_eac(face, u, v, eac_w, eac_h):
    """Face-local coordinates -> pixel position in the un-spliced EAC image."""
    pad = 2.0
    u_pad, v_pad = pad / eac_w, pad / eac_h
    u = (2.0 / np.pi) * np.arctan(u) + 0.5
    v = (2.0 / np.pi) * np.arctan(v) + 0.5
    u_face = (face % 3).astype(np.float32)
    v_face = (face // 3).astype(np.float32)
    U = ((u + u_face) * (1.0 - 2.0 * u_pad) / 3.0 + u_pad) * eac_w
    V = (v * (0.5 - 2.0 * v_pad) + v_pad + 0.5 * v_face) * eac_h
    return U, V


def build_maps(out_w, out_h, src_w, src_h, world_R=None, blend=True):
    """Remap tables for one output frame.

    Returns (map1, map2, weight): sample the stacked source with both maps and
    lerp by weight. In the two overlap bands the maps point at the two
    duplicate copies of the same content; elsewhere they are identical.
    """
    scale = src_w / BASE_WIDTH
    cut = CUT * scale
    overlap = OVERLAP * scale
    eac_w = src_w - 2 * overlap
    eac_h = 2 * src_h                       # both tracks stacked

    xyz = equirect_rays(out_w, out_h, world_R)
    face, u, v = xyz_to_cube(xyz)
    U, V = cube_to_eac(face, u, v, eac_w, eac_h)

    # splice the overlap bands back in
    left = U < cut
    mid = (U >= cut) & (U < eac_w - cut)
    Xa = np.where(left, U, np.where(mid, U + overlap, U + 2 * overlap))
    Xb = Xa.copy()
    W = np.zeros_like(U)

    if blend:
        # left face -> FRONT/BACK extension
        zA = (U >= cut - overlap) & (U < cut)
        Xb = np.where(zA, U + overlap, Xb)
        W = np.where(zA, (U - (cut - overlap)) / overlap, W)
        # FRONT/BACK extension -> right face
        zB = (U >= eac_w - cut) & (U < eac_w - cut + overlap)
        Xa = np.where(zB, U + overlap, Xa)
        Xb = np.where(zB, U + 2 * overlap, Xb)
        W = np.where(zB, (U - (eac_w - cut)) / overlap, W)

    return (Xa.astype(np.float32), V.astype(np.float32),
            Xb.astype(np.float32), V.astype(np.float32),
            W.astype(np.float32))


class Rotator:
    """Applies a per-frame rotation as an equirect->equirect resample.

    Rebuilding the full EAC mapping every frame is the dominant cost of
    stabilised video, because deciding which cube face each ray lands on is
    expensive. The projection itself doesn't change frame to frame though -
    only the rotation does - so the EAC maps are built once and the rotation
    becomes a second, much cheaper pass over the finished equirect.
    """

    def __init__(self, width, height):
        lon = ((2.0 * np.arange(width, dtype=np.float32) + 0.5) / width - 1.0) * np.pi
        lat = ((2.0 * np.arange(height, dtype=np.float32) + 0.5) / height - 1.0) * (np.pi / 2)
        lon, lat = np.meshgrid(lon, lat)
        ct = np.cos(lat)
        self.rays = np.stack([ct * np.sin(lon), np.sin(lat), ct * np.cos(lon)],
                             axis=-1).astype(np.float32)
        self.w, self.h = width, height

    def maps(self, R):
        d = self.rays @ np.asarray(R, np.float32).T
        lon = np.arctan2(d[..., 0], d[..., 2])
        lat = np.arcsin(np.clip(d[..., 1], -1.0, 1.0))
        mx = (lon / np.pi + 1.0) * 0.5 * self.w - 0.5
        my = (lat / (np.pi / 2) + 1.0) * 0.5 * self.h - 0.5
        return mx.astype(np.float32), my.astype(np.float32)

    def apply(self, img, R, interp=cv2.INTER_LINEAR):
        mx, my = self.maps(R)
        # wrap in longitude, clamp in latitude
        return cv2.remap(img, mx, my, interp, borderMode=cv2.BORDER_WRAP)


def render(front, rear, maps, interp=cv2.INTER_CUBIC):
    """Stitch one stacked frame pair into equirectangular."""
    xa, ya, xb, yb, w = maps
    stacked = np.vstack([front, rear])
    a = cv2.remap(stacked, xa, ya, interp, borderMode=cv2.BORDER_REPLICATE)
    if not w.any():
        return a
    b = cv2.remap(stacked, xb, yb, interp, borderMode=cv2.BORDER_REPLICATE)
    w3 = w[..., None]
    return (a.astype(np.float32) * (1 - w3) + b.astype(np.float32) * w3).astype(np.uint8)


# ---------------------------------------------------------------------------
# stabilisation
# ---------------------------------------------------------------------------

def align_rotation(a, b):
    """Minimal rotation taking unit vector a onto unit vector b."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    b = np.asarray(b, float) / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(a @ b)
    s = np.linalg.norm(v)
    if s < 1e-9:
        return np.eye(3) if c > 0 else -np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))


def resample(series, n):
    """Resample a telemetry series to n frames."""
    series = np.asarray(series, float)
    if len(series) == n:
        return series
    src = np.linspace(0, 1, len(series))
    dst = np.linspace(0, 1, n)
    return np.stack([np.interp(dst, src, series[:, i]) for i in range(series.shape[1])], axis=1)


def stabilise_rotations(cori, grav, n_frames, mode="full"):
    """Per-frame world rotations that hold the scene still and the horizon flat.

    GoPro's GRAV is already in the projection's own axes - no mount offset to
    calibrate, unlike Insta360 - and CORI gives orientation relative to the
    first frame. Measured on a real clip, Q^T @ GRAV(t) reproduces GRAV(0) to
    0.18 degrees mean, so the two agree closely.

    mode:
      full     cancel all camera motion, then level once. Rock steady.
      horizon  level every frame independently. Keeps intentional panning,
               removes roll and pitch. Usually what you want handheld.
      none     no correction.
    """
    from .gpmf import quat_to_matrix

    if mode == "none":
        return [None] * n_frames

    grav = np.asarray(grav, float)
    grav = grav / np.linalg.norm(grav, axis=1, keepdims=True)
    grav = resample(grav, n_frames)
    grav /= np.linalg.norm(grav, axis=1, keepdims=True)

    if mode == "horizon":
        # +y is down, so drag output-down onto measured gravity each frame
        return [align_rotation([0, 1, 0], grav[i]) for i in range(n_frames)]

    cori = resample(np.asarray(cori, float), n_frames)
    R_level = align_rotation([0, 1, 0], grav[0])
    out = []
    for i in range(n_frames):
        Q = quat_to_matrix(cori[i])
        out.append(Q @ R_level)
    return out
