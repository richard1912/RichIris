# Feature Spec / Dev Prompt: Two-Tier Storage (Fast SSD + Archive HDD)

> Paste this to a fresh Claude Code session in the RichIris repo to implement the feature.
> Read the "Current architecture" section first and verify it against the code before changing anything.

## Goal

Implement BlueIris-style two-tier storage for RichIris:

- **HOT tier** — a fast SSD (e.g. `E:\`). Holds the **active/recent** writes that are latency-sensitive: in-progress + recent recording segments, thumbnails, detection thumbs, and the **SQLite database**.
- **ARCHIVE tier** — a large HDD (e.g. `G:\`). Holds bulk long-term footage. Completed segments are **flushed** here in the background. Retention/deletion runs here.

Motivation: today everything writes to a single `data_dir` (currently `G:\`). That drive is a failing HDD, and its I/O stalls delay the event→DB-insert→motion-script chain (observed "up to 10s" trigger latency, baseline ~2s). Moving the latency-critical writes (DB, thumbnails, active recordings) onto an SSD removes the stalls; flushing cold segments to the HDD keeps bulk storage cheap and protects SSD endurance. Mirrors BlueIris "New" → "Stored".

## Current architecture (verify before editing)

- Config: `backend/app/config.py` (`get_config`), `bootstrap.yaml` at repo root (`data_dir`, `port`). `data_dir` currently `G:/`.
- Data layout under `data_dir`: `database/`, `recordings/<camera>/<date>/`, `thumbnails/<camera>/<date>/{thumbs,detection_thumbs}/`, `playback/`, `exports/`, `logs/`.
- Recording: `backend/app/services/recorder.py` runs ffmpeg segmenting to `recordings/<camera>/<date>/rec_HH-MM-SS.ts`, then renames finished segments to `<camera> <date> HH.MM - HH.MM.ts` and registers them in the DB ("Registered segment", `camera_id`, `path`, `size`).
- DB: SQLite under `data_dir/database` via `backend/app/database.py`; models in `backend/app/models.py` (MotionEvent, and the recording-segment table — confirm its name/columns, esp. the stored file `path`).
- Thumbnails: `backend/app/services/thumbnail_capture.py` (periodic) + detection thumbs written from `motion_detector.py`.
- Playback / export: `backend/app/services/playback.py`, `clip_exporter.py`, routers `recordings.py`, `clips.py`.
- Retention/cleanup: `backend/app/services/retention.py`.
- **Existing storage scaffolding to reuse**: `backend/app/services/storage_migration.py`, `backend/app/routers/storage.py`. Read these first — there may already be drive/path abstractions or a migration mechanism to build on rather than reinvent.

## Requirements

### Config — runtime, user-configurable (NOT a hand-edited file)
This must be **production-ready and fully user-configurable from the app UI**, not a YAML the user edits by hand. `bootstrap.yaml` only seeds first-run defaults.

- **Source of truth = the runtime settings store** (the DB/settings mechanism behind `backend/app/routers/settings.py` + `backend/app/services/settings.py`), editable live via the API and the app's Settings screen — same pattern as every other RichIris setting.
- Settings model (persisted, API-exposed, UI-edited):
  ```
  storage.hot_dir              : path   (SSD: db, thumbnails, active recordings)
  storage.archive_dir          : path   (HDD: flushed long-term recordings)
  storage.hot_retention_minutes: int    (recent footage kept on SSD before flush is eligible)
  storage.hot_max_gb           : int    (cap; flush aggressively / backpressure above this)
  storage.two_tier_enabled     : bool   (off = single-tier legacy behavior)
  ```
- `bootstrap.yaml` keeps `data_dir` as the **first-run default**. On upgrade, seed `hot_dir = archive_dir = data_dir`, `two_tier_enabled = false` → identical to today until the user opts in. No breakage for existing installs.
- **Database + thumbnails always live on `hot_dir`** (latency-critical). Never put the SQLite DB on the archive HDD.

### User configuration & UX (production-ready)
A Storage section in the app's **Settings screen** (Flutter, alongside the existing settings UI; backend via `routers/settings.py`). Must be safe for a non-expert to operate.

- **Drive picker, not free-text paths.** Backend endpoint (extend `routers/storage.py`) enumerates available volumes and returns, per drive: letter, label, **media type (SSD/HDD)**, total/free space, and **SMART health** (`Healthy` / `Warning` / `Predictive Failure`). Surface this in the picker so the user sees what they're choosing. Free-text override allowed (network/UNC paths) but validated.
- **Health-aware warnings (production-critical).** If a chosen drive reports SMART `Warning`/`Predictive Failure`, show a prominent warning and require confirmation. (This is exactly the situation now: G: is in Predictive Failure — the UI should have flagged it.) Recommend SSD for hot tier; warn (non-blocking) if hot isn't an SSD or archive is smaller than hot.
- **Pre-apply validation** (backend, returns structured errors the UI renders inline): path exists or is creatable; writable (probe write+delete); enough free space; not the OS/system drive by accident; hot ≠ archive unless single-tier; for a hot-tier change, target has room for the DB + thumbnails + retention window.
- **Safe apply — different rules per field:**
  - `archive_dir`, retention, caps, enable/disable → **apply live**, no restart. New flushes target the new archive; old segments stay resolvable via the tier-aware resolver (or are re-pointed by a background re-flush).
  - `hot_dir` change (holds the open DB) → **cannot swap live.** Run a guided flow: validate → **migrate** (copy DB + thumbnails + recent recordings, verify) → **graceful service restart** to reopen the DB at the new location → verify → done. Show progress + a clear "do not power off" state; fully resumable if interrupted.
- **Live status panel:** show per-tier used/free, current flush backlog/lag, last flush time, and a degraded-mode banner when the archive drive is offline/slow (so the user understands why footage isn't moving). Pull SMART health here too as an early-warning for the next failing disk.
- **Idempotent, transactional settings apply;** all errors surfaced in the UI (never a silent failure or a crash). Sensible defaults so a fresh install works with zero storage config (single-tier on `data_dir`).
- Permissions: this is an admin-level setting; gate it the same way other destructive settings are gated.

### Recording path
- Recorder writes active segments to `hot_dir/recordings/...`. No change to ffmpeg cadence.
- DB stores recordings as **tier-agnostic logical paths** (or a `tier` column + relative path). Path resolution must find a segment whether it currently sits on hot or archive. Do **not** hard-code the drive into stored paths.

### Background flush (hot → archive)
- A background task moves **finalized** segments (not the in-progress one) from `hot_dir` to `archive_dir` once they're older than `hot_retention_minutes`, or sooner under `hot_max_gb` backpressure.
- Move must be: atomic (temp name + rename, or copy-then-verify-then-delete across volumes), crash-safe (resumable; never lose a segment on interrupt), and **non-blocking** to the recorder/detection pipeline.
- On successful move, update the segment's DB record to point at the archive tier (or flip a `tier` flag) in a single transaction.
- If `archive_dir` is unavailable/slow, **recording continues on hot**, flush retries with backoff, and the system logs a warning (degraded mode) — the live pipeline must never block on archive I/O. This is the key resilience property given the failing-drive history.

### Reads (playback / export / clips)
- `playback.py`, `clip_exporter.py`, and the `recordings`/`clips` routers must resolve a segment's current physical location via the tier-aware path resolver (check hot first, then archive). A clip/export spanning the flush boundary may have some segments on each tier — handle transparently.

### Retention
- Retention (`retention.py`) deletes from the **archive tier** by age/size as today. Add a separate hot-tier policy: flush (not delete) is the primary mechanism; only delete from hot after a verified flush.

### Migration
- Provide a one-shot migration (extend `storage_migration.py`): move the existing `database/` and `thumbnails/` to `hot_dir`, leave/relocate existing recordings appropriately, rewrite/relativize stored paths, and validate. Must be safe to run against the **currently failing G: drive** — read-tolerant, copy-then-verify, resumable, and it should surface unreadable/corrupt files (e.g. the corrupted `thumbnails` dir) rather than aborting the whole migration.

## Edge cases to handle
- In-progress (open) segment must never be moved/locked.
- File-in-use / sharing violations during move → retry.
- Crash mid-move → no orphaned/duplicate/lost segments (idempotent recovery on startup).
- `hot_dir` full → flush faster / oldest-first; never wedge the recorder.
- `archive_dir` offline → degraded mode (hot-only), auto-resume flush when it returns.
- Cross-volume move is a copy+delete (not rename) — verify size/hash before deleting source.
- Corrupt source files (failing HDD) during migration → log + skip + report, don't abort.

## Non-goals
- Not RAID/replication. Single copy per segment (hot OR archive, not both long-term).
- No cloud tier.
- Don't change the detection/AI pipeline here (separate work).

## Acceptance criteria
- **All storage config is set from the app Settings UI** (drive picker with media-type, free space, and SMART health); no hand-editing of files required. Changes persist in the runtime settings store and survive restart.
- Choosing a drive in `Warning`/`Predictive Failure` triggers a visible warning + confirmation.
- Invalid/un-writable/too-small targets are rejected pre-apply with a clear inline error (no crash, no partial apply).
- `archive_dir`/retention/cap changes apply **live** without restart; a `hot_dir` change runs the guided migrate→restart flow with resumable progress and no data loss.
- DB + thumbnails + active recordings verified on `hot_dir` (SSD); flushed segments on `archive_dir` (HDD).
- Pulling the archive drive offline does **not** stall recording or motion-script firing; flush resumes when it returns; UI shows degraded-mode banner.
- Playback and clip export work seamlessly across segments that span both tiers.
- Single-`data_dir` legacy / fresh-install (zero config) still works unchanged.
- Measured: event→script latency no longer spikes when the archive disk is slow/stalling (the original symptom).
- Migration runs against a degraded/failing source drive without data loss and reports unreadable files.

## Suggested order of work
1. Read `config.py`, `services/settings.py` + `routers/settings.py` (runtime settings mechanism), `routers/storage.py`, `storage_migration.py`, `recorder.py`, `retention.py`, `playback.py`, the recording model in `models.py`, and the app's Settings screen (`app/lib/screens/...`). Confirm the existing settings + path/tier abstractions.
2. Add the persisted `storage.*` settings (settings store + schema in `schemas.py`) with the back-compat seed (`two_tier_enabled=false`, hot=archive=`data_dir`). Centralize all path building behind a hot/archive-aware `StoragePaths` resolver.
3. Backend storage endpoints: enumerate drives (media type, space, SMART health), validate a proposed config, and apply (live vs migrate-required). 
4. Point recorder, thumbnails, and DB at `hot_dir` via the resolver.
5. Implement the background flusher + tier-aware read resolver (playback/export).
6. Wire retention to archive tier + hot flush/backpressure policy.
7. Migration command + the guided `hot_dir`-change flow (migrate → graceful restart → verify, resumable).
8. **Flutter Settings UI:** Storage section — drive picker with type/space/health, warnings, validation errors inline, live status panel (per-tier usage, flush backlog, degraded banner).
9. Tests: each edge case above; degraded-archive integration test; settings validation/apply (live + migrate paths); UI flows.

## Related context
- Latency investigation that motivated this: baseline ~2s (2 fps stream + ~1.9s buffering + SCRFD face pass on person events); the *spikes* trace to G: I/O stalls. Two-tier removes the spikes. Separate planned work: skip AI confirmation/face for `motion_only` "Any motion" light scripts to cut the baseline.
- G: (disk #1) is in SMART **Predictive Failure** with a corrupted `thumbnails` dir — replace it regardless; two-tier makes the system tolerant of slow archive storage so a future bad disk can't stall the live path.
