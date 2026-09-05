# RichIris inference — operator guide

How object detection runs, how to change the model, and how to undo any of it.

**Current state (2026-08-31):** inference is **fully local on the Debian box**. No GPU
offload, no Windows dependency. Model **`yolo11s-320.onnx`** on CPU, 1 thread, with
region-of-interest cropping on. Switched up from `yolo11n-320` after a night walk-past
test detected a person at only 0.45 confidence on Front North 42 against a 0.40 threshold
— too little margin on IR.

---

## Changing the detection model

This is the knob to reach for if accuracy is not good enough.

```bash
ssh offload
sqlite3 -cmd '.timeout 8000' /var/lib/richiris/database/richiris.db \
  "update settings set value='yolo11s-320.onnx' where key='ai.local_model';"
sudo systemctl restart richiris
```

Upsert works whether the row exists or not. **`settings.category` is NOT NULL**, so an
insert that omits it fails with `NOT NULL constraint failed: settings.category`:

```bash
sqlite3 -cmd '.timeout 8000' /var/lib/richiris/database/richiris.db \
  "insert into settings(key,value,category) values('ai.local_model','yolo11s-320.onnx','ai')
   on conflict(key) do update set value=excluded.value;"
```

> **Always pass `-cmd '.timeout 8000'`.** The running service holds the DB open, and a
> plain `sqlite3` update fails with `database is locked` — and `sqlite3` reports that on
> stderr while still exiting cleanly, so it is easy to think it worked when it did not.
> Read the value back before restarting.

Confirm which model actually loaded:

```bash
journalctl -u richiris --since '-2 min' | grep 'Detection model loaded'
# ... model=yolo11s-320.onnx input_size=320 format=yolo provider=CPUExecutionProvider threads=1
```

Set `ai.local_model` to `''` to fall back to the built-in priority order
(`_MODEL_FILENAMES` in `object_detector.py`). A configured-but-missing filename logs
`Configured local model not found — using fallback` and loads the next candidate rather
than leaving detection dead, so a typo degrades instead of breaking.

### Installed models and what they cost

Measured on the box (i5-8600), 35 real detection thumbnails across all cameras, conf 0.40,
person + vehicle classes, `rtdetr-l@640` as the reference (114 objects, 1116 ms/frame).

| Model | Inference | Recall, full frame | Recall, cropped | Extra boxes |
|---|---|---|---|---|
| `yolo11n-320.onnx` | 20 ms | 43.0% | 50.9% | **0** |
| **`yolo11s-320.onnx`** (current) | **58 ms** | **53.5%** | **59.6%** | 7 |
| `yolo11m-320.onnx` | 169 ms | 60.5% | 71.1% | 6 |
| `rtdetr-l.onnx` | 1116 ms | (reference) | — | — |

Not installed, measured and rejected: `yolo11n-640` (85 ms, 54.4%) and `yolo11s-640`
(245 ms, 51.8%) — **s@640 is both slower AND less accurate than s@320.** With region
cropping, input size past 320 stops paying, which is the same effect Frigate documents.

**Reading these numbers honestly:**

- "Recall" is *agreement with RT-DETR-L*, not ground truth. RT-DETR-L has its own false
  positives, so a "miss" is sometimes the small model being right.
- The cropped column assumes the motion pre-filter localises the object perfectly. It is
  an **upper bound**; reality sits between the two columns.
- **Per-frame recall is not per-event recall.** At 2 FPS an object persists across many
  frames and confirmation needs only 2 detections in 3, so events are caught far more
  reliably than ~51% suggests. Brief or distant events are the real exposure.
- `yolo11n` produces **zero** extra boxes: its errors are misses, not false alarms. Moving
  up the range buys recall but starts adding spurious detections (s: 7, m: 6).
- Confidences run lower than RT-DETR-L's (0.65 mean vs 0.95 on the same car). Cameras sit
  at 40–50% thresholds, which still passes, but do not raise thresholds without re-testing.

**Rule of thumb:** `yolo11s-320` is the sensible upgrade — +9 points of recall for 58 ms,
still faster than the 64 ms the 4080 used to take. `yolo11m-320` at 169 ms is slower than
the old GPU path; take it only if accuracy matters more than latency.

### Adding a new model

```bash
python -m venv /tmp/yb && /tmp/yb/bin/pip install ultralytics \
  --extra-index-url https://download.pytorch.org/whl/cpu
/tmp/yb/bin/python -c "from ultralytics import YOLO; YOLO('yolo11s.pt').export(format='onnx', imgsz=320, opset=17)"
cp /tmp/yb/yolo11s.onnx /opt/richiris/dependencies/models/yolo11s-320.onnx
chown richard:richard /opt/richiris/dependencies/models/yolo11s-320.onnx
rm -rf /tmp/yb      # the venv is ~1.8 GB — do not leave it
```

No code change needed. The loader reads **input size and output format from the ONNX file
itself**, so a 320 or 640 export, YOLO or RT-DETR, all just work:

- RT-DETR emits `[1, 300, 84]` — normalized coords, NMS-free → `_postprocess`
- YOLO emits `[1, 84, N]` — input-space pixel coords, needs NMS → `_postprocess_yolo`

Models are gitignored (`dependencies/`); push to a rebuilt box with
`./scripts/deploy_box.sh --with-models`.

---

## Other tuning knobs

| Setting | Default | Effect |
|---|---|---|
| `ai.local_model` | `''` | Which model file to load. See above. |
| `ai.region_crop_enabled` | `true` | Crop a square around the motion and detect on that instead of the whole frame. Free (same input tensor), worth ~8 points of recall. |
| `ai.remote_url` | `''` | Non-empty re-enables GPU offload. See rollback below. |
| `ai.face_enabled` | `false` | Face detection/recognition. Off since 2026-08-31. |

Code constants in `object_detector.py`:

- `LOCAL_CPU_THREADS = 1` — deliberately 1. On this CPU yolo11n@320 costs 21.6 ms on 1
  core, 14.0 ms on 2, 12.1 ms on 5. Spending 4 extra cores to save 9 ms is a bad trade on
  a box also decoding 8 camera streams. Raise only if latency becomes the bottleneck.
- `REGION_PADDING` 1.4, `REGION_MIN_SIZE` 224, `REGION_SKIP_RATIO` 0.9 — crop geometry.
  Motion wider than 90% of the frame skips cropping and uses the full frame (~2 in 3
  events in practice).
- `MIN_BOX_AREA_FRACTION` 0.002 — applied against the **crop** area, so proportionally
  more permissive on a crop. The per-camera confidence threshold is the real filter.

---

## Rolling back to the RTX 4080

The Windows `RichIrisInference` service was **uninstalled on 2026-08-31**. The code is
still at `C:\01-Self-Hosting\Debian\RichIris\inference_server\`, so rollback is two steps.

**1. Reinstall the Windows service** (elevated prompt on RICHARD-PC):

```
C:\01-Self-Hosting\Debian\RichIris\inference_server\install-service.bat
```

It stops and removes any existing copy first, so it is safe to re-run. It registers NSSM
with:

| | |
|---|---|
| Application | `C:\Users\Richard\AppData\Local\Programs\Python\Python313\python.exe` |
| Parameters | `server.py` |
| AppDirectory | `C:\01-Self-Hosting\Debian\RichIris\inference_server` |
| Logs | `inference_server\logs\service-{stdout,stderr}.log`, rotating at 10 MB |
| Start | `SERVICE_AUTO_START` |

Verify: `curl http://127.0.0.1:8701/health`

**2. Point the box back at it:**

```bash
sqlite3 -cmd '.timeout 8000' /var/lib/richiris/database/richiris.db \
  "update settings set value='http://192.168.8.11:8701' where key='ai.remote_url';"
sudo systemctl restart richiris
journalctl -u richiris --since '-1 min' | grep 'Remote inference active'
```

With a remote URL set, the local model is **not** loaded at startup (saves RAM); it
lazy-loads only if the remote fails. The circuit breaker opens after 3 timeouts and falls
back to whatever `ai.local_model` selects.

Pre-change DB backups on the box:
`richiris.db.bak-local-inference-20260831`, `richiris.db.bak-face-off-20260831`.

---

## Why it is local (the measurements behind the decision)

| | 4080 offload | Fully local |
|---|---|---|
| Inference (live, end-to-end) | 64.4 ms | **26.5 ms** |
| Box CPU (whole service tree) | 0.58 cores | 0.64 cores |
| Memory | 0.84 GB | 0.82 GB |
| Windows cost | 18.3% of a core + GPU | none |
| Box load | 2.4–3.8 | 1.18 |

The GPU only ever did ~11 ms of that 64 ms. The rest was JPEG-encoding each frame, the
HTTP hop, and decoding on the far side — **the round trip cost more than the inference it
bought**. Going local is 2.4x faster for +0.06 cores, and removes a dependency on a
Windows PC that crashed 12 times between 30 July and 31 August 2026 (one of those took the
NVR down for 6.5 hours; see `CLAUDE.md`).

### Why not the iGPU

Considered and rejected on measurement. Debian trixie has **dropped `intel-opencl-icd`**,
so OpenVINO 2026.3.1 on the box reports `devices: ['CPU']` only. Enabling the GPU means
third-party Intel compute-runtime `.deb`s on the box whose VAAPI stack Jellyfin, Immich and
RichIris transcode all depend on. Frigate quotes ~15–25 ms for MobileNetV2 on an HD
620-class iGPU; yolo11n@320 already does 21.6 ms on **one CPU core**, and the whole local
inference tier costs 0.06 cores. There is nothing meaningful left to move, and the UHD 630
is already busy transcoding.

The iGPU would only help if you wanted a **bigger** model locally — that is an accuracy
argument, not an efficiency one, and it would need benchmarking against transcode load
first.

---

## Re-running the accuracy eval

There is no committed harness (it was scratch), but it is ~80 lines and worth rebuilding if
you change models. The method:

1. Sample real frames from `/var/lib/richiris/thumbnails/*/<date>/detection_thumbs/*.jpg`.
2. Run `rtdetr-l.onnx` @640 as the reference at conf 0.40, classes `{0,1,2,3,5,7}`.
3. Run each candidate; match to the reference by **same label + IoU ≥ 0.5**.
4. Report matched / reference (recall), unmatched candidate boxes (extras), mean confidence.
5. For the cropped variant, crop around each reference box using the `REGION_*` constants
   before detecting, then offset results back — remembering that is an upper bound.

Import `_preprocess`, `_postprocess`, `_postprocess_yolo` from
`app.services.object_detector` so the eval uses the same code path as production.
