class ThumbnailInfo {
  final String timestamp;
  final String url;
  final int thumbWidth;
  final int thumbHeight;
  final int interval;

  ThumbnailInfo({
    required this.timestamp,
    required this.url,
    required this.thumbWidth,
    required this.thumbHeight,
    required this.interval,
  });

  factory ThumbnailInfo.fromJson(Map<String, dynamic> json) => ThumbnailInfo(
        timestamp: json['timestamp'] as String,
        url: json['url'] as String,
        thumbWidth: json['thumb_width'] as int,
        thumbHeight: json['thumb_height'] as int,
        interval: json['interval'] as int,
      );
}
