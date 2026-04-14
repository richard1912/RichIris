import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/face.dart';
import '../services/face_api.dart';
import '../services/motion_api.dart';
import 'face_enrollment_screen.dart';
import 'face_detail_screen.dart';

class FacesScreen extends StatefulWidget {
  final FaceApi faceApi;
  final MotionApi motionApi;
  final List<Camera> cameras;
  final VoidCallback? onBack;

  const FacesScreen({
    super.key,
    required this.faceApi,
    required this.motionApi,
    required this.cameras,
    this.onBack,
  });

  @override
  State<FacesScreen> createState() => _FacesScreenState();
}

class _FacesScreenState extends State<FacesScreen> {
  List<Face> _faces = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final faces = await widget.faceApi.fetchAll();
      if (mounted) {
        setState(() {
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

  Future<void> _openEnrollment({Face? seedFace}) async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => FaceEnrollmentScreen(
          faceApi: widget.faceApi,
          cameras: widget.cameras,
          seedFace: seedFace,
        ),
      ),
    );
    if (mounted) _load();
  }

  Future<void> _openDetail(Face face) async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => FaceDetailScreen(
          face: face,
          faceApi: widget.faceApi,
          cameras: widget.cameras,
        ),
      ),
    );
    if (mounted) _load();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: widget.onBack != null
            ? IconButton(icon: const Icon(Icons.arrow_back), onPressed: widget.onBack)
            : null,
        title: const Text('Faces', style: TextStyle(fontSize: 16)),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _load,
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        icon: const Icon(Icons.person_add),
        label: const Text('Enroll from detections'),
        onPressed: () => _openEnrollment(),
      ),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(child: Text('Error: $_error'));
    }
    if (_faces.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(32),
          child: Text(
            'No faces enrolled yet.\n\nTap "Enroll from detections" to tag faces from past motion thumbnails.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Color(0xFF737373)),
          ),
        ),
      );
    }
    return ListView.separated(
      padding: const EdgeInsets.all(16),
      itemCount: _faces.length,
      separatorBuilder: (_, __) => const SizedBox(height: 8),
      itemBuilder: (context, i) {
        final f = _faces[i];
        return Card(
          child: ListTile(
            leading: CircleAvatar(
              radius: 24,
              backgroundColor: const Color(0xFF1F2937),
              backgroundImage: f.latestCropPath != null
                  ? NetworkImage(widget.faceApi.latestCropUrl(f.id))
                  : null,
              child: f.latestCropPath == null
                  ? const Icon(Icons.person, color: Color(0xFF9CA3AF))
                  : null,
            ),
            title: Text(f.name, style: const TextStyle(fontWeight: FontWeight.w600)),
            subtitle: Text(
              '${f.embeddingCount} sample${f.embeddingCount == 1 ? '' : 's'}'
              '${f.notes != null && f.notes!.isNotEmpty ? '  —  ${f.notes}' : ''}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF737373)),
            ),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => _openDetail(f),
          ),
        );
      },
    );
  }
}
