import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';

import '../models/camera.dart';
import '../models/guard_event.dart';
import '../services/events_api.dart';
import '../utils/time_utils.dart';

const _kPageSize = 50;
const _kAutoRefresh = Duration(seconds: 30);
const _kMaxContentWidth = 920.0;

const _muted = Color(0xFF737373); // neutral-500
const _mutedLight = Color(0xFFA3A3A3); // neutral-400
const _border = Color(0xFF404040); // neutral-700
const _alertFired = Color(0xFFEF4444); // red-500
const _aiAlert = Color(0xFF3B82F6); // blue-500

/// Suspicion 0..3 -> chip colour.
Color suspicionColor(int level) {
  switch (level) {
    case 1:
      return const Color(0xFF14B8A6); // teal-500
    case 2:
      return const Color(0xFFF59E0B); // amber-500
    case 3:
      return const Color(0xFFEF4444); // red-500
    default:
      return const Color(0xFF6B7280); // gray-500
  }
}

String _suspicionText(GuardAssessment a) {
  final label = a.suspicionLabel;
  if (label != null && label.isNotEmpty) return label;
  return const ['none', 'low', 'medium', 'high'][a.suspicion];
}

const _months = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/// "20:10:43" for today (server clock), "18 Nov 20:10:43" otherwise. Works on
/// the string: event times are server-local with no timezone, so they must
/// never be run through a UTC conversion.
String formatEventTime(String iso, String today) {
  if (iso.length < 19) return iso;
  final time = iso.substring(11, 19);
  final date = iso.substring(0, 10);
  if (date == today) return time;
  final month = int.tryParse(iso.substring(5, 7)) ?? 0;
  final day = int.tryParse(iso.substring(8, 10)) ?? 0;
  if (month < 1 || month > 12) return '$date $time';
  final year = date.substring(0, 4) == today.substring(0, 4)
      ? ''
      : ' ${date.substring(0, 4)}';
  return '$day ${_months[month - 1]}$year $time';
}

String _capitalise(String s) =>
    s.isEmpty ? s : '${s[0].toUpperCase()}${s.substring(1)}';

/// The guard's log: a newest-first feed of detections with the AI's
/// description and suspicion level for each.
class EventsScreen extends StatefulWidget {
  final EventsApi eventsApi;
  final List<Camera> cameras;
  final int tzOffsetMs;

  /// False while something else (fullscreen playback) covers the pane, so the
  /// auto-refresh stops polling for a feed nobody is looking at.
  final bool active;
  final VoidCallback? onBack;

  /// Open the event's camera in fullscreen playback at the event.
  final ValueChanged<GuardEvent>? onPlayRecording;

  const EventsScreen({
    super.key,
    required this.eventsApi,
    required this.cameras,
    required this.tzOffsetMs,
    this.active = true,
    this.onBack,
    this.onPlayRecording,
  });

  @override
  State<EventsScreen> createState() => _EventsScreenState();
}

class _EventsScreenState extends State<EventsScreen> {
  final ScrollController _scroll = ScrollController();
  List<GuardEvent> _events = [];
  bool _loading = true;
  bool _loadingMore = false;
  bool _hasMore = false;
  String? _error;
  int? _minSuspicion; // null = All
  int? _cameraId; // null = all cameras
  int _gen = 0; // bumped on a filter change; stale responses are dropped
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_onScroll);
    _reload();
    _timer = Timer.periodic(_kAutoRefresh, (_) {
      if (widget.active) _refresh();
    });
  }

  @override
  void didUpdateWidget(covariant EventsScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Back from playback: catch up straight away rather than on the next tick.
    if (widget.active && !oldWidget.active) _refresh();
  }

  @override
  void dispose() {
    _timer?.cancel();
    _scroll.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (!_scroll.hasClients) return;
    if (_scroll.position.extentAfter < 600) _loadMore();
  }

  Future<List<GuardEvent>> _fetch({String? before}) => widget.eventsApi
      .fetchEvents(
        limit: _kPageSize,
        before: before,
        cameraId: _cameraId,
        minSuspicion: _minSuspicion,
      );

  String _describeError(Object e) {
    if (e is DioException) {
      final code = e.response?.statusCode;
      if (code != null) return 'The server answered $code.';
      return "Can't reach the server.";
    }
    return '$e';
  }

  /// Start over (first load, or a filter changed).
  Future<void> _reload() async {
    final gen = ++_gen;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final events = await _fetch();
      if (!mounted || gen != _gen) return;
      setState(() {
        _events = events;
        _hasMore = events.length >= _kPageSize;
        _loading = false;
      });
      if (_scroll.hasClients) _scroll.jumpTo(0);
    } catch (e) {
      if (!mounted || gen != _gen) return;
      setState(() {
        _events = [];
        _hasMore = false;
        _loading = false;
        _error = _describeError(e);
      });
    }
  }

  /// Fetch the newest page and merge it over what is loaded, so new events
  /// appear on top and late assessments fill in, without losing the pages
  /// already scrolled through.
  Future<void> _refresh() async {
    if (_loading) return;
    final gen = _gen;
    try {
      final latest = await _fetch();
      if (!mounted || gen != _gen) return;
      final ids = latest.map((e) => e.id).toSet();
      final firstPageOnly = _events.length <= _kPageSize;
      setState(() {
        _error = null;
        if (firstPageOnly) {
          _events = latest;
          _hasMore = latest.length >= _kPageSize;
        } else {
          _events = [...latest, ..._events.where((e) => !ids.contains(e.id))];
        }
      });
    } catch (e) {
      if (!mounted || gen != _gen) return;
      // Keep showing what we have; only an empty feed needs the error.
      if (_events.isEmpty) setState(() => _error = _describeError(e));
    }
  }

  Future<void> _loadMore() async {
    if (_loadingMore || _loading || !_hasMore || _events.isEmpty) return;
    final gen = _gen;
    setState(() => _loadingMore = true);
    try {
      final older = await _fetch(before: _events.last.startTime);
      if (!mounted || gen != _gen) return;
      final ids = _events.map((e) => e.id).toSet();
      setState(() {
        _events = [..._events, ...older.where((e) => !ids.contains(e.id))];
        _hasMore = older.length >= _kPageSize;
        _loadingMore = false;
      });
    } catch (_) {
      if (!mounted || gen != _gen) return;
      // Leave _hasMore set: scrolling again retries.
      setState(() => _loadingMore = false);
    }
  }

  void _setMinSuspicion(int? value) {
    if (_minSuspicion == value) return;
    _minSuspicion = value;
    _reload();
  }

  void _setCamera(int? id) {
    if (_cameraId == id) return;
    _cameraId = id;
    _reload();
  }

  void _openDetail(GuardEvent event) {
    final canPlay = widget.onPlayRecording != null &&
        widget.cameras.any((c) => c.id == event.cameraId);
    Navigator.of(context).push(MaterialPageRoute(
      builder: (ctx) => EventDetailScreen(
        event: event,
        eventsApi: widget.eventsApi,
        today: todayDate(tzOffsetMs: widget.tzOffsetMs),
        onPlayRecording: canPlay
            ? () {
                Navigator.of(ctx).pop();
                widget.onPlayRecording!(event);
              }
            : null,
      ),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: widget.onBack != null
            ? IconButton(
                icon: const Icon(Icons.arrow_back),
                onPressed: widget.onBack,
              )
            : null,
        title: const Text('Events'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh, size: 20),
            tooltip: 'Refresh',
            onPressed: _loading ? null : _refresh,
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: Column(
        children: [
          _centered(_buildFilters()),
          const Divider(height: 1, color: _border),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _refresh,
              child: _buildFeed(),
            ),
          ),
        ],
      ),
    );
  }

  Widget _centered(Widget child) => Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: _kMaxContentWidth),
          child: child,
        ),
      );

  Widget _buildFilters() {
    const levels = <(String, int?)>[
      ('All', null),
      ('Low+', 1),
      ('Medium+', 2),
      ('High', 3),
    ];
    // The filtered camera may have been deleted since it was picked.
    final cameraValue =
        widget.cameras.any((c) => c.id == _cameraId) ? _cameraId : null;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          for (final (label, value) in levels)
            ChoiceChip(
              label: Text(label),
              selected: _minSuspicion == value,
              showCheckmark: false,
              visualDensity: VisualDensity.compact,
              selectedColor: value == null
                  ? const Color(0xFF3B82F6)
                  : suspicionColor(value).withValues(alpha: 0.85),
              side: const BorderSide(color: _border),
              onSelected: (_) => _setMinSuspicion(value),
            ),
          Container(
            height: 34,
            padding: const EdgeInsets.symmetric(horizontal: 10),
            decoration: BoxDecoration(
              color: const Color(0xFF262626),
              border: Border.all(color: _border),
              borderRadius: BorderRadius.circular(6),
            ),
            child: DropdownButtonHideUnderline(
              child: DropdownButton<int?>(
                value: cameraValue,
                isDense: true,
                style: const TextStyle(fontSize: 13, color: Colors.white),
                dropdownColor: const Color(0xFF262626),
                items: [
                  const DropdownMenuItem<int?>(
                    value: null,
                    child: Text('All cameras'),
                  ),
                  for (final c in widget.cameras)
                    DropdownMenuItem<int?>(value: c.id, child: Text(c.name)),
                ],
                onChanged: _setCamera,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildFeed() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_events.isEmpty) return _buildEmpty();
    final today = todayDate(tzOffsetMs: widget.tzOffsetMs);
    return ListView.builder(
      controller: _scroll,
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: _events.length + 1,
      itemBuilder: (context, i) {
        if (i == _events.length) return _buildFooter();
        final event = _events[i];
        return _centered(Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
          child: _EventCard(
            key: ValueKey(event.id),
            event: event,
            eventsApi: widget.eventsApi,
            today: today,
            onTap: () => _openDetail(event),
          ),
        ));
      },
    );
  }

  Widget _buildFooter() {
    if (_loadingMore) {
      return const Padding(
        padding: EdgeInsets.all(16),
        child: Center(
          child: SizedBox(
            width: 22,
            height: 22,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
        ),
      );
    }
    if (_hasMore) {
      return Center(
        child: TextButton(onPressed: _loadMore, child: const Text('Load more')),
      );
    }
    return const Padding(
      padding: EdgeInsets.all(16),
      child: Center(
        child: Text('End of events', style: TextStyle(color: _muted, fontSize: 12)),
      ),
    );
  }

  /// Scrollable so pull-to-refresh still works on an empty feed.
  Widget _buildEmpty() {
    final filtered = _minSuspicion != null || _cameraId != null;
    return LayoutBuilder(
      builder: (context, constraints) => SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        child: SizedBox(
          height: constraints.maxHeight,
          child: Center(
            child: Padding(
              padding: const EdgeInsets.all(32),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    _error != null ? Icons.error_outline : Icons.event_note,
                    size: 48,
                    color: Colors.grey[600],
                  ),
                  const SizedBox(height: 12),
                  Text(
                    _error != null
                        ? "Couldn't load events"
                        : filtered
                            ? 'No events match these filters'
                            : 'No events yet',
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500),
                    textAlign: TextAlign.center,
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 6),
                    Text(
                      _error!,
                      style: const TextStyle(color: _muted, fontSize: 13),
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 16),
                    OutlinedButton.icon(
                      onPressed: _reload,
                      icon: const Icon(Icons.refresh, size: 18),
                      label: const Text('Retry'),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  final String text;
  final Color color;
  final IconData? icon;
  final bool filled;

  const _Chip({required this.text, required this.color, this.icon, this.filled = false});

  @override
  Widget build(BuildContext context) {
    final fg = filled ? Colors.white : color;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: filled ? 0.9 : 0.14),
        border: Border.all(color: color.withValues(alpha: filled ? 0.9 : 0.5)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 12, color: fg),
            const SizedBox(width: 4),
          ],
          Text(
            text,
            style: TextStyle(fontSize: 11, fontWeight: FontWeight.w600, color: fg),
          ),
        ],
      ),
    );
  }
}

/// The suspicion chip plus the two alert markers (what really fired vs what
/// the AI would have done), shared by the feed card and the detail page.
List<Widget> _statusChips(GuardEvent event) {
  final a = event.assessment;
  return [
    if (a == null)
      const _Chip(text: 'not assessed', color: _muted)
    else if (!a.isOk)
      const _Chip(text: 'assessment failed', color: _muted, icon: Icons.error_outline)
    else
      _Chip(
        text: _suspicionText(a).toUpperCase(),
        color: suspicionColor(a.suspicion),
        filled: a.suspicion >= 2,
      ),
    if (event.scriptsFired.isNotEmpty)
      const _Chip(
        text: 'alert fired',
        color: _alertFired,
        icon: Icons.notifications_active,
      ),
    if (a != null && a.isOk && a.wouldAlert)
      const _Chip(text: 'AI would alert', color: _aiAlert, icon: Icons.smart_toy_outlined),
  ];
}

class _EventImage extends StatelessWidget {
  final String? url;
  final BoxFit fit;

  const _EventImage({required this.url, this.fit = BoxFit.cover});

  @override
  Widget build(BuildContext context) {
    const placeholder = ColoredBox(
      color: Color(0xFF171717),
      child: Center(
        child: Icon(Icons.image_not_supported_outlined, color: _border, size: 22),
      ),
    );
    if (url == null) return placeholder;
    return Image.network(
      url!,
      fit: fit,
      gaplessPlayback: true,
      errorBuilder: (_, _, _) => placeholder,
    );
  }
}

class _EventCard extends StatelessWidget {
  final GuardEvent event;
  final EventsApi eventsApi;
  final String today;
  final VoidCallback onTap;

  const _EventCard({
    super.key,
    required this.event,
    required this.eventsApi,
    required this.today,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final a = event.assessment;
    final assessed = a != null && a.isOk;
    final preview = event.previewUrl;
    final level = assessed ? a.suspicion : 0;

    return LayoutBuilder(builder: (context, constraints) {
      final wide = constraints.maxWidth >= 600;
      // Unassessed events are a line or two, so their picture is smaller.
      final thumbW = assessed ? (wide ? 208.0 : 112.0) : (wide ? 112.0 : 80.0);
      return Material(
        color: const Color(0xFF262626),
        borderRadius: BorderRadius.circular(8),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Container(
            decoration: BoxDecoration(
              border: Border(
                left: BorderSide(
                  color: level >= 2 ? suspicionColor(level) : Colors.transparent,
                  width: 3,
                ),
              ),
            ),
            padding: const EdgeInsets.fromLTRB(7, 10, 10, 10),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ClipRRect(
                  borderRadius: BorderRadius.circular(6),
                  child: SizedBox(
                    width: thumbW,
                    height: thumbW * 9 / 16,
                    child: _EventImage(
                      url: preview != null ? eventsApi.absoluteUrl(preview) : null,
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(child: _buildText(a, assessed)),
              ],
            ),
          ),
        ),
      );
    });
  }

  Widget _buildText(GuardAssessment? a, bool assessed) {
    final secondary = <String>[
      if (assessed && a!.subject != null && a.subject != 'none')
        _capitalise(a.subject!),
      if (assessed && (a!.activity ?? '').isNotEmpty) a.activity!,
    ].join(' · ');
    final reason = assessed ? (a!.reason ?? '') : '';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                event.cameraName,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
              ),
            ),
            const SizedBox(width: 8),
            Text(
              formatEventTime(event.startTime, today),
              style: const TextStyle(
                fontSize: 12,
                color: _mutedLight,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
        const SizedBox(height: 6),
        Wrap(spacing: 6, runSpacing: 4, children: _statusChips(event)),
        if (assessed && (a!.description ?? '').isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            a.description!,
            style: const TextStyle(fontSize: 14, height: 1.35),
          ),
        ],
        if (secondary.isNotEmpty) ...[
          const SizedBox(height: 6),
          Text(secondary, style: const TextStyle(fontSize: 12, color: _mutedLight)),
        ],
        if (reason.isNotEmpty) ...[
          const SizedBox(height: 2),
          Text(reason, style: const TextStyle(fontSize: 12, color: _muted)),
        ],
        if (a != null && !a.isOk && (a.error ?? '').isNotEmpty) ...[
          const SizedBox(height: 6),
          Text(
            a.error!,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 12, color: _muted),
          ),
        ],
      ],
    );
  }
}

/// One event in full: every frame the AI was shown, its whole answer, and a
/// jump to the recording.
class EventDetailScreen extends StatefulWidget {
  final GuardEvent event;
  final EventsApi eventsApi;
  final String today;
  final VoidCallback? onPlayRecording;

  const EventDetailScreen({
    super.key,
    required this.event,
    required this.eventsApi,
    required this.today,
    this.onPlayRecording,
  });

  @override
  State<EventDetailScreen> createState() => _EventDetailScreenState();
}

class _EventDetailScreenState extends State<EventDetailScreen> {
  final PageController _pages = PageController();
  int _page = 0;

  late final List<String> _frames = () {
    final e = widget.event;
    final frames = e.assessment?.frameUrls ?? const <String>[];
    final urls = frames.isNotEmpty
        ? frames
        : [if (e.thumbnailUrl != null) e.thumbnailUrl!];
    return urls.map(widget.eventsApi.absoluteUrl).toList();
  }();

  @override
  void dispose() {
    _pages.dispose();
    super.dispose();
  }

  void _step(int delta) {
    final next = (_page + delta).clamp(0, _frames.length - 1);
    _pages.animateToPage(
      next,
      duration: const Duration(milliseconds: 200),
      curve: Curves.easeOut,
    );
  }

  void _zoom(String url) {
    showDialog<void>(
      context: context,
      builder: (ctx) => Dialog.fullscreen(
        backgroundColor: Colors.black,
        child: Stack(
          children: [
            Positioned.fill(
              child: InteractiveViewer(
                maxScale: 8,
                child: Center(child: Image.network(url, fit: BoxFit.contain)),
              ),
            ),
            Positioned(
              top: 8,
              right: 8,
              child: SafeArea(
                child: IconButton(
                  icon: const Icon(Icons.close),
                  tooltip: 'Close',
                  onPressed: () => Navigator.of(ctx).pop(),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final e = widget.event;
    return Scaffold(
      appBar: AppBar(
        title: Text(
          '${e.cameraName} · ${formatEventTime(e.startTime, widget.today)}',
          style: const TextStyle(fontSize: 16),
        ),
      ),
      body: LayoutBuilder(builder: (context, constraints) {
        // Wide window: frames left, text right. Phone: stacked.
        if (constraints.maxWidth >= 1000) {
          return Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 1500),
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      flex: 3,
                      child: SingleChildScrollView(child: _buildFrames()),
                    ),
                    const SizedBox(width: 20),
                    Expanded(
                      flex: 2,
                      child: SingleChildScrollView(child: _buildText()),
                    ),
                  ],
                ),
              ),
            ),
          );
        }
        return SingleChildScrollView(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _buildFrames(),
              const SizedBox(height: 14),
              _buildText(),
            ],
          ),
        );
      }),
    );
  }

  Widget _buildFrames() {
    if (_frames.isEmpty) {
      return const AspectRatio(
        aspectRatio: 16 / 9,
        child: ClipRRect(
          borderRadius: BorderRadius.all(Radius.circular(8)),
          child: _EventImage(url: null),
        ),
      );
    }
    final many = _frames.length > 1;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        AspectRatio(
          aspectRatio: 16 / 9,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: Stack(
              children: [
                Positioned.fill(
                  child: ColoredBox(
                    color: Colors.black,
                    child: PageView.builder(
                      controller: _pages,
                      itemCount: _frames.length,
                      onPageChanged: (i) => setState(() => _page = i),
                      itemBuilder: (_, i) => GestureDetector(
                        onTap: () => _zoom(_frames[i]),
                        child: _EventImage(url: _frames[i], fit: BoxFit.contain),
                      ),
                    ),
                  ),
                ),
                // Chevrons: a mouse cannot swipe a PageView.
                if (many && _page > 0)
                  _navButton(Alignment.centerLeft, Icons.chevron_left, () => _step(-1)),
                if (many && _page < _frames.length - 1)
                  _navButton(Alignment.centerRight, Icons.chevron_right, () => _step(1)),
              ],
            ),
          ),
        ),
        const SizedBox(height: 6),
        Text(
          many
              ? 'Frame ${_page + 1} of ${_frames.length} the AI saw · tap to zoom'
              : 'Tap to zoom',
          style: const TextStyle(fontSize: 12, color: _muted),
        ),
      ],
    );
  }

  Widget _navButton(Alignment alignment, IconData icon, VoidCallback onTap) {
    return Align(
      alignment: alignment,
      child: Padding(
        padding: const EdgeInsets.all(6),
        child: Material(
          color: Colors.black54,
          shape: const CircleBorder(),
          clipBehavior: Clip.antiAlias,
          child: IconButton(icon: Icon(icon), onPressed: onTap),
        ),
      ),
    );
  }

  Widget _buildText() {
    final e = widget.event;
    final a = e.assessment;
    final ok = a != null && a.isOk;
    final rows = <(String, String)>[
      if (ok && (a.subject ?? '').isNotEmpty) ('Subject', _capitalise(a.subject!)),
      if (ok && a.peopleCount != null) ('People', '${a.peopleCount}'),
      if (ok && (a.activity ?? '').isNotEmpty) ('Activity', a.activity!),
      if (ok && (a.reason ?? '').isNotEmpty) ('Reason', a.reason!),
      if (ok) ('AI would alert', a.wouldAlert ? 'Yes' : 'No'),
      (
        'Alert fired',
        e.scriptsFired.isEmpty ? 'No' : e.scriptsFired.join(', '),
      ),
      (
        'Detection',
        [
          e.label ?? 'motion',
          if (e.confidence != null) '${(e.confidence! * 100).round()}%',
        ].join(' '),
      ),
      (
        'Time',
        [
          formatEventTime(e.startTime, widget.today),
          if (e.endTime != null) formatEventTime(e.endTime!, widget.today),
        ].join(' to '),
      ),
      if (a != null && (a.model ?? '').isNotEmpty) ('Model', a.model!),
      if (a != null && a.latencyMs != null)
        ('Latency', '${(a.latencyMs! / 1000).toStringAsFixed(1)} s'),
      if (a != null && a.createdAt != null)
        ('Assessed', formatEventTime(a.createdAt!, widget.today)),
    ];

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(spacing: 6, runSpacing: 4, children: _statusChips(e)),
        const SizedBox(height: 12),
        if (ok && (a.description ?? '').isNotEmpty)
          SelectableText(
            a.description!,
            style: const TextStyle(fontSize: 16, height: 1.4),
          )
        else if (a == null)
          const Text(
            'The AI guard has not assessed this event.',
            style: TextStyle(fontSize: 14, color: _mutedLight),
          )
        else if (!a.isOk)
          SelectableText(
            (a.error ?? '').isNotEmpty ? a.error! : 'The assessment failed.',
            style: const TextStyle(fontSize: 14, color: _mutedLight),
          ),
        const SizedBox(height: 16),
        for (final (label, value) in rows)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 104,
                  child: Text(label, style: const TextStyle(fontSize: 13, color: _muted)),
                ),
                Expanded(
                  child: SelectableText(value, style: const TextStyle(fontSize: 13)),
                ),
              ],
            ),
          ),
        if (widget.onPlayRecording != null) ...[
          const SizedBox(height: 16),
          ElevatedButton.icon(
            onPressed: widget.onPlayRecording,
            icon: const Icon(Icons.play_arrow, size: 20),
            label: const Text('Play recording'),
          ),
        ],
      ],
    );
  }
}
