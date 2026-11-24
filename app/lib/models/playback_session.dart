class PlaybackSession {
  final String segmentUrl;
  final double seekSeconds;
  final String segmentStart;
  final String segmentEnd;
  final bool hasMore;
  /// 'forward' or 'backward' (server-rendered reverse stream).
  final String direction;
  /// 'riv' when served from RichIris's own recording format, else 'ts'.
  final String format;
  /// > 1: a keyframe-only fast-forward stream; position p = segmentStart + p*speed.
  final int speed;

  PlaybackSession({
    required this.segmentUrl,
    required this.seekSeconds,
    required this.segmentStart,
    required this.segmentEnd,
    required this.hasMore,
    this.direction = 'forward',
    this.format = 'ts',
    this.speed = 1,
  });

  factory PlaybackSession.fromJson(Map<String, dynamic> json) =>
      PlaybackSession(
        segmentUrl: json['segment_url'] as String,
        seekSeconds: (json['seek_seconds'] as num).toDouble(),
        segmentStart: (json['segment_start'] as String?) ?? '',
        segmentEnd: json['segment_end'] as String,
        hasMore: json['has_more'] as bool,
        direction: (json['direction'] as String?) ?? 'forward',
        format: (json['format'] as String?) ?? 'ts',
        speed: (json['speed'] as num?)?.toInt() ?? 1,
      );
}
