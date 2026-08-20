# Orbit

A self-hosted 360 photo and video library for TrueNAS 25.10 (Goldeye). Folders like
Google Drive, an in-browser sphere viewer, and share links for individual items or
whole folder trees.

---

## 1. The one thing to understand before you build this

**Stitched files are easy. Raw camera files are hard, and no open-source tool matches
what the vendor's own software does.**

Insta360's FlowState stabilisation and horizon lock use gyroscope data in a
proprietary track. GoPro's `.360` uses a modified equi-angular cubemap that plain
ffmpeg gets visibly wrong. Orbit does its best on both, but "best" here means an
unstabilised, roughly-seamed sphere — fine for browsing an archive, not fine as a
final deliverable.

So Orbit is built around two paths, and you'll want both:

| You upload | What happens | Quality |
|---|---|---|
| Equirectangular export from Insta360 Studio / GoPro Player / Ricoh Theta | Transcoded to web proxies, metadata preserved | Exactly what your camera's software produced |
| Raw `.insp` photo | Solved calibration, feather-blended, horizon levelled from the file's own IMU | Close to vendor software |
| Raw `.360` video (GoPro MAX) | Native EAC projection, overlap-blended, stabilised from GoPro telemetry | Close to GoPro Player |
| Raw `.insv` video | Server-side reprojection with ffmpeg | Usable, unstabilised, seams visible on close inspection |

The practical workflow most people land on: dump raw files in for instant browsing
and search, then re-upload proper exports for the shots worth keeping. Orbit keeps
originals untouched either way, so you can always re-export later.

### Format support

| Source | Extension | Projection | Path | Notes |
|---|---|---|---|---|
| Insta360 X5/X4/X3/X2, ONE X/R/RS | `.insp` | dual-fisheye | solved stitch + IMU levelling | It's a JPEG with a proprietary trailer. Calibration is solved, not guessed — see §4. |
| Insta360 video | `.insv` | dual-fisheye | `v360` reprojection | `_00_` is full res, `_10_` is the camera's low-res proxy. Upload the `_00_`. No stabilisation. |
| GoPro MAX | `.360`, `.mp4` | GoPro EAC | native projection + telemetry stabilisation | No patched ffmpeg, no OpenCL. Detected by container shape, so renamed files still work — see §6. |
| Ricoh Theta | `.jpg` `.mp4` | equirectangular | passthrough | Carries GPano/spherical metadata. Works out of the box. |
| Any stitched export | `.jpg` `.mp4` `.mov` | equirectangular | passthrough | Detected via XMP-GPano or st3d/sv3d, with a 2:1 aspect-ratio fallback. |
| GoPro Fusion | two `.mp4` files | dual-fisheye pair | ✗ | Not supported. Fusion Studio's front/back pairing isn't implemented. |

Anything Orbit can't recognise is stored, marked `unsupported`, and left alone. It
never modifies or deletes an original.

---

## 2. Datasets

Create these in **Datasets**, not as folders. Recordsize matters a lot for media.

| Dataset | Recordsize | Purpose |
|---|---|---|
| `apps/orbit/src` | default | This repository |
| `apps/orbit/data` | **1M** | Originals, derivatives, SQLite |

Set `atime = off` on `apps/orbit/data`. Skip dedup — 360 files are already
compressed and dedup will just cost you RAM. `compression = lz4` is fine; it won't
do much on H.264 but costs nothing.

Ownership must be the TrueNAS apps user:

```
Dataset > Edit Permissions > Owner 568, Group 568, Apply recursively
```

**Budget storage generously.** An 8K Insta360 clip runs roughly 200 Mbit/s — about
1.5 GB per minute — and Orbit keeps the original *plus* a 4K and a 1080p proxy.
Expect derivatives to add 30–50% on top of your originals. Snapshot
`apps/orbit/data/originals`; there's no reason to snapshot `derived`, since deleting
it and re-queueing rebuilds everything.

## 3. Install and update

Orbit is deployed as a container image built by CI, so the NAS never needs a
source checkout or a compiler. Full walkthrough in §8; the short version:

```bash
# once, on your workstation
gh repo create orbit --private --source=. --push

# TrueNAS: Apps > Discover > Install via YAML, paste docker-compose.yaml
# updating later: bump the tag and redeploy
```

## 3b. Datasets and first run

Edit `docker-compose.yaml`: replace `YOURUSER` with your GitHub username,
every `POOL` with your pool name, set `ORBIT_ADMIN_PASSWORD`, and set
`ORBIT_PUBLIC_URL` to your chosen subdomain.

**Apps → Discover → ⋮ → Install via YAML**, paste the file, install.

Two containers come up: `orbit` (web) and `orbit-worker` (encoding). They're
separate so a 40-minute 8K encode never blocks the interface. Reach it on the LAN at
`http://truenas-ip:8899`.

`ORBIT_ADMIN_PASSWORD` only seeds the first account. Once that account exists the
variable is ignored, so you can blank it afterwards.

## 4. Calibration

Stitch quality is dominated by lens geometry, and none of it is recorded in the
file. The usual approach is to guess a field of view; that guess is what makes
naive stitches ghost. Orbit **solves** the geometry instead — both circle
centres, the radius, the true field of view, and the three-axis rotation of the
rear lens relative to the front — by maximising photometric agreement across the
seam.

This happens automatically the first time you import from a camera Orbit doesn't
have a profile for. It takes a few minutes, once, then gets cached to
`/data/config/calibrations/` and every later import is fast. Set
`ORBIT_AUTO_SOLVE=0` to turn that off; unknown cameras then fall back to an
assumed 200° field of view.

An Insta360 X5 profile ships built in:

| Parameter | Solved value |
|---|---|
| Field of view | **205.03°** (not the 190° usually assumed) |
| Circle radius | 3039.9 px |
| Rear lens rotation | yaw 0.87°, pitch 1.56°, roll −1.01° |
| Seam correlation | 0.90, up from 0.54 with a naive guess |

To redo or inspect a calibration by hand:

```bash
docker exec -it orbit-worker python -m app.calibrate show
docker exec -it orbit-worker python -m app.calibrate solve /data/originals/12/IMG.insp
docker exec -it orbit-worker python -m app.calibrate inspect /data/originals/12/IMG.insp
```

Solve from an outdoor daylight frame with detail near the seam — the optimiser is
matching image content across the overlap, so a flat sky gives it nothing to work
with. Profiles you solve override the shipped ones. Hit **Reprocess** on affected
items to rebuild them.

### Horizon levelling

`.insp` files carry a 1 kHz accelerometer and gyroscope stream in their trailer,
which gives the gravity vector at the moment of exposure. Orbit uses it to level
the horizon.

One caveat worth knowing: the IMU is not mounted square to the lenses (on the X5
it sits about 46° off), so the raw readings mean nothing in image space until
rotated. That rotation is in the shipped profile. Deriving it needs a frame you
know to be level, and a single level reference pins it only up to a rotation
about gravity itself — so on a heavily tilted shot the correction may be right in
magnitude but off in azimuth. Profiles solved by Orbit have no `imu_mount` and
simply skip levelling rather than guess.

### Instant previews

Insta360 cameras embed their own stitched 2560×1280 equirectangular in the
trailer. Orbit extracts it on ingest — about a second, no reprojection at all —
so a photo is browsable in the grid almost immediately, badged *camera preview*,
while the real stitch runs behind it. If the full stitch later fails, the item
stays viewable on that preview instead of disappearing.

## 5. Cloudflare

You already run a tunnel for Nextcloud. Add a public hostname to that same tunnel
rather than starting a second `cloudflared`:

```
Hostname : orbit.agrabah.app
Service  : http://<truenas-lan-ip>:8899
```

Three things will bite you otherwise:

**Upload size.** Cloudflare caps request bodies at 100 MB on Free and Pro plans.
A single 5.7K clip blows straight through that. Upload over the LAN, or via
Tailscale — the tunnel is for viewing and sharing, not ingest. (Chunked resumable
upload is the obvious fix and is not implemented here; see §7.)

**Cache rules.** Add a rule for `orbit.agrabah.app/api/items/*/media/*` and
`/api/s/*/items/*/media/*` set to **Bypass cache**. Range requests through
Cloudflare's cache make video seeking behave strangely, and you do not want private
media sitting in an edge cache keyed only by URL.

**Cloudflare's terms.** Section 2.8 of the self-serve terms restricts using the CDN
to serve a disproportionate share of non-HTML content — video especially. Family
sharing a few clips is not what they're policing, but if this becomes a
high-traffic public gallery, move video delivery off the tunnel. Your options are
Cloudflare Stream, an R2 bucket with signed URLs, or a plain reverse proxy on a
different hostname.

Orbit sets `Cache-Control: private` on all media and uses `SameSite=Lax`, HttpOnly
session cookies. Put Cloudflare Access in front of `/` if you want the library
itself behind SSO — but leave `/s/*` and `/api/s/*` open, or share links break.

## 6. GoPro MAX `.360`

Orbit projects these itself, in numpy and OpenCV. No patched ffmpeg, no OpenCL,
no GPU passthrough.

A `.360` is two HEVC tracks which, stacked, form a 4096×2688 equi-angular
cubemap laid out 3×2:

```
row 0 (front track)   LEFT   FRONT   RIGHT
row 1 (rear track)    DOWN   BACK    UP        (UP/DOWN rotated 270°, BACK 90°)
```

Plain `ffmpeg -vf v360=eac` gets this wrong: GoPro's variant is not the EAC that
filter implements, and it knows nothing of the layout below.

**Overlap bands.** Each row is not three faces butted together. The usable width
is 3968, and two 64-pixel bands are spliced in at source columns 688 and 3344.
Those bands are not padding — they hold the FRONT/BACK face extended past each
edge, duplicating what the neighbour covers, so the joins can be cross-faded.
Orbit blends them. (The commonly used `gopromax_opencl` filter discards them and
hard-cuts, and derives its splice constants from the EAC width rather than the
source width, which lands the cut at column 666 instead of 688 — a ~22 px
misalignment along every vertical join.)

**Detection is by container shape, not extension.** Cameras and phone apps
rename these to `.mp4` constantly, and nothing in the standard metadata says
"360", so Orbit looks for two equal-sized video tracks plus GoPro's telemetry
handler. Track indices are probed, never hardcoded — normally 0 and 5, but
TimeWarp clips have no audio and land at 0 and 4.

**Stabilisation** comes from the `GoPro MET` telemetry track (GPMF). Unlike
Insta360, GoPro reports gravity directly in the projection's own axes, so there
is no mount rotation to calibrate. `ORBIT_MAX360_STABILISE` picks the mode:

| Mode | Behaviour |
|---|---|
| `horizon` (default) | levels every frame, keeps intentional panning. Best handheld. |
| `full` | cancels all camera motion. Best on a pole or mount; drifts on long clips. |
| `none` | skips the rotation pass; roughly 3x faster. |

**Throughput.** This is CPU work and it is not fast: about 0.85 s per frame at
3840×1920 with stabilisation, so a 30-second clip takes around 13 minutes. All
renditions come out of a single projection pass, and the per-frame rotation is
applied as a cheap second resample rather than by rebuilding the projection each
frame — without those two the same clip would take about 45 minutes. Set
`ORBIT_MAX360_STABILISE=none` if you would rather have speed.

## 7. What this doesn't do yet

Being upfront about the gaps, roughly in the order they'll annoy you:

- **Resumable upload.** Single-shot multipart, so a dropped connection restarts the
  file and Cloudflare's 100 MB cap applies. Wiring in `tus` is the fix.
- **HLS.** Video is progressive MP4 with byte-range seeking. Fine on a LAN and for
  a handful of remote viewers; a proper HLS ladder is what you want if you're
  serving many people over WAN.
- **Hardware encoding.** Everything is libx264 on CPU. A 20-minute 8K clip will keep
  a worker busy for a long while. QSV or NVENC would help — add `/dev/dri` back to
  the compose file and swap the encoder in `pipeline.py`.
- **Insta360 video stabilisation.** GoPro video is stabilised from telemetry
  (§6), and Insta360 photos are levelled from their IMU (§4), but `.insv` video
  is not: FlowState-equivalent stabilisation isn't implemented, and ffmpeg's
  `v360` can't express a per-lens rotation, so `.insv` gets the solved field of
  view only.
- **GoPro horizontal joins.** The overlap bands only run vertically. The join
  between the two tracks has no duplicated data, so it stays a hard cut. Nothing
  equalises exposure between the two sensors either.
- **Single-user model.** One shared library, one admin account. Share links are the
  only multi-user surface.
- **No search, tags, or map view.** EXIF GPS is read but not surfaced.

## 8. Layout

```
app/config.py     paths, settings, calibration profile lookup
app/insp360.py    .insp container parsing, IMU decode, solved stitching
app/max360.py     .360 cubemap projection, overlap blending, stabilisation
app/gpmf.py       GoPro GPMF telemetry parser
app/db.py         SQLite schema and helpers
app/formats.py    probing: what is this file, which pipeline applies
app/pipeline.py   the ffmpeg recipes
app/worker.py     job queue drain loop
app/main.py       HTTP API, media delivery, share links
app/calibrate.py  calibration solve / show / inspect
app/calibrations/ shipped camera profiles
web/              browser UI (Photo Sphere Viewer, no build step)
```

The frontend has no bundler — it's an import map and ES modules, so you can edit
`web/app.js` and reload. Photo Sphere Viewer and three.js load from jsDelivr; if you
want it working with no internet, vendor those into `web/vendor/` and rewrite the
import map.

### Data model

`folders` are a plain parent-pointer tree. `items` hold one original plus probe
results. `derivatives` are regenerable outputs keyed by kind (`thumb`, `preview`,
`master`, `video_2160`, `video_1080`). Items pass through `pending` →
`processing` → `preview` (camera preview extracted, browsable) → `ready`. `shares` point at either a folder or an item;
folder shares grant access to the whole subtree, resolved at request time so moving
a folder in changes what a link exposes.

Jobs are claimed with a conditional `UPDATE ... WHERE state='queued'`, so you can
scale `orbit-worker` to several replicas without adding a broker. Watch your CPU
before you do — parallel 4K encodes will saturate most NAS hardware.


---

## 8. Running from a Git repository

Two machines, two different jobs: the workstation builds and tests, the NAS
only ever pulls a finished image. That keeps compilers, source trees and build
caches off the NAS entirely.

### Create the repository

```bash
cd orbit
git init -b main
git add .
git commit -m "Orbit: 360 photo and video library"

gh repo create orbit --private --source=. --push
# or, without the gh CLI:
#   git remote add origin git@github.com:YOURUSER/orbit.git
#   git push -u origin main
```

`.gitignore` already excludes `data/`, `.env` and the SQLite database, so no
media, passwords or solved calibrations get committed. Copy `.env.example` to
`.env` for local secrets.

Pushing to `main` triggers two workflows: `test.yml` runs the smoke test and
checks the image starts, and `publish.yml` pushes
`ghcr.io/YOURUSER/orbit:latest` to GitHub's registry.

### Make the image reachable

The GHCR package is private by default. Either:

- **Make it public** — Repository → Packages → orbit → Package settings →
  Change visibility. Simplest, and the image contains no secrets.
- **Or keep it private** and log the NAS in once, with a personal access token
  that has `read:packages`:
  ```bash
  echo "$TOKEN" | docker login ghcr.io -u YOURUSER --password-stdin
  ```

### Workstation (Linux Mint)

```bash
git clone git@github.com:YOURUSER/orbit.git
cd orbit

./scripts/dev.sh              # venv, no Docker, --reload. Fastest to iterate.
./scripts/dev.sh test         # smoke test only
./scripts/update.sh           # git pull + rebuild + restart containers
./scripts/update.sh --pull    # run the published image instead of building
```

`dev.sh` is the one to use while changing code — uvicorn reloads on save and
there is no image rebuild in the loop. `update.sh` is for verifying that what
CI produced actually works before you touch the NAS.

### TrueNAS

Nothing to clone. Paste `docker-compose.yaml` into **Apps → Discover → ⋮ →
Install via YAML** once, with `YOURUSER`, `POOL` and the password filled in.

To update afterwards:

1. **Apps → Installed → orbit → Edit**
2. If you pinned a version, bump the tag; if you are on `:latest`, leave it
3. **Save**, which re-pulls and recreates both containers

The `data` dataset is a bind mount and is never rebuilt, so your library,
database and solved calibrations all survive. Rolling back is the same
operation with an older tag.

### Releases

`:latest` moves whenever you push to `main`, which is convenient but means the
NAS can pick up a half-finished change. For anything you care about, tag it:

```bash
git tag v1.0.0 && git push --tags
```

That publishes `:v1.0.0`, `:1.0`, `:1` and `:latest`. Pinning the NAS to
`:1` gets patch updates on redeploy without ever jumping a major version.

### Suggested loop

1. Change code on Mint, run `./scripts/dev.sh test`
2. Import one real `.insp` and one real `.360`, check the output in a viewer
3. Commit and push; wait for the `test` workflow to go green
4. Tag a release
5. On TrueNAS, edit the app, set the new tag, save

### Keeping calibrations

Camera calibrations solved on one machine live in `data/config/calibrations/`
and are deliberately not in the repo — they are specific to your cameras, not
to the code. Copy them across rather than re-solving:

```bash
scp -r data/config/calibrations/* \
    truenas:/mnt/POOL/apps/orbit/data/config/calibrations/
```

Profiles that are genuinely general — a solved calibration for a camera model
anyone would have — are worth moving into `app/calibrations/` and committing,
which is how the shipped Insta360 X5 profile got there.
