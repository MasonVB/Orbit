#!/usr/bin/env python3
"""Smoke test - exercises Orbit end to end without a browser or a server.

    python3 smoke_test.py                      # API, auth, folders, sharing
    python3 smoke_test.py FILE [FILE ...]      # also import real media

Give it a .insp, a .360, or an already-equirectangular jpg/mp4 and it will run
each one through detection, the worker, and media delivery, then report what
came out. Runs against a scratch directory; nothing touches your real library.
"""
import os
import shutil
import sys
import tempfile
import time

DATA = tempfile.mkdtemp(prefix="orbit-smoke-")
# Forced, not setdefault: the test must run against its own scratch library
# with its own credentials, whatever the surrounding shell has exported.
# Otherwise it writes into your real ./data and logs in with the wrong password.
os.environ["ORBIT_DATA"] = DATA
os.environ["ORBIT_ADMIN_PASSWORD"] = "smoketest"
os.environ["ORBIT_PUBLIC_URL"] = "http://localhost:8899"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(label, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(label)
    mark = "ok  " if ok else "FAIL"
    detail = "" if ok else f"   (got {got!r}, wanted {want!r})"
    print(f"  [{mark}] {label}{detail}")
    return ok


def main(paths):
    print(f"scratch directory: {DATA}\n")
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:
        sys.exit(f"missing dependency: {exc}\n"
                 "  pip install -r requirements.txt httpx")

    from app.main import app
    from app import worker

    c = TestClient(app)

    print("core API")
    check("anonymous access refused", c.get("/api/root").status_code, 401)
    check("bad password refused",
          c.post("/api/auth/login",
                 data={"username": "admin", "password": "wrong"}).status_code, 401)
    check("login",
          c.post("/api/auth/login",
                 data={"username": "admin", "password": "smoketest"}).status_code, 200)
    root = c.get("/api/root").json()["id"]
    folder = c.post("/api/folders", data={"parent_id": root, "name": "Smoke"}).json()["id"]
    check("duplicate folder refused",
          c.post("/api/folders", data={"parent_id": root, "name": "Smoke"}).status_code, 409)
    check("unsupported upload refused",
          c.post("/api/upload", data={"folder_id": folder},
                 files={"file": ("x.txt", b"hi", "text/plain")}).status_code, 415)

    print("\nsharing")
    share = c.post("/api/shares", data={
        "target_type": "folder", "target_id": folder, "label": "Smoke",
        "password": "guest", "expires_days": 1, "allow_download": 0}).json()
    anon = TestClient(app)
    anon.cookies.clear()
    check("share requires password", anon.get(f"/api/s/{share['token']}").status_code, 403)
    check("share opens with password",
          anon.get(f"/api/s/{share['token']}",
                   headers={"X-Share-Password": "guest"}).status_code, 200)
    print(f"         link: {share['url']}")

    if not paths:
        print("\n(no media given - pass file paths to test importing)")
        return

    from app.formats import probe
    from pathlib import Path

    for path in paths:
        p = Path(path)
        print(f"\n{p.name}")
        if not p.exists():
            check("file exists", False, True)
            continue

        det = probe(p)
        print(f"         detected: {det.source_format} / {det.projection} "
              f"-> pipeline '{det.pipeline}'")
        if det.camera_make or det.camera_model:
            print(f"         camera:   {det.camera_make} {det.camera_model}".rstrip())
        if det.gopro_tracks:
            print(f"         tracks:   front={det.gopro_tracks['front']} "
                  f"rear={det.gopro_tracks['rear']} meta={det.gopro_tracks['meta']}")
        if det.insta360_trailer:
            print(f"         trailer:  yes, embedded preview="
                  f"{'yes' if det.has_embedded_preview else 'no'}")
        if not check("recognised as 360 media", det.pipeline != "unsupported", True):
            print(f"         reason: {det.note}")
            continue

        with p.open("rb") as fh:
            up = c.post("/api/upload", data={"folder_id": folder},
                        files={"file": (p.name, fh, "application/octet-stream")})
        if not check("upload accepted", up.status_code, 200):
            continue
        item = up.json()["id"]

        t0 = time.time()
        job = worker.claim()
        try:
            worker.process(job)
        except Exception as exc:
            check("worker completed", f"raised {type(exc).__name__}", "ok")
            print(f"         {exc}")
            continue
        elapsed = time.time() - t0

        it = c.get(f"/api/items/{item}").json()
        check("processed", it["status"], "ready")
        if it.get("error"):
            print(f"         error: {it['error']}")
        print(f"         took {elapsed:.1f}s"
              + (f" for {it['duration']:.1f}s of footage" if it.get("duration") else ""))
        for d in sorted(it["derivatives"], key=lambda x: x["kind"]):
            print(f"         {d['kind']:12} {d['width']}x{d['height']}  "
                  f"{d['size_bytes'] / 1e6:.2f} MB")
        for kind in [d["kind"] for d in it["derivatives"]]:
            check(f"serves {kind}",
                  c.get(f"/api/items/{item}/media/{kind}").status_code, 200)
        biggest = max(it["derivatives"], key=lambda d: d["size_bytes"])["kind"]
        r = c.get(f"/api/items/{item}/media/{biggest}", headers={"Range": "bytes=0-999"})
        check("range requests work", r.status_code, 206)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    finally:
        print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            print("failed: " + ", ".join(FAIL))
        keep = os.environ.get("ORBIT_KEEP_SCRATCH")
        if keep:
            print(f"scratch kept at {DATA}")
        else:
            shutil.rmtree(DATA, ignore_errors=True)
        sys.exit(1 if FAIL else 0)
