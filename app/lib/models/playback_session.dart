class PlaybackSession {
  final String segmentUrl;
  final double seekSeconds;
  final String segmentStart;
  final String segmentEnd;
  final bool hasMore;
  /// 'forward' or 'backward' (server-rendered reverse stream).
  final String direction;

  PlaybackSession({
    required this.segmentUrl,
    required this.seekSeconds,
    required this.segmentStart,
    required this.segmentEnd,
    required this.hasMore,
    this.direction = 'forward',
  });

  factory PlaybackSession.fromJson(Map<String, dynamic> json) =>
      PlaybackSession(
        segmentUrl: json['segment_url'] as String,
        seekSeconds: (json['seek_seconds'] as num).toDouble(),
        segmentStart: (json['segment_start'] as String?) ?? '',
        segmentEnd: json['segment_end'] as String,
        hasMore: json['has_more'] as bool,
        direction: (json['direction'] as String?) ?? 'forward',
      );
}
