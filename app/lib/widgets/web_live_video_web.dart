import 'dart:async';
import 'dart:js_interop';
import 'dart:ui_web' as ui_web;

import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

/// One `<video>` element per instance, registered as a Flutter platform view.
///
/// The browser is handed the backend's fMP4 proxy
/// (`/api/streams/{id}/live.mp4`), which go2rtc serves without re-encoding.
/// A plain `<video>` plays it directly — HEVC included, on Chrome, Edge and
/// Safari — so there is no MSE buffer to manage, no WebRTC signalling and no
/// transcode.
class WebLiveVideo extends StatefulWidget {
  final String url;
  final BoxFit fit;

  /// Fires `true` the first time playback position actually advances, and
  /// `false` if the feed later stalls. The caller uses it to lift the poster
  /// frame, so it deliberately reports *frames moving*, not merely metadata
  /// having loaded — dimensions arrive with the stream's parameter sets, well
  /// before anything is on screen.
  final ValueChanged<bool>? onPlayingChanged;

  /// Lift the platform view above Flutter's canvas.
  ///
  /// Flutter web composites a platform view as a real DOM node sitting
  /// *between* canvases, and decides the split from paint order. For a view
  /// that fills the viewport - fullscreen - it gets that wrong and leaves the
  /// element beneath the canvas holding the Scaffold's opaque black
  /// background: the video decodes, is correctly sized and is never seen.
  /// A z-index on the host element puts it back on top.
  ///
  /// The grid must NOT set this. There the layering is already right, and the
  /// per-tile chrome Flutter paints over each feed (gear, feature badges,
  /// camera name, status overlay) would end up behind the video.
  final bool elevate;

  const WebLiveVideo({
    super.key,
    required this.url,
    this.fit = BoxFit.contain,
    this.onPlayingChanged,
    this.elevate = false,
  });

  @override
  State<WebLiveVideo> createState() => _WebLiveVideoState();
}

/// Platform-view types are registered process-wide and cannot be unregistered,
/// so each element needs a name no other instance will reuse.
int _viewSeq = 0;

class _WebLiveVideoState extends State<WebLiveVideo> {
  late final String _viewType;
  late final web.HTMLVideoElement _element;

  Timer? _watchdog;
  double _lastTime = -1;
  DateTime _lastAdvance = DateTime.now();
  bool _reportedPlaying = false;
  bool _disposing = false;

  /// A feed with no new frames for this long is treated as dead and reloaded.
  /// Generous on purpose: these cameras run as low as 7fps and a keyframe can
  /// be seconds away, so a tighter bound would reconnect healthy streams.
  static const _stallAfter = Duration(seconds: 12);

  @override
  void initState() {
    super.initState();
    _viewType = 'richiris-live-${_viewSeq++}';

    _element = web.HTMLVideoElement()
      ..autoplay = true
      ..muted = true
      // Without this iOS Safari takes the video fullscreen on play.
      ..playsInline = true;
    _element.style
      ..width = '100%'
      ..height = '100%'
      ..objectFit = switch (widget.fit) {
        BoxFit.cover => 'cover',
        BoxFit.fill => 'fill',
        _ => 'contain',
      };

    ui_web.platformViewRegistry
        .registerViewFactory(_viewType, (int _) => _element);

    // Flutter re-parents a platform view whenever the scene is rebuilt, and
    // moving a <video> in the DOM pauses it. Fullscreen rebuilds often enough
    // (clock, timeline playhead) that a 3s watchdog left the feed visibly
    // stop-starting, so resume the moment it happens instead. `_disposing`
    // keeps this from fighting the teardown in [dispose].
    _element.onPause.listen((_) {
      if (!_disposing && mounted) _play();
    });
    _element.src = widget.url;
    _play();
    _watchdog = Timer.periodic(const Duration(seconds: 3), (_) => _check());
  }

  @override
  void didUpdateWidget(WebLiveVideo old) {
    super.didUpdateWidget(old);
    if (old.url != widget.url) _reload();
  }

  /// Start playback, tolerating a refusal.
  ///
  /// The `autoplay` attribute alone is not enough here: the element is created
  /// detached and only enters the document when Flutter mounts the platform
  /// view, by which point the browser has already made its autoplay decision.
  /// The element is muted, so Chrome's policy does permit this — but a
  /// rejected promise is still an unhandled error if nothing catches it, and
  /// the watchdog re-calls this anyway.
  void _play() {
    _element.play().toDart.then((_) {}).catchError((Object _) => null);
  }

  void _check() {
    if (!mounted) return;
    // A tile that has data but is not playing simply needs another nudge --
    // the usual cause is the play() that ran before the platform view was
    // mounted.
    if (_element.paused && _element.readyState > 0) _play();
    // The <flt-platform-view> host only exists once Flutter has mounted the
    // view, which is after initState, so this is applied here rather than up
    // front. Cheap and idempotent.
    if (widget.elevate) {
      final host = _element.parentElement as web.HTMLElement?;
      if (host != null && host.style.zIndex != '1') {
        host.style
          ..position = 'relative'
          ..zIndex = '1';
      }
    }
    final t = _element.currentTime;
    if (t != _lastTime) {
      _lastTime = t;
      _lastAdvance = DateTime.now();
      if (!_reportedPlaying && t > 0) {
        _reportedPlaying = true;
        widget.onPlayingChanged?.call(true);
      }
      return;
    }
    if (DateTime.now().difference(_lastAdvance) > _stallAfter) {
      if (_reportedPlaying) {
        _reportedPlaying = false;
        widget.onPlayingChanged?.call(false);
      }
      _reload();
    }
  }

  /// Re-point the element at the same URL. The backend opens a fresh go2rtc
  /// consumer per request, so this recovers a dropped stream without touching
  /// the platform-view registration.
  void _reload() {
    _lastTime = -1;
    _lastAdvance = DateTime.now();
    _element
      ..pause()
      ..src = widget.url
      ..load();
    _play();
  }

  @override
  void dispose() {
    _disposing = true;
    _watchdog?.cancel();
    // Clearing src is what actually closes the HTTP request to the backend.
    // Without it the fMP4 response keeps streaming and go2rtc keeps a consumer
    // alive for a tile that is no longer on screen.
    _element
      ..pause()
      ..removeAttribute('src')
      ..load();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => HtmlElementView(viewType: _viewType);
}
