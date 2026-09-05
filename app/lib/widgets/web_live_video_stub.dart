import 'package:flutter/material.dart';

/// Native placeholder. Never constructed: `LivePlayer` only reaches for
/// [WebLiveVideo] when `isWeb`, and on Windows/Android it plays RTSP through
/// media_kit instead. It exists so the conditional export has something to
/// resolve to when `dart:js_interop` is absent.
class WebLiveVideo extends StatelessWidget {
  final String url;
  final BoxFit fit;
  final ValueChanged<bool>? onPlayingChanged;
  final bool elevate;

  const WebLiveVideo({
    super.key,
    required this.url,
    this.fit = BoxFit.contain,
    this.onPlayingChanged,
    this.elevate = false,
  });

  @override
  Widget build(BuildContext context) => const SizedBox.shrink();
}
