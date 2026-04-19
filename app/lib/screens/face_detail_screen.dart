import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/face.dart';
import '../services/face_api.dart';
import 'face_enrollment_screen.dart';

// TEMP FACE-DIAG
void _diag(String msg) => debugPrint('[FACE-DIAG] $msg');

class FaceDetailScreen extends StatefulWidget {
  final Face face;
  final FaceApi faceApi;
  final List<Camera> cameras;

  const FaceDetailScreen({
    super.key,
    required this.face,
    required this.faceApi,
    required this.cameras,
  });

  @override
  State<FaceDetailScreen> createState() => _FaceDetailScreenState();
}

class _FaceDetailScreenState extends State<FaceDetailScreen> {
  List<FaceEmbeddingInfo> _embeddings = [];
  bool _loading = true;
  late String _name = widget.face.displayName;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    _diag('detail._load face=${widget.face.id} name=${widget.face.name}'); // TEMP FACE-DIAG
    setState(() => _loading = true);
    try {
      final emb = await widget.faceApi.listEmbeddings(widget.face.id);
      _diag('detail._load done count=${emb.length} ${sw.elapsedMilliseconds}ms'); // TEMP FACE-DIAG
      if (mounted) {
        setState(() {
          _embeddings = emb;
          _loading = false;
        });
      }
    } catch (e) {
      _diag('detail._load ERROR $e'); // TEMP FACE-DIAG
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _rename() async {
    final controller = TextEditingController(text: _name);
    final result = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Rename face'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(border: OutlineInputBorder()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () => Navigator.pop(ctx, controller.text.trim()),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (result == null || result.isEmpty || result == _name) return;
    try {
      final updated = await widget.faceApi.update(widget.face.id, name: result);
      if (mounted) setState(() => _name = updated.displayName);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Rename failed: $e'), backgroundColor: Colors.red),
        );
      }
    }
  }

  Future<void> _delete() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Delete "$_name"?'),
        content: const Text('This removes the face and all its samples. Motion events that previously matched this face will be re-classified as unknown on next detection.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await widget.faceApi.delete(widget.face.id);
      if (mounted) Navigator.pop(context);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Delete failed: $e'), backgroundColor: Colors.red),
        );
      }
    }
  }

  Future<void> _addSamples() async {
    _diag('addSamples push enrollment seedFace=${widget.face.id}'); // TEMP FACE-DIAG
    final sw = Stopwatch()..start(); // TEMP FACE-DIAG
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => FaceEnrollmentScreen(
          faceApi: widget.faceApi,
          cameras: widget.cameras,
          seedFace: Face(
            id: widget.face.id,
            name: _name,
            createdAt: widget.face.createdAt,
          ),
        ),
      ),
    );
    _diag('addSamples returned in_screen_ms=${sw.elapsedMilliseconds} reloading detail'); // TEMP FACE-DIAG
    if (mounted) _load();
  }

  Future<void> _deleteEmbedding(int embeddingId) async {
    try {
      await widget.faceApi.deleteEmbedding(embeddingId);
      _load();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Delete failed: $e'), backgroundColor: Colors.red),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_name, style: const TextStyle(fontSize: 16)),
        actions: [
          IconButton(icon: const Icon(Icons.edit), onPressed: _rename),
          IconButton(icon: const Icon(Icons.delete_outline), onPressed: _delete),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        icon: const Icon(Icons.add_a_photo),
        label: const Text('Add samples'),
        onPressed: _addSamples,
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _embeddings.isEmpty
              ? const Center(
                  child: Padding(
                    padding: EdgeInsets.all(32),
                    child: Text(
                      'No samples yet.\n\nTap "Add samples" to enroll face crops from past detections.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Color(0xFF737373)),
                    ),
                  ),
                )
              : GridView.builder(
                  padding: const EdgeInsets.all(12),
                  gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                    maxCrossAxisExtent: 140,
                    mainAxisSpacing: 8,
                    crossAxisSpacing: 8,
                    childAspectRatio: 1.0,
                  ),
                  itemCount: _embeddings.length,
                  itemBuilder: (context, i) {
                    final e = _embeddings[i];
                    return Card(
                      clipBehavior: Clip.antiAlias,
                      child: Stack(
                        fit: StackFit.expand,
                        children: [
                          e.faceCropPath != null
                              ? Image.network(
                                  widget.faceApi.embeddingCropUrl(e.id),
                                  fit: BoxFit.cover,
                                  errorBuilder: (_, __, ___) => const Center(
                                    child: Icon(Icons.broken_image, color: Color(0xFF737373)),
                                  ),
                                )
                              : const Center(child: Icon(Icons.person, size: 48)),
                          Positioned(
                            top: 2,
                            right: 2,
                            child: IconButton(
                              visualDensity: VisualDensity.compact,
                              icon: const Icon(Icons.close, size: 18, color: Colors.white),
                              style: IconButton.styleFrom(
                                backgroundColor: Colors.black54,
                                padding: const EdgeInsets.all(4),
                              ),
                              onPressed: () => _deleteEmbedding(e.id),
                            ),
                          ),
                        ],
                      ),
                    );
                  },
                ),
    );
  }
}
