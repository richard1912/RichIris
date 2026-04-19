import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/face.dart';
import '../services/face_api.dart';

// TEMP FACE-DIAG
void _diag(String msg) => debugPrint('[FACE-DIAG] $msg');

class FaceEnrollmentScreen extends StatefulWidget {
  final FaceApi faceApi;
  final List<Camera> cameras;
  final Face? seedFace;

  const FaceEnrollmentScreen({
    super.key,
    required this.faceApi,
    required this.cameras,
    this.seedFace,
  });

  @override
  State<FaceEnrollmentScreen> createState() => _FaceEnrollmentScreenState();
}

class _FaceEnrollmentScreenState extends State<FaceEnrollmentScreen> {
  List<UnlabeledThumb> _thumbs = [];
  List<Face> _faces = [];
  bool _loading = true;
  String? _error;
  int? _cameraFilter;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    _diag('enrollment._load start seedFace=${widget.seedFace?.id} cameraFilter=$_cameraFilter'); // TEMP FACE-DIAG
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      // TEMP FACE-DIAG: these run sequentially — note gap between the two
      // API calls in the backend bench traces. Could be parallelised.
      final thumbs = await widget.faceApi.unlabeledThumbnails(cameraId: _cameraFilter);
      final swFaces = Stopwatch()..start(); // TEMP FACE-DIAG
      final faces = await widget.faceApi.fetchAll();
      _diag('enrollment._load thumbs=${thumbs.length} faces=${faces.length} faces_only=${swFaces.elapsedMilliseconds}ms total=${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
      if (mounted) {
        setState(() {
          _thumbs = thumbs;
          _faces = faces;
          _loading = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = '$e';
          _loading = false;
        });
      }
    }
  }

  Future<void> _onThumbTap(UnlabeledThumb t) async {
    _diag('onThumbTap event=${t.eventId} camera=${t.cameraName} seedFace=${widget.seedFace?.id}'); // TEMP FACE-DIAG
    // When the caller already chose a face ("Add samples" from face detail),
    // skip the assign dialog entirely and enroll straight into that face.
    if (widget.seedFace != null) {
      await _enrollForFace(t, widget.seedFace!.id);
      return;
    }

    // Otherwise ask which face to assign (or create new)
    final choice = await showDialog<_AssignChoice>(
      context: context,
      builder: (ctx) => _AssignDialog(faces: _faces, seedFace: widget.seedFace),
    );
    if (choice == null) return;

    int faceId;
    if (choice.newName != null) {
      final name = choice.newName!;
      try {
        final created = await widget.faceApi.create(name);
        faceId = created.id;
        _faces = [..._faces, created];
      } catch (e) {
        // Reuse an existing face with the same name (e.g. after a prior
        // no_face attempt that already created the record)
        final existing = _faces.where(
          (f) => (f.name ?? '').toLowerCase() == name.toLowerCase(),
        ).firstOrNull;
        if (existing != null) {
          faceId = existing.id;
        } else {
          // Refresh the list in case it was created by someone else
          try {
            final faces = await widget.faceApi.fetchAll();
            final match = faces.where(
              (f) => (f.name ?? '').toLowerCase() == name.toLowerCase(),
            ).firstOrNull;
            if (match != null) {
              faceId = match.id;
              _faces = faces;
            } else {
              _showError('Failed to create face: $e');
              return;
            }
          } catch (_) {
            _showError('Failed to create face: $e');
            return;
          }
        }
      }
    } else if (choice.existingFaceId != null) {
      faceId = choice.existingFaceId!;
    } else {
      return;
    }

    await _enrollForFace(t, faceId);
  }

  Future<void> _enrollForFace(UnlabeledThumb t, int faceId) async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    _diag('enrollForFace event=${t.eventId} -> face=$faceId'); // TEMP FACE-DIAG
    try {
      final path = await widget.faceApi.eventThumbnailPath(t.eventId);
      final swEnroll = Stopwatch()..start(); // TEMP FACE-DIAG
      final result = await widget.faceApi.enroll(faceId, path);
      _diag('enrollForFace first-try status=${result.status} ${swEnroll.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
      if (!mounted) return;
      if (result.status == 'enrolled') {
        _markAssigned(t, faceId);
      } else if (result.status == 'no_face') {
        _showError('No face detected in that thumbnail');
      } else if (result.status == 'multiple_faces') {
        _diag('enrollForFace multiple_faces candidates=${result.candidates.length} waiting on user pick'); // TEMP FACE-DIAG
        final pickSw = Stopwatch()..start(); // TEMP FACE-DIAG
        final candidate = await showDialog<FaceEnrollCandidate>(
          context: context,
          builder: (ctx) => _CandidatePickerDialog(
            thumbnailUrl: t.thumbnailUrl,
            candidates: result.candidates,
          ),
        );
        _diag('enrollForFace candidate picked=${candidate?.bbox} pick_ms=${pickSw.elapsedMilliseconds}'); // TEMP FACE-DIAG
        if (candidate == null) return;
        final swAgain = Stopwatch()..start(); // TEMP FACE-DIAG
        final again = await widget.faceApi.enroll(faceId, path, bbox: candidate.bbox);
        _diag('enrollForFace retry status=${again.status} ${swAgain.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
        if (!mounted) return;
        if (again.status == 'enrolled') {
          _markAssigned(t, faceId);
        } else {
          _showError('Enroll failed: ${again.status}');
        }
      }
    } catch (e) {
      _diag('enrollForFace ERROR $e'); // TEMP FACE-DIAG
      _showError('Enroll failed: $e');
    } finally {
      _diag('enrollForFace DONE event=${t.eventId} face=$faceId total=${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
    }
  }

  void _markAssigned(UnlabeledThumb t, int faceId) {
    final face = _faces.where((f) => f.id == faceId).firstOrNull;
    final name = face?.name ?? widget.seedFace?.name ?? 'face $faceId';
    setState(() {
      if (!t.assignedFaceNames.contains(name)) {
        t.assignedFaceNames = [...t.assignedFaceNames, name];
      }
    });
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Enrolled as $name'),
        duration: const Duration(seconds: 2),
        backgroundColor: const Color(0xFF06B6D4),
      ),
    );
  }

  void _showError(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(msg), backgroundColor: Colors.red),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Enroll from detections', style: TextStyle(fontSize: 16)),
        actions: [
          IconButton(icon: const Icon(Icons.refresh), onPressed: _load),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              children: [
                const Text('Camera: ', style: TextStyle(fontSize: 13)),
                Expanded(
                  child: DropdownButton<int?>(
                    isExpanded: true,
                    value: _cameraFilter,
                    hint: const Text('All cameras'),
                    items: [
                      const DropdownMenuItem<int?>(value: null, child: Text('All cameras')),
                      ...widget.cameras.map((c) => DropdownMenuItem<int?>(
                            value: c.id,
                            child: Text(c.name),
                          )),
                    ],
                    onChanged: (v) {
                      setState(() => _cameraFilter = v);
                      _load();
                    },
                  ),
                ),
              ],
            ),
          ),
          Expanded(child: _buildGrid()),
        ],
      ),
    );
  }

  Widget _buildGrid() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return Center(child: Text('Error: $_error'));
    if (_thumbs.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(32),
          child: Text(
            'No person detections found in the last 7 days.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Color(0xFF737373)),
          ),
        ),
      );
    }
    final baseUrl = widget.faceApi.embeddingCropUrl(0).split('/api/')[0];
    return GridView.builder(
      padding: const EdgeInsets.all(8),
      gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
        maxCrossAxisExtent: 240,
        mainAxisSpacing: 8,
        crossAxisSpacing: 8,
        childAspectRatio: 16 / 10,
      ),
      itemCount: _thumbs.length,
      itemBuilder: (context, i) {
        final t = _thumbs[i];
        final isAssigned = t.assignedFaceNames.isNotEmpty;
        return GestureDetector(
          onTap: () => _onThumbTap(t),
          child: Card(
            clipBehavior: Clip.antiAlias,
            child: Stack(
              fit: StackFit.expand,
              children: [
                // Dim already-enrolled thumbnails so the user's attention is
                // drawn to the outstanding ones
                Opacity(
                  opacity: isAssigned ? 0.55 : 1.0,
                  child: Image.network('$baseUrl${t.thumbnailUrl}', fit: BoxFit.cover),
                ),
                // Corner badge with enrolled face name(s)
                if (isAssigned)
                  Positioned(
                    top: 4,
                    right: 4,
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(
                        color: const Color(0xFF06B6D4),
                        borderRadius: BorderRadius.circular(10),
                        boxShadow: const [
                          BoxShadow(color: Colors.black38, blurRadius: 3, offset: Offset(0, 1)),
                        ],
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Icon(Icons.check_circle, size: 12, color: Colors.white),
                          const SizedBox(width: 3),
                          Text(
                            t.assignedFaceNames.join(', '),
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 11,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                Positioned(
                  left: 0,
                  right: 0,
                  bottom: 0,
                  child: Container(
                    color: Colors.black54,
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    child: Text(
                      '${t.cameraName}  ${t.startTime.substring(11, 16)}',
                      style: const TextStyle(color: Colors.white, fontSize: 11),
                    ),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}

class _AssignChoice {
  final int? existingFaceId;
  final String? newName;
  _AssignChoice({this.existingFaceId, this.newName});
}

class _AssignDialog extends StatefulWidget {
  final List<Face> faces;
  final Face? seedFace;
  const _AssignDialog({required this.faces, this.seedFace});

  @override
  State<_AssignDialog> createState() => _AssignDialogState();
}

class _AssignDialogState extends State<_AssignDialog> {
  int? _selected;
  final _controller = TextEditingController();

  @override
  void initState() {
    super.initState();
    _selected = widget.seedFace?.id;
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Assign face'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (widget.faces.isNotEmpty) ...[
              const Text('Assign to existing:', style: TextStyle(fontSize: 13)),
              const SizedBox(height: 4),
              DropdownButton<int>(
                isExpanded: true,
                value: _selected,
                hint: const Text('— pick —'),
                items: widget.faces
                    .map((f) => DropdownMenuItem(value: f.id, child: Text(f.displayName)))
                    .toList(),
                onChanged: (v) => setState(() {
                  _selected = v;
                  _controller.clear();
                }),
              ),
              const SizedBox(height: 12),
              const Divider(),
            ],
            const Text('Or create new:', style: TextStyle(fontSize: 13)),
            const SizedBox(height: 4),
            TextField(
              controller: _controller,
              decoration: const InputDecoration(
                hintText: 'New face name',
                border: OutlineInputBorder(),
              ),
              onChanged: (_) => setState(() => _selected = null),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        ValueListenableBuilder<TextEditingValue>(
          valueListenable: _controller,
          builder: (context, value, _) {
            final newName = value.text.trim();
            final canAssign = newName.isNotEmpty || _selected != null;
            return ElevatedButton(
              onPressed: canAssign
                  ? () {
                      if (newName.isNotEmpty) {
                        Navigator.pop(context, _AssignChoice(newName: newName));
                      } else {
                        Navigator.pop(context, _AssignChoice(existingFaceId: _selected));
                      }
                    }
                  : null,
              child: const Text('Assign'),
            );
          },
        ),
      ],
    );
  }
}

class _CandidatePickerDialog extends StatelessWidget {
  final String thumbnailUrl;
  final List<FaceEnrollCandidate> candidates;

  const _CandidatePickerDialog({required this.thumbnailUrl, required this.candidates});

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Multiple faces detected — pick one'),
      content: SizedBox(
        width: 400,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: candidates
              .asMap()
              .entries
              .map((entry) => ListTile(
                    leading: CircleAvatar(child: Text('${entry.key + 1}')),
                    title: Text('Face ${entry.key + 1}'),
                    subtitle: Text(
                      'Score: ${(entry.value.score * 100).toStringAsFixed(0)}%  '
                      '@ ${entry.value.bbox.join(",")}',
                      style: const TextStyle(fontSize: 11),
                    ),
                    onTap: () => Navigator.pop(context, entry.value),
                  ))
              .toList(),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
      ],
    );
  }
}
