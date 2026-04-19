import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import '../models/camera.dart';
import '../models/face.dart';
import '../services/face_api.dart';
import '../services/motion_api.dart';
import '../widgets/face_cluster_card.dart';
import 'face_enrollment_screen.dart';
import 'face_detail_screen.dart';

// TEMP FACE-DIAG
void _diag(String msg) => debugPrint('[FACE-DIAG] $msg');

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

class _FacesScreenState extends State<FacesScreen> with SingleTickerProviderStateMixin {
  late final TabController _tabs;
  List<Face> _faces = [];
  List<FaceCluster> _clusters = [];
  int _suggestionMinSize = 3;
  bool _loadingFaces = true;
  bool _loadingClusters = true;
  String? _facesError;
  String? _clustersError;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 2, vsync: this);
    _loadFaces();
    _loadClusters();
  }

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  Future<void> _loadFaces() async {
    setState(() {
      _loadingFaces = true;
      _facesError = null;
    });
    try {
      final faces = await widget.faceApi.fetchAll();
      if (mounted) setState(() { _faces = faces; _loadingFaces = false; });
    } catch (e) {
      if (mounted) setState(() { _facesError = '$e'; _loadingFaces = false; });
    }
  }

  Future<void> _loadClusters() async {
    setState(() {
      _loadingClusters = true;
      _clustersError = null;
    });
    try {
      final clusters = await widget.faceApi.listClusters(minSize: _suggestionMinSize);
      if (mounted) setState(() { _clusters = clusters; _loadingClusters = false; });
    } catch (e) {
      if (mounted) setState(() { _clustersError = '$e'; _loadingClusters = false; });
    }
  }

  Future<void> _openEnrollment({Face? seedFace}) async {
    _diag('FLOW START: Enroll-from-detections (seed=${seedFace?.id})');
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => FaceEnrollmentScreen(
          faceApi: widget.faceApi,
          cameras: widget.cameras,
          seedFace: seedFace,
        ),
      ),
    );
    if (mounted) { _loadFaces(); _loadClusters(); }
  }

  Future<void> _openDetail(Face face) async {
    _diag('FLOW START: Face detail id=${face.id} name=${face.name}');
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => FaceDetailScreen(
          face: face,
          faceApi: widget.faceApi,
          cameras: widget.cameras,
        ),
      ),
    );
    if (mounted) { _loadFaces(); _loadClusters(); }
  }

  Future<void> _nameCluster(FaceCluster c) async {
    final controller = TextEditingController();
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Name this person'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'e.g. a name'),
          onSubmitted: (v) => Navigator.of(ctx).pop(v.trim()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (name == null || name.isEmpty) return;

    // Client-side clash detection: if the user typed an existing name, offer
    // to merge this cluster into that person instead of creating a duplicate.
    final existing = _faces.where(
      (f) => (f.name ?? '').toLowerCase() == name.toLowerCase(),
    ).firstOrNull;
    if (existing != null) {
      await _offerMergeForExistingName(c, existing);
      return;
    }

    try {
      await widget.faceApi.nameCluster(c.id, name);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Named cluster #${c.id} → "$name"')),
        );
        _loadFaces();
        _loadClusters();
      }
    } on DioException catch (e) {
      // Defense-in-depth: if _faces was stale (cluster worker created a new
      // named face since we last loaded), the server will still reject with
      // 409 — fall back to the merge offer by re-fetching and retrying.
      if (e.response?.statusCode == 409) {
        await _loadFaces();
        final existing = _faces.where(
          (f) => (f.name ?? '').toLowerCase() == name.toLowerCase(),
        ).firstOrNull;
        if (existing != null) {
          await _offerMergeForExistingName(c, existing);
          return;
        }
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Failed to name cluster: $e')),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Failed to name cluster: $e')),
        );
      }
    }
  }

  Future<void> _offerMergeForExistingName(FaceCluster c, Face existing) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('"${existing.displayName}" already exists'),
        content: Text(
          'A person named "${existing.displayName}" is already enrolled with '
          '${existing.embeddingCount} sample${existing.embeddingCount == 1 ? '' : 's'}. '
          'Merge this cluster (${c.embeddingCount} face${c.embeddingCount == 1 ? '' : 's'}) '
          'into them?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Merge'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await widget.faceApi.mergeCluster(c.id, existing.id);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Merged into "${existing.displayName}"')),
        );
        _loadFaces();
        _loadClusters();
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Merge failed: $e')),
        );
      }
    }
  }

  Future<void> _mergeCluster(FaceCluster c) async {
    final named = _faces.where((f) => f.id != c.id).toList();
    if (named.isEmpty) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('No named people yet — use "Name this person" first.')),
        );
      }
      return;
    }
    final targetId = await showDialog<int>(
      context: context,
      builder: (ctx) => SimpleDialog(
        title: const Text('Merge cluster into…'),
        children: named
            .map((f) => SimpleDialogOption(
                  onPressed: () => Navigator.of(ctx).pop(f.id),
                  child: Text(f.displayName),
                ))
            .toList(),
      ),
    );
    if (targetId == null) return;
    try {
      await widget.faceApi.mergeCluster(c.id, targetId);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Cluster merged')),
        );
        _loadFaces();
        _loadClusters();
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Merge failed: $e')),
        );
      }
    }
  }

  Future<void> _discardCluster(FaceCluster c) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Discard cluster?'),
        content: Text('This deletes the suggested person (${c.embeddingCount} faces).'),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Discard'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await widget.faceApi.discardCluster(c.id);
      if (mounted) _loadClusters();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Discard failed: $e')),
        );
      }
    }
  }

  Future<void> _recluster() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Re-run clustering?'),
        content: const Text(
          'Deletes all auto-clustered suggestions and rebuilds them from the queue. '
          'Named people and their user-enrolled samples are preserved.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.of(ctx).pop(true), child: const Text('Re-cluster')),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await widget.faceApi.recluster();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Re-clustering started — refresh in a minute.')),
        );
        _loadClusters();
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Re-cluster failed: $e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: widget.onBack != null
            ? IconButton(icon: const Icon(Icons.arrow_back), onPressed: widget.onBack)
            : null,
        title: const Text('Faces', style: TextStyle(fontSize: 16)),
        bottom: TabBar(
          controller: _tabs,
          tabs: [
            const Tab(text: 'People'),
            Tab(text: 'Suggestions (${_clusters.length})'),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: () { _loadFaces(); _loadClusters(); },
          ),
          PopupMenuButton<String>(
            onSelected: (v) {
              if (v == 'recluster') _recluster();
            },
            itemBuilder: (_) => const [
              PopupMenuItem(value: 'recluster', child: Text('Re-run clustering')),
            ],
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        icon: const Icon(Icons.person_add),
        label: const Text('Enroll from detections'),
        onPressed: () => _openEnrollment(),
      ),
      body: TabBarView(
        controller: _tabs,
        children: [
          _buildPeopleTab(),
          _buildSuggestionsTab(),
        ],
      ),
    );
  }

  Widget _buildPeopleTab() {
    if (_loadingFaces) return const Center(child: CircularProgressIndicator());
    if (_facesError != null) return Center(child: Text('Error: $_facesError'));
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
            title: Text(f.displayName, style: const TextStyle(fontWeight: FontWeight.w600)),
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

  Widget _buildSuggestionsTab() {
    if (_loadingClusters) return const Center(child: CircularProgressIndicator());
    if (_clustersError != null) return Center(child: Text('Error: $_clustersError'));

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
          child: Row(
            children: [
              const Text('Min faces:', style: TextStyle(fontSize: 12, color: Color(0xFF737373))),
              const SizedBox(width: 8),
              DropdownButton<int>(
                value: _suggestionMinSize,
                isDense: true,
                items: const [1, 2, 3, 5, 10]
                    .map((v) => DropdownMenuItem(value: v, child: Text('$v')))
                    .toList(),
                onChanged: (v) {
                  if (v != null) {
                    setState(() => _suggestionMinSize = v);
                    _loadClusters();
                  }
                },
              ),
              const Spacer(),
              Text('${_clusters.length} cluster${_clusters.length == 1 ? '' : 's'}',
                  style: const TextStyle(fontSize: 12, color: Color(0xFF737373))),
            ],
          ),
        ),
        Expanded(
          child: _clusters.isEmpty
              ? const Center(
                  child: Padding(
                    padding: EdgeInsets.all(32),
                    child: Text(
                      'No cluster suggestions yet.\n\n'
                      'Unknown faces from your cameras will group here as the clusterer runs.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Color(0xFF737373)),
                    ),
                  ),
                )
              : GridView.builder(
                  padding: const EdgeInsets.all(16),
                  gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                    maxCrossAxisExtent: 280,
                    mainAxisSpacing: 12,
                    crossAxisSpacing: 12,
                    childAspectRatio: 0.78,
                  ),
                  itemCount: _clusters.length,
                  itemBuilder: (context, i) {
                    final c = _clusters[i];
                    return FaceClusterCard(
                      cluster: c,
                      faceApi: widget.faceApi,
                      onName: () => _nameCluster(c),
                      onMerge: () => _mergeCluster(c),
                      onDiscard: () => _discardCluster(c),
                    );
                  },
                ),
        ),
      ],
    );
  }
}
