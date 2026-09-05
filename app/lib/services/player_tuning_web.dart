import 'package:media_kit/media_kit.dart';

/// No-op on web: media_kit's web backend is an HTML `<video>` element, which
/// has no libmpv underneath and no equivalent knobs. Buffering and hardware
/// decoding are the browser's business there.
void applyLiveTuning(Player player) {}

/// No-op on web — the browser decides its own decode path.
void applyHwdec(Player player) {}

/// Always `''` on web. The stats bar treats an empty string as "unknown" and
/// falls back to what it can read from the player's own streams, so this
/// degrades to a thinner stats panel rather than a broken one.
Future<String> playerProperty(Player player, String name) async => '';
