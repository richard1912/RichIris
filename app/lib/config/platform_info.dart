import 'dart:io' as io show Platform;

import 'package:flutter/foundation.dart' show kIsWeb;

/// Platform predicates that are safe to call on **web**.
///
/// `dart:io` COMPILES for the web target — importing it and referencing
/// `Platform` raises no compile error — but every getter on it throws
/// `UnsupportedError` the moment it is read in a browser. That makes it a
/// runtime trap rather than a build failure: the web bundle builds cleanly,
/// then dies on the first `Platform.isAndroid` with a minified stack that
/// says nothing about `dart:io`. This app hit exactly that, in
/// `isClientOnlyInstall()`, which runs on every prefs read and so killed
/// startup before the first frame.
///
/// Each predicate short-circuits on [kIsWeb] before touching `Platform`.
/// `kIsWeb` is a compile-time constant, so on web dart2js folds the whole
/// expression to `false` and drops the `dart:io` reference entirely.
///
/// Always prefer these over `Platform.*` in code that can reach the web build.
bool get isWeb => kIsWeb;
bool get isAndroid => !kIsWeb && io.Platform.isAndroid;
bool get isWindows => !kIsWeb && io.Platform.isWindows;
