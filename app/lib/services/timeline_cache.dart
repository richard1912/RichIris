import 'dart:async';
import 'package:flutter/foundation.dart';
import '../models/camera.dart';
import '../models/motion_event.dart';
import '../models/recording_segment.dart';
import '../models/thumbnail_info.dart';
import 'recording_api.dart';
import 'motion_api.dart';

// [TLCACHE] === TEMPORARY DEBUG LOGGING — remove after verification ===
// Grep logs for "[TLCACHE]" to trace prewarm behavior end-to-end.
void _log(String msg) => debugPrint('[TLCACHE] $msg');

class TimelineCacheEntry {
  List<RecordingSegment>? segments;
  List<MotionEvent>? motionEvents;
  List<ThumbnailInfo>? thumbnails;
  DateTime? segmentsAt;
  DateTime? motionAt;
  DateTime? thumbnailsAt;
}

class _Key {
  final int cameraId;
  final String date;
  const _Key(this.cameraId, this.date);

  @override
  bool operator ==(Object other) =>
      other is _Key && other.cameraId == cameraId && other.date == date;

  @override
  int get hashCode => Object.hash(cameraId, date);
}

class TimelineCache {
  final RecordingApi recordingApi;
  final MotionApi motionApi;
  final Map<_Key, TimelineCacheEntry> _store = {};

  /// Future that completes when the app-startup prewarm finishes. Null until
  /// prewarmAll is first invoked. Used by the app shell to schedule post-
  /// prewarm work (e.g. image precaching).
  Future<void>? prewarmFuture;

  /// Completes once `prewarmAll` has been called at least once. Lets the app
  /// shell start waiting for prewarm before `_fetchInitialData` has even
  /// returned (the shell widget mounts before that API call resolves).
  final Completer<void> _prewarmKickedOff = Completer<void>();
  Future<void> get prewarmKickedOff => _prewarmKickedOff.future;

  /// Tracks the "today" value (NVR-local) the cache last observed. Used to
  /// detect midnight rollover and sweep stale entries.
  String? _lastSeenToday;

  TimelineCache(this.recordingApi, this.motionApi);

  TimelineCacheEntry? get(int cameraId, String date) =>
      _store[_Key(cameraId, date)];

  TimelineCacheEntry _entry(int cameraId, String date) =>
      _store.putIfAbsent(_Key(cameraId, date), TimelineCacheEntry.new);

  /// Drop every entry whose date is strictly earlier than [date].
  void dropEntriesBefore(String date) {
    final before = _store.length;
    _store.removeWhere((k, _) => k.date.compareTo(date) < 0);
    final removed = before - _store.length;
    if (removed > 0) {
      _log('dropEntriesBefore $date removed=$removed remaining=${_store.length}');
    }
  }

  /// Drop every entry for the given camera (all dates).
  void dropCamera(int cameraId) {
    final before = _store.length;
    _store.removeWhere((k, _) => k.cameraId == cameraId);
    final removed = before - _store.length;
    if (removed > 0) {
      _log('dropCamera cam=$cameraId removed=$removed remaining=${_store.length}');
    }
  }

  /// Called whenever a consumer observes the current NVR-local "today".
  /// If today has advanced since the last observation, purge entries older
  /// than today (midnight rollover sweep).
  void observeToday(String today) {
    if (_lastSeenToday == today) return;
    if (_lastSeenToday != null) {
      _log('observeToday rollover $_lastSeenToday -> $today');
      dropEntriesBefore(today);
    }
    _lastSeenToday = today;
  }

  /// Build a list of thumbnail URLs to precache after prewarm — up to
  /// [perCamera] most-recent thumbs for every cached (camera, date) entry.
  /// Used by the app shell to warm Flutter's image cache so the first
  /// timeline hover returns instantly instead of paying a network round-trip.
  List<String> buildPrecacheThumbUrls({int perCamera = 5}) {
    final urls = <String>[];
    for (final entry in _store.values) {
      final thumbs = entry.thumbnails;
      if (thumbs == null || thumbs.isEmpty) continue;
      // Timestamps are "HH:MM:SS" strings, lexicographic sort == chronological.
      final sorted = [...thumbs]
        ..sort((a, b) => b.timestamp.compareTo(a.timestamp));
      final take = sorted.take(perCamera);
      for (final t in take) {
        urls.add(recordingApi.getThumbnailUrl(t.url));
      }
    }
    _log('buildPrecacheThumbUrls perCamera=$perCamera count=${urls.length}');
    return urls;
  }

  void putSegments(int cameraId, String date, List<RecordingSegment> segs) {
    final e = _entry(cameraId, date);
    e.segments = segs;
    e.segmentsAt = DateTime.now();
    _log('put segments cam=$cameraId date=$date count=${segs.length}');
  }

  void putMotionEvents(int cameraId, String date, List<MotionEvent> events) {
    final e = _entry(cameraId, date);
    e.motionEvents = events;
    e.motionAt = DateTime.now();
    _log('put motion cam=$cameraId date=$date count=${events.length}');
  }

  void putThumbnails(int cameraId, String date, List<ThumbnailInfo> thumbs) {
    final e = _entry(cameraId, date);
    e.thumbnails = thumbs;
    e.thumbnailsAt = DateTime.now();
    _log('put thumbs cam=$cameraId date=$date count=${thumbs.length}');
  }

  /// Prewarm today's timeline data (segments, motion events, thumbnails) for
  /// every enabled camera. Runs fire-and-forget from app startup.
  ///
  /// Concurrency capped at 3 cameras in flight at once to be nice to the
  /// backend (unindexed recordings queries + filesystem-glob thumbnails).
  /// Within each camera: segments + motion in parallel, then thumbnails.
  Future<void> prewarmAll(List<Camera> cameras, String date) async {
    // Observe "today" so a midnight rollover during a long-lived session
    // sweeps yesterday's entries the next time prewarm runs.
    observeToday(date);
    final future = _runPrewarm(cameras, date);
    prewarmFuture = future;
    if (!_prewarmKickedOff.isCompleted) _prewarmKickedOff.complete();
    return future;
  }

  Future<void> _runPrewarm(List<Camera> cameras, String date) async {
    final startAll = DateTime.now();
    _log('prewarmAll START cameras=${cameras.length} date=$date');
    const maxConcurrent = 3;
    var idx = 0;
    Future<void> worker(int workerId) async {
      while (true) {
        final i = idx++;
        if (i >= cameras.length) return;
        final cam = cameras[i];
        await _prewarmOne(cam, date, workerId);
      }
    }

    final workers =
        List.generate(maxConcurrent, (id) => worker(id), growable: false);
    await Future.wait(workers);
    final ms = DateTime.now().difference(startAll).inMilliseconds;
    _log('prewarmAll DONE total_ms=$ms');
  }

  Future<void> _prewarmOne(Camera cam, String date, int workerId) async {
    final camStart = DateTime.now();
    _log('prewarm[$workerId] cam=${cam.id} (${cam.name}) START');

    // Segments + motion in parallel — both small DB queries.
    final segsF = recordingApi.fetchSegments(cam.id, date).then((segs) {
      final t = DateTime.now().difference(camStart).inMilliseconds;
      _log(
          'prewarm[$workerId] cam=${cam.id} segments OK in ${t}ms count=${segs.length}');
      putSegments(cam.id, date, segs);
      return segs;
    }).catchError((e) {
      _log('prewarm[$workerId] cam=${cam.id} segments FAIL $e');
      return <RecordingSegment>[];
    });

    final motionF = motionApi.fetchEvents(cam.id, date).then((events) {
      final t = DateTime.now().difference(camStart).inMilliseconds;
      _log(
          'prewarm[$workerId] cam=${cam.id} motion OK in ${t}ms count=${events.length}');
      putMotionEvents(cam.id, date, events);
      return events;
    }).catchError((e) {
      _log('prewarm[$workerId] cam=${cam.id} motion FAIL $e');
      return <MotionEvent>[];
    });

    await Future.wait([segsF, motionF]);

    // Thumbnails last — slowest endpoint (filesystem glob).
    try {
      final thumbs = await recordingApi.fetchThumbnails(cam.id, date);
      final t = DateTime.now().difference(camStart).inMilliseconds;
      _log(
          'prewarm[$workerId] cam=${cam.id} thumbs  OK in ${t}ms count=${thumbs.length}');
      putThumbnails(cam.id, date, thumbs);
    } catch (e) {
      _log('prewarm[$workerId] cam=${cam.id} thumbs  FAIL $e');
    }

    final totalMs = DateTime.now().difference(camStart).inMilliseconds;
    _log('prewarm[$workerId] cam=${cam.id} DONE total_ms=$totalMs');
  }
}
