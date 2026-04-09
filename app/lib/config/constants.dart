enum StreamSource { s1, s2 }

extension StreamSourceExt on StreamSource {
  String get label => this == StreamSource.s1 ? 'Main' : 'Sub';
  String get description =>
      this == StreamSource.s1 ? 'Main' : 'Sub';
  String get param => name;
}

enum Quality { direct, high, low, ultraLow }

extension QualityExt on Quality {
  String get label {
    switch (this) {
      case Quality.direct:
        return 'Direct';
      case Quality.high:
        return 'High';
      case Quality.low:
        return 'Low';
      case Quality.ultraLow:
        return 'Ultra Low';
    }
  }

  String get description {
    switch (this) {
      case Quality.direct:
        return 'Native passthrough, no re-encode';
      case Quality.high:
        return 'HEVC, source-matched quality';
      case Quality.low:
        return 'HEVC, reduced quality';
      case Quality.ultraLow:
        return 'HEVC, minimal bandwidth';
    }
  }

  String get param {
    if (this == Quality.ultraLow) return 'ultralow';
    return name;
  }
}

const List<int> kSpeeds = [-4, -2, -1, 1, 2, 4, 16, 32];

const double kMinZoom = 1.0;
const double kMaxZoom = 24.0;
const double kSegmentMergeGapSeconds = 30.0;
const int kCameraRefreshMs = 5000;
const int kSegmentPollMs = 15000;
