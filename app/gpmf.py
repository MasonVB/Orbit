"""Minimal GPMF parser - enough to recover GoPro MAX orientation telemetry.

GPMF is a big-endian nested key/length/value format. Every item is:

    4 bytes  FourCC key
    1 byte   type      (0x00 means the payload is itself GPMF)
    1 byte   structure size, in bytes
    2 bytes  repeat count
    N bytes  payload, zero padded up to a 4-byte boundary

Values are stored as scaled integers; a sibling SCAL item carries the divisor.
"""
import struct
from collections import defaultdict

import numpy as np

# GPMF type char -> (numpy dtype, bytes per element)
TYPES = {
    b'b': ('>i1', 1), b'B': ('>u1', 1),
    b's': ('>i2', 2), b'S': ('>u2', 2),
    b'l': ('>i4', 4), b'L': ('>u4', 4),
    b'j': ('>i8', 8), b'J': ('>u8', 8),
    b'f': ('>f4', 4), b'd': ('>f8', 8),
}


def parse(buf, start=0, end=None):
    """Yield (key, type, payload_bytes, struct_size, repeat) at one level."""
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        key = buf[i:i + 4]
        typ = buf[i + 4:i + 5]
        ssize = buf[i + 5]
        repeat = struct.unpack('>H', buf[i + 6:i + 8])[0]
        length = ssize * repeat
        payload = buf[i + 8:i + 8 + length]
        yield key, typ, payload, ssize, repeat
        i += 8 + (length + 3) // 4 * 4     # pad to 4 bytes


def decode(typ, payload, ssize):
    """Decode a payload into an array, or text for character types."""
    if typ in (b'c', b'F'):
        return payload.rstrip(b'\x00').decode('utf-8', 'replace')
    spec = TYPES.get(typ)
    if not spec:
        return payload
    dtype, esize = spec
    a = np.frombuffer(payload[:len(payload) // esize * esize], dtype=dtype)
    cols = max(ssize // esize, 1)
    return a.reshape(-1, cols) if cols > 1 else a


def streams(buf):
    """Walk DEVC/STRM containers, returning {fourcc: [scaled arrays]}.

    SCAL divisors are applied, so ACCL comes back in m/s^2, GRAV as a unit
    vector, CORI/IORI as unit quaternions.
    """
    out = defaultdict(list)
    names = {}

    def walk_strm(payload):
        scal, items = None, []
        for key, typ, data, ssize, repeat in parse(payload):
            if key == b'SCAL':
                scal = np.asarray(decode(typ, data, ssize), dtype=np.float64).ravel()
            elif key == b'STNM':
                items.append(('__name__', decode(typ, data, ssize)))
            elif key not in (b'STMP', b'TSMP', b'TIMO', b'EMPT'):
                items.append((key.decode(), decode(typ, data, ssize)))
        for name, val in items:
            if name == '__name__':
                continue
            if isinstance(val, np.ndarray) and val.dtype.kind in 'iuf':
                v = val.astype(np.float64)
                if scal is not None and scal.size:
                    div = scal if scal.size == (v.shape[1] if v.ndim > 1 else 1) else scal[0]
                    v = v / div
                out[name].append(v)
            elif isinstance(val, str):
                names.setdefault(name, val)

    def walk(buf_, start=0, end=None):
        for key, typ, data, ssize, repeat in parse(buf_, start, end):
            if key == b'STRM':
                walk_strm(data)
            elif typ == b'\x00':
                walk(data)

    walk(buf)
    return {k: np.concatenate(v, axis=0) for k, v in out.items() if v}, names


def quat_to_matrix(q):
    """Unit quaternion (w, x, y, z) -> 3x3 rotation matrix."""
    w, x, y, z = q / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
