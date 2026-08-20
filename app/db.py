"""SQLite storage. WAL mode, no ORM - the schema is small enough to read."""
import sqlite3
import hashlib
import secrets
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  pw_salt TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(parent_id, name)
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  original_path TEXT NOT NULL,
  sha256 TEXT,
  size_bytes INTEGER,
  kind TEXT,                -- photo | video
  source_format TEXT,       -- insp | insv | gopromax360 | equirect_jpg | equirect_mp4 | ...
  projection TEXT,          -- dfisheye | eac | equirectangular | unknown
  camera_make TEXT,
  camera_model TEXT,
  width INTEGER,
  height INTEGER,
  duration REAL,
  captured_at REAL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|ready|failed|unsupported
  error TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_folder ON items(folder_id);

CREATE TABLE IF NOT EXISTS derivatives (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,       -- thumb | preview | master | video_2160 | video_1080
  path TEXT NOT NULL,
  mime TEXT,
  width INTEGER,
  height INTEGER,
  size_bytes INTEGER,
  UNIQUE(item_id, kind)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  progress TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY,
  token TEXT UNIQUE NOT NULL,
  target_type TEXT NOT NULL,   -- folder | item
  target_id INTEGER NOT NULL,
  label TEXT,
  pw_hash TEXT,
  pw_salt TEXT,
  expires_at REAL,
  allow_download INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id),
  created_at REAL NOT NULL,
  view_count INTEGER NOT NULL DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        row = c.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name='Library'").fetchone()
        if not row:
            c.execute(
                "INSERT INTO folders (parent_id, name, created_at) VALUES (NULL, 'Library', ?)",
                (time.time(),),
            )


# --- password hashing (stdlib scrypt, no extra dependency) -----------------

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32
    )
    return digest.hex(), salt


def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate, pw_hash)


def folder_path(conn, folder_id: int) -> list[dict]:
    """Breadcrumb from root to the given folder."""
    chain = []
    cur = folder_id
    while cur:
        row = conn.execute("SELECT id, name, parent_id FROM folders WHERE id=?", (cur,)).fetchone()
        if not row:
            break
        chain.append({"id": row["id"], "name": row["name"]})
        cur = row["parent_id"]
    return list(reversed(chain))


def descendant_folder_ids(conn, folder_id: int) -> list[int]:
    """All folders under (and including) folder_id - used for share scoping."""
    out, stack = [], [folder_id]
    while stack:
        fid = stack.pop()
        out.append(fid)
        for r in conn.execute("SELECT id FROM folders WHERE parent_id=?", (fid,)):
            stack.append(r["id"])
    return out
