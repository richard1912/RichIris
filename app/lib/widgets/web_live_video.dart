/// Live video surface for the **web** build.
///
/// media_kit is bypassed entirely in the browser. Its web backend does drive
/// an `<video>` element correctly — the streams decode, report their true
/// dimensions and advance — but the element is never attached to the document,
/// because `media_kit_video`'s web `Video` widget only mounts its
/// `HtmlElementView` while `id`, `rect` and an internally-tracked `_visible`
/// flag all line up, and `stop()` (which `open()` calls first, every time)
/// pushes a null width that clears `_visible`. The result is eight video
/// elements decoding 4K HEVC into nothing.
///
/// Registering the element as a platform view directly costs about as much
/// code as working around that, and it puts reconnect behaviour and the
/// first-frame signal under this app's control instead of a third party's.
export 'web_live_video_stub.dart'
    if (dart.library.js_interop) 'web_live_video_web.dart';
