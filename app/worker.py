"""Job runner. Runs as its own container so a long encode never blocks the UI.

Claiming is done with a conditional UPDATE, so several workers can share the
queue safely without a broker.
"""
import logging
import time
import traceback
from pathlib import Path

from . import db
from .config import WORKER_CONCURRENCY
from .formats import probe
from .pipeline import build, quick_preview, PipelineError

log = logging.getLogger("orbit.worker")
MAX_ATTEMPTS = 3


def claim() -> dict | None:
    with db.tx() as c:
        row = c.execute(
            "SELECT id, item_id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        changed = c.execute(
            "UPDATE jobs SET state='running', attempts=attempts+1, started_at=? "
            "WHERE id=? AND state='queued'",
            (time.time(), row["id"]),
        ).rowcount
        if not changed:
            return None
        c.execute("UPDATE items SET status='processing' WHERE id=?", (row["item_id"],))
        return {"id": row["id"], "item_id": row["item_id"]}


def progress(job_id: int, message: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE jobs SET progress=? WHERE id=?", (message, job_id))


def save_derivatives(item_id: int, rows: list[dict]) -> None:
    with db.tx() as c:
        for d in rows:
            path = Path(d["path"])
            size = path.stat().st_size if path.exists() else 0
            c.execute(
                "INSERT INTO derivatives (item_id, kind, path, mime, width, height, size_bytes) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(item_id, kind) DO UPDATE SET "
                "path=excluded.path, mime=excluded.mime, width=excluded.width, "
                "height=excluded.height, size_bytes=excluded.size_bytes",
                (item_id, d["kind"], d["path"], d["mime"], d["width"], d["height"], size),
            )


def process(job: dict) -> None:
    job_id, item_id = job["id"], job["item_id"]
    with db.tx() as c:
        item = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        with db.tx() as c:
            c.execute("UPDATE jobs SET state='failed', error='item deleted' WHERE id=?", (job_id,))
        return

    src = Path(item["original_path"])
    if not src.exists():
        raise PipelineError(f"Original file missing: {src}")

    progress(job_id, "inspecting file")
    p = probe(src)

    with db.tx() as c:
        c.execute(
            "UPDATE items SET kind=?, source_format=?, projection=?, camera_make=?, "
            "camera_model=?, width=?, height=?, duration=?, captured_at=COALESCE(NULLIF(?,0), captured_at) "
            "WHERE id=?",
            (p.kind, p.source_format, p.projection, p.camera_make, p.camera_model,
             p.width, p.height, p.duration, p.captured_at, item_id),
        )

    # Phase 1: anything viewable without reprojection goes out immediately, so
    # an Insta360 photo appears in the grid within a second or two while the
    # real stitch runs behind it.
    try:
        early = quick_preview(item_id, src, p)
    except Exception as exc:
        log.warning("item %s quick preview failed: %s", item_id, exc)
        early = []
    if early:
        progress(job_id, "camera preview ready, stitching full resolution")
        save_derivatives(item_id, early)
        with db.tx() as c:
            c.execute("UPDATE items SET status='preview' WHERE id=?", (item_id,))

    if p.pipeline == "unsupported":
        with db.tx() as c:
            c.execute("UPDATE items SET status='unsupported', error=? WHERE id=?",
                      (p.note or "Unrecognised format", item_id))
            c.execute("UPDATE jobs SET state='done', finished_at=?, progress='skipped' WHERE id=?",
                      (time.time(), job_id))
        log.info("item %s unsupported: %s", item_id, p.note)
        return

    derivatives = build(item_id, src, p, on_progress=lambda m: progress(job_id, m))
    save_derivatives(item_id, derivatives)

    with db.tx() as c:
        c.execute("UPDATE items SET status='ready', error=NULL WHERE id=?", (item_id,))
        c.execute("UPDATE jobs SET state='done', finished_at=?, progress='complete' WHERE id=?",
                  (time.time(), job_id))
    log.info("item %s ready (%s -> %d derivatives)", item_id, p.source_format, len(derivatives))


def fail(job: dict, exc: Exception) -> None:
    message = str(exc)[:2000]
    with db.tx() as c:
        row = c.execute("SELECT attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()
        attempts = row["attempts"] if row else MAX_ATTEMPTS
        if attempts >= MAX_ATTEMPTS:
            c.execute("UPDATE jobs SET state='failed', error=?, finished_at=? WHERE id=?",
                      (message, time.time(), job["id"]))
            # if the camera preview came through, keep the item viewable
            has_preview = c.execute(
                "SELECT 1 FROM derivatives WHERE item_id=? AND kind='preview'",
                (job["item_id"],)).fetchone()
            c.execute("UPDATE items SET status=?, error=? WHERE id=?",
                      ('preview' if has_preview else 'failed', message, job["item_id"]))
        else:
            c.execute("UPDATE jobs SET state='queued', error=? WHERE id=?", (message, job["id"]))
    log.error("job %s failed (attempt %s): %s", job["id"], attempts, message)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init()
    log.info("worker up, concurrency=%s", WORKER_CONCURRENCY)
    idle = 0
    while True:
        job = claim()
        if not job:
            idle = min(idle + 1, 10)
            time.sleep(0.5 * idle)
            continue
        idle = 0
        try:
            process(job)
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the loop
            traceback.print_exc()
            fail(job, exc)


if __name__ == "__main__":
    main()
