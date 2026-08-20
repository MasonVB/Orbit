"""HTTP layer: library browsing, uploads, media delivery, share links."""
import mimetypes
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import (
    ADMIN_PASSWORD, ADMIN_USER, DERIVED, ORIGINALS, PUBLIC_BASE_URL, TMP,
)
from .formats import SUPPORTED_EXT

WEB = Path(__file__).resolve().parent.parent / "web"
SESSION_COOKIE = "orbit_session"
SESSION_TTL = 60 * 60 * 24 * 30
CHUNK = 1024 * 512

def bootstrap() -> None:
    """Idempotent. Called at import so the schema exists before any request,
    regardless of worker/web start order or ASGI lifespan behaviour."""
    db.init()
    if ADMIN_PASSWORD:
        with db.tx() as c:
            row = c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
            if not row:
                h, s = db.hash_password(ADMIN_PASSWORD)
                c.execute(
                    "INSERT INTO users (username, pw_hash, pw_salt, is_admin, created_at) "
                    "VALUES (?,?,?,1,?)",
                    (ADMIN_USER, h, s, time.time()),
                )


bootstrap()

app = FastAPI(title="Orbit", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with db.tx() as c:
        row = c.execute(
            "SELECT u.id, u.username, u.is_admin FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token=? AND s.expires_at > ?",
            (token, time.time()),
        ).fetchone()
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    return user


@app.post("/api/auth/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    with db.tx() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not db.verify_password(password, row["pw_hash"], row["pw_salt"]):
            time.sleep(0.4)
            raise HTTPException(401, "That username and password don't match.")
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                  (token, row["id"], time.time() + SESSION_TTL))
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_TTL, path="/")
    return {"username": row["username"], "is_admin": bool(row["is_admin"])}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = current_user(request)
    return user or JSONResponse({"error": "anonymous"}, status_code=401)


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------

def item_json(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "kind": row["kind"],
        "status": row["status"], "error": row["error"],
        "source_format": row["source_format"], "projection": row["projection"],
        "camera": " ".join(x for x in [row["camera_make"], row["camera_model"]] if x).strip(),
        "width": row["width"], "height": row["height"], "duration": row["duration"],
        "size_bytes": row["size_bytes"], "created_at": row["created_at"],
    }


@app.get("/api/folders/{folder_id}")
def get_folder(folder_id: int, user=Depends(require_user)):
    with db.tx() as c:
        folder = c.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
        if not folder:
            raise HTTPException(404, "Folder not found.")
        children = c.execute(
            "SELECT id, name FROM folders WHERE parent_id=? ORDER BY name COLLATE NOCASE",
            (folder_id,)).fetchall()
        items = c.execute(
            "SELECT * FROM items WHERE folder_id=? ORDER BY COALESCE(captured_at, created_at) DESC",
            (folder_id,)).fetchall()
        counts = c.execute(
            "SELECT status, COUNT(*) n FROM items WHERE folder_id=? GROUP BY status",
            (folder_id,)).fetchall()
        return {
            "folder": {"id": folder["id"], "name": folder["name"],
                       "parent_id": folder["parent_id"]},
            "breadcrumb": db.folder_path(c, folder_id),
            "folders": [dict(r) for r in children],
            "items": [item_json(r) for r in items],
            "counts": {r["status"]: r["n"] for r in counts},
        }


@app.get("/api/root")
def root_folder(user=Depends(require_user)):
    with db.tx() as c:
        row = c.execute("SELECT id FROM folders WHERE parent_id IS NULL ORDER BY id LIMIT 1").fetchone()
    return {"id": row["id"]}


@app.post("/api/folders")
def create_folder(parent_id: int = Form(...), name: str = Form(...), user=Depends(require_user)):
    name = name.strip()
    if not name or "/" in name:
        raise HTTPException(400, "Folder names can't be empty or contain a slash.")
    with db.tx() as c:
        try:
            cur = c.execute(
                "INSERT INTO folders (parent_id, name, created_at) VALUES (?,?,?)",
                (parent_id, name, time.time()))
        except Exception:
            raise HTTPException(409, "A folder with that name already exists here.")
        return {"id": cur.lastrowid, "name": name}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int, user=Depends(require_user)):
    with db.tx() as c:
        folder = c.execute("SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
        if not folder:
            raise HTTPException(404, "Folder not found.")
        if folder["parent_id"] is None:
            raise HTTPException(400, "The top-level folder can't be deleted.")
        ids = db.descendant_folder_ids(c, folder_id)
        marks = ",".join("?" * len(ids))
        for row in c.execute(f"SELECT id, original_path FROM items WHERE folder_id IN ({marks})", ids):
            Path(row["original_path"]).unlink(missing_ok=True)
            shutil.rmtree(DERIVED / str(row["id"]), ignore_errors=True)
        c.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    return {"ok": True}


@app.patch("/api/items/{item_id}")
def move_or_rename(item_id: int, folder_id: int = Form(None), name: str = Form(None),
                   user=Depends(require_user)):
    with db.tx() as c:
        if folder_id is not None:
            c.execute("UPDATE items SET folder_id=? WHERE id=?", (folder_id, item_id))
        if name:
            c.execute("UPDATE items SET name=? WHERE id=?", (name.strip(), item_id))
    return {"ok": True}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int, user=Depends(require_user)):
    with db.tx() as c:
        row = c.execute("SELECT original_path FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Item not found.")
        Path(row["original_path"]).unlink(missing_ok=True)
        shutil.rmtree(DERIVED / str(item_id), ignore_errors=True)
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
    return {"ok": True}


@app.post("/api/items/{item_id}/reprocess")
def reprocess(item_id: int, user=Depends(require_user)):
    shutil.rmtree(DERIVED / str(item_id), ignore_errors=True)
    with db.tx() as c:
        c.execute("DELETE FROM derivatives WHERE item_id=?", (item_id,))
        c.execute("UPDATE items SET status='pending', error=NULL WHERE id=?", (item_id,))
        c.execute("INSERT INTO jobs (item_id, created_at) VALUES (?,?)", (item_id, time.time()))
    return {"ok": True}


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

@app.post("/api/upload")
async def upload(folder_id: int = Form(...), file: UploadFile = File(...),
                 user=Depends(require_user)):
    name = Path(file.filename or "upload.bin").name
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise HTTPException(
            415, f"Orbit doesn't handle {ext or 'files without an extension'} yet.")

    staging = TMP / f"{secrets.token_hex(8)}{ext}"
    size = 0
    with staging.open("wb") as out:
        while chunk := await file.read(CHUNK):
            size += len(chunk)
            out.write(chunk)

    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO items (folder_id, name, original_path, size_bytes, status, created_at) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (folder_id, name, "", size, time.time()))
        item_id = cur.lastrowid
        final_dir = ORIGINALS / str(item_id)
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / name
        shutil.move(str(staging), final)
        c.execute("UPDATE items SET original_path=? WHERE id=?", (str(final), item_id))
        c.execute("INSERT INTO jobs (item_id, created_at) VALUES (?,?)", (item_id, time.time()))

    return {"id": item_id, "name": name, "status": "pending"}


# --------------------------------------------------------------------------
# Media delivery (Range-aware, so video seeking works)
# --------------------------------------------------------------------------

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def serve_file(path: Path, request: Request, download_name: str | None = None) -> Response:
    if not path.exists():
        raise HTTPException(404, "That file isn't on disk.")
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=86400"}
    if download_name:
        headers["Content-Disposition"] = f'attachment; filename="{download_name}"'

    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type=mime, headers=headers)

    m = RANGE_RE.match(rng)
    if not m:
        raise HTTPException(416, "Malformed Range header.")
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, "Range outside the file.")
    length = end - start + 1

    def stream():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                data = fh.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers |= {"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)}
    return StreamingResponse(stream(), status_code=206, media_type=mime, headers=headers)


def derivative_path(item_id: int, kind: str) -> Path:
    with db.tx() as c:
        row = c.execute("SELECT path FROM derivatives WHERE item_id=? AND kind=?",
                        (item_id, kind)).fetchone()
    if not row:
        raise HTTPException(404, "That version hasn't been generated.")
    return Path(row["path"])


@app.get("/api/items/{item_id}/media/{kind}")
def media(item_id: int, kind: str, request: Request, user=Depends(require_user)):
    if kind == "original":
        with db.tx() as c:
            row = c.execute("SELECT original_path, name FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Item not found.")
        return serve_file(Path(row["original_path"]), request, download_name=row["name"])
    return serve_file(derivative_path(item_id, kind), request)


@app.get("/api/items/{item_id}")
def get_item(item_id: int, user=Depends(require_user)):
    with db.tx() as c:
        row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Item not found.")
        derivs = c.execute("SELECT kind, width, height, size_bytes FROM derivatives WHERE item_id=?",
                           (item_id,)).fetchall()
        job = c.execute("SELECT state, progress, error FROM jobs WHERE item_id=? "
                        "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    out = item_json(row)
    out["derivatives"] = [dict(d) for d in derivs]
    out["job"] = dict(job) if job else None
    return out


# --------------------------------------------------------------------------
# Sharing
# --------------------------------------------------------------------------

@app.post("/api/shares")
def create_share(target_type: str = Form(...), target_id: int = Form(...),
                 label: str = Form(""), password: str = Form(""),
                 expires_days: int = Form(0), allow_download: int = Form(1),
                 user=Depends(require_user)):
    if target_type not in ("folder", "item"):
        raise HTTPException(400, "Share a folder or an item.")
    token = secrets.token_urlsafe(16)
    pw_hash = pw_salt = None
    if password:
        pw_hash, pw_salt = db.hash_password(password)
    expires_at = time.time() + expires_days * 86400 if expires_days else None
    with db.tx() as c:
        c.execute(
            "INSERT INTO shares (token, target_type, target_id, label, pw_hash, pw_salt, "
            "expires_at, allow_download, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, target_type, target_id, label, pw_hash, pw_salt, expires_at,
             int(allow_download), user["id"], time.time()))
    return {"token": token, "url": f"{PUBLIC_BASE_URL}/s/{token}"}


@app.get("/api/shares")
def list_shares(user=Depends(require_user)):
    with db.tx() as c:
        rows = c.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            name = None
            table = "folders" if r["target_type"] == "folder" else "items"
            t = c.execute(f"SELECT name FROM {table} WHERE id=?", (r["target_id"],)).fetchone()
            if t:
                name = t["name"]
            out.append({
                "id": r["id"], "token": r["token"], "target_type": r["target_type"],
                "target_id": r["target_id"], "target_name": name, "label": r["label"],
                "has_password": bool(r["pw_hash"]), "expires_at": r["expires_at"],
                "allow_download": bool(r["allow_download"]), "view_count": r["view_count"],
                "url": f"{PUBLIC_BASE_URL}/s/{r['token']}",
            })
    return out


@app.delete("/api/shares/{share_id}")
def revoke_share(share_id: int, user=Depends(require_user)):
    with db.tx() as c:
        c.execute("DELETE FROM shares WHERE id=?", (share_id,))
    return {"ok": True}


def load_share(token: str, request: Request) -> dict:
    with db.tx() as c:
        row = c.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "This link is no longer active.")
    if row["expires_at"] and row["expires_at"] < time.time():
        raise HTTPException(410, "This link has expired.")
    if row["pw_hash"]:
        supplied = request.headers.get("x-share-password") or request.query_params.get("pw", "")
        if not supplied or not db.verify_password(supplied, row["pw_hash"], row["pw_salt"]):
            raise HTTPException(403, "This link needs a password.")
    return dict(row)


def share_allows_item(share: dict, item_id: int) -> bool:
    if share["target_type"] == "item":
        return share["target_id"] == item_id
    with db.tx() as c:
        row = c.execute("SELECT folder_id FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return False
        return row["folder_id"] in db.descendant_folder_ids(c, share["target_id"])


@app.get("/api/s/{token}")
def share_manifest(token: str, request: Request, folder: int | None = None):
    share = load_share(token, request)
    with db.tx() as c:
        c.execute("UPDATE shares SET view_count=view_count+1 WHERE id=?", (share["id"],))

    if share["target_type"] == "item":
        with db.tx() as c:
            row = c.execute("SELECT * FROM items WHERE id=? AND status IN ('ready','preview')",
                            (share["target_id"],)).fetchone()
            if not row:
                raise HTTPException(404, "This item isn't available.")
            derivs = c.execute("SELECT kind, width, height FROM derivatives WHERE item_id=?",
                               (row["id"],)).fetchall()
        payload = item_json(row)
        payload["derivatives"] = [dict(d) for d in derivs]
        return {"mode": "item", "allow_download": bool(share["allow_download"]),
                "label": share["label"], "item": payload}

    root = share["target_id"]
    current = folder or root
    with db.tx() as c:
        allowed = db.descendant_folder_ids(c, root)
        if current not in allowed:
            raise HTTPException(403, "That folder isn't part of this link.")
        f = c.execute("SELECT id, name FROM folders WHERE id=?", (current,)).fetchone()
        subs = c.execute("SELECT id, name FROM folders WHERE parent_id=? ORDER BY name COLLATE NOCASE",
                         (current,)).fetchall()
        items = c.execute(
            "SELECT * FROM items WHERE folder_id=? AND status IN ('ready','preview') "
            "ORDER BY COALESCE(captured_at, created_at) DESC", (current,)).fetchall()
        full = db.folder_path(c, current)
    trimmed = full[next((i for i, x in enumerate(full) if x["id"] == root), 0):]
    return {
        "mode": "folder", "allow_download": bool(share["allow_download"]),
        "label": share["label"] or f["name"], "root": root,
        "folder": {"id": f["id"], "name": f["name"]},
        "breadcrumb": trimmed,
        "folders": [dict(r) for r in subs],
        "items": [item_json(r) for r in items],
    }


@app.get("/api/s/{token}/items/{item_id}/media/{kind}")
def share_media(token: str, item_id: int, kind: str, request: Request):
    share = load_share(token, request)
    if not share_allows_item(share, item_id):
        raise HTTPException(403, "That item isn't part of this link.")
    if kind == "original" and not share["allow_download"]:
        raise HTTPException(403, "Downloads are turned off for this link.")
    if kind == "original":
        with db.tx() as c:
            row = c.execute("SELECT original_path, name FROM items WHERE id=?", (item_id,)).fetchone()
        return serve_file(Path(row["original_path"]), request, download_name=row["name"])
    return serve_file(derivative_path(item_id, kind), request)


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    with db.tx() as c:
        pending = c.execute("SELECT COUNT(*) n FROM jobs WHERE state IN ('queued','running')").fetchone()["n"]
    return {"ok": True, "queue": pending}


@app.get("/s/{token}")
def share_page(token: str):
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
