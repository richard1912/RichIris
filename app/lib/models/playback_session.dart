class PlaybackSession {
  final String playbackUrl;
  final String windowEnd;
  final bool hasMore;

  PlaybackSession({
    required this.playbackUrl,
    required this.windowEnd,
    required this.hasMore,
  });

  factory PlaybackSession.fromJson(Map<String, dynamic> json) =>
      PlaybackSession(
        playbackUrl: json['playback_url'] as String,
        windowEnd: json['window_end'] as String,
        hasMore: json['has_more'] as bool,
      );
}
