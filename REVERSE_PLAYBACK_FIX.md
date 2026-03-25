# Reverse Playback — Bug Analysis & Fix

## The Bug

When playing in reverse, reaching the start of the current video causes it to loop back to the end of the **same** video instead of loading the previous (earlier) segment.

## Root Cause

The playback API works with **forward windows**:

```
POST /api/recordings/{id}/playback?start=15:15:00
→ Backend creates window: [15:15:00, 15:45:00]  (start + 30min)
→ Remuxes all segments in that range into one MP4
→ Returns: { playback_url, window_end: "15:45:00" }
```

When reverse-scrubbing through this video, `video.currentTime` counts down from `duration` to 0. Meanwhile `virtualTimeRef` is decremented each tick to track the NVR clock going backwards.

**When `video.currentTime` hits 0** (start of the MP4), the code requests a new session at `virtualTimeRef`:

```
virtualTimeRef ≈ 15:14:50  (just past the start of the current window)
Request: start=15:14:50
Backend window: [15:14:50, 15:44:50]  ← OVERLAPS THE SAME SEGMENTS
```

The backend returns essentially the same content. The frontend seeks to the end. The cycle repeats — **infinite loop on the same video**.

## The Fix

When loading the next session for **reverse** playback, the request must cover the time **before** the current position, not after it. Subtract the playback window duration:

```
virtualTimeRef ≈ 15:14:50
PLAYBACK_WINDOW = 30 minutes

Reverse request: start = 15:14:50 - 30min = 14:44:50
Backend window: [14:44:50, 15:14:50]  ← PREVIOUS 30 minutes
```

This loads the **earlier** segments. Seek to `video.duration - 1` (≈ 15:14:50), continue scrubbing backwards.

### Code Change

In `startReverseInterval` inside `CameraFullscreen.tsx`, when `video.currentTime <= 0.5`:

```diff
- const newStart = formatLocalISO(virtualTimeRef.current)
+ const WINDOW_MS = 30 * 60 * 1000
+ const newStart = formatLocalISO(virtualTimeRef.current - WINDOW_MS)
```

And update `playbackStartTimeRef` to the new start so `virtualTimeRef` syncing stays correct.

## Data Flow After Fix

```
1. User clicks -2x while viewing segment [15:15 - 15:45]
2. Video pauses, interval scrubs currentTime backwards
3. currentTime hits 0 → virtualTimeRef ≈ 15:14:50
4. Request: start = 14:44:50 → window [14:44:50, 15:14:50]
5. Backend remuxes segments covering that range
6. Frontend: video.src = new MP4, seek to video.duration - 1
7. Continue scrubbing backwards through this new 30min chunk
8. currentTime hits 0 again → virtualTimeRef ≈ 14:44:40
9. Request: start = 14:14:40 → window [14:14:40, 14:44:40]
10. Repeat...
```

## Context

- **Segments on disk**: 15-minute `.ts` files, e.g. `Camera 1 2026-03-11 15.00 - 15.15.ts`
- **Playback window**: 30 minutes (backend `PLAYBACK_WINDOW = 1800`)
- **In-progress segments**: `rec_HH-MM-SS.ts`, registered in DB with `in_progress=True`, `end_time` updated to file mtime every 15s by scanner
- **Times**: All local, no timezone. Frontend must use `formatLocalISO()`, never `.toISOString()`
