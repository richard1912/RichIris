import 'dart:io';

/// Returns true if this Flutter app is running from a RichIris *client-only*
/// Windows install.
///
/// The client installer (`installer/richiris_client.iss`) drops an empty
/// `client_only.txt` marker next to the app executable. The full installer
/// does not, so its absence means a full install (or dev/debug build).
///
/// Android always returns false — the Android APK has no flavor split
/// (there's only one APK per release) and always picks `assets.android`.
///
/// Result is memoized since callers hit this on every prefs read/write and
/// the answer can't change at runtime.
bool? _cached;

bool isClientOnlyInstall() {
  final cached = _cached;
  if (cached != null) return cached;
  if (!Platform.isWindows) return _cached = false;
  try {
    final exeDir = File(Platform.resolvedExecutable).parent;
    final marker =
        File('${exeDir.path}${Platform.pathSeparator}client_only.txt');
    return _cached = marker.existsSync();
  } catch (_) {
    return _cached = false;
  }
}
