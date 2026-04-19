import 'package:flutter/material.dart';
import '../models/face.dart';
import '../services/face_api.dart';

/// Suggested-person tile: 2x2 mosaic of sample face crops, embedding count,
/// camera provenance, and quick actions (name / merge / discard).
class FaceClusterCard extends StatelessWidget {
  final FaceCluster cluster;
  final FaceApi faceApi;
  final VoidCallback onName;
  final VoidCallback onMerge;
  final VoidCallback onDiscard;

  const FaceClusterCard({
    super.key,
    required this.cluster,
    required this.faceApi,
    required this.onName,
    required this.onMerge,
    required this.onDiscard,
  });

  @override
  Widget build(BuildContext context) {
    return Card(
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          AspectRatio(
            aspectRatio: 1,
            child: _Mosaic(ids: cluster.sampleEmbeddingIds, faceApi: faceApi),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 8, 10, 4),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '${cluster.embeddingCount} face${cluster.embeddingCount == 1 ? '' : 's'}',
                  style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                ),
                if (cluster.camerasSeen.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      cluster.camerasSeen.join(', '),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 11, color: Color(0xFF737373)),
                    ),
                  ),
              ],
            ),
          ),
          const Divider(height: 1),
          Row(
            children: [
              Expanded(
                child: TextButton(
                  onPressed: onName,
                  style: TextButton.styleFrom(
                    shape: const RoundedRectangleBorder(),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                  ),
                  child: const Text('Name', style: TextStyle(fontSize: 12)),
                ),
              ),
              const VerticalDivider(width: 1, indent: 4, endIndent: 4),
              Expanded(
                child: TextButton(
                  onPressed: onMerge,
                  style: TextButton.styleFrom(
                    shape: const RoundedRectangleBorder(),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                  ),
                  child: const Text('Merge', style: TextStyle(fontSize: 12)),
                ),
              ),
              const VerticalDivider(width: 1, indent: 4, endIndent: 4),
              IconButton(
                onPressed: onDiscard,
                tooltip: 'Discard',
                icon: const Icon(Icons.delete_outline, size: 18),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _Mosaic extends StatelessWidget {
  final List<int> ids;
  final FaceApi faceApi;
  const _Mosaic({required this.ids, required this.faceApi});

  @override
  Widget build(BuildContext context) {
    if (ids.isEmpty) {
      return Container(
        color: const Color(0xFF111827),
        alignment: Alignment.center,
        child: const Icon(Icons.person, color: Color(0xFF4B5563), size: 48),
      );
    }
    final cells = <Widget>[];
    for (var i = 0; i < 4; i++) {
      if (i < ids.length) {
        cells.add(Image.network(
          faceApi.embeddingCropUrl(ids[i]),
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) => Container(color: const Color(0xFF1F2937)),
        ));
      } else {
        cells.add(Container(color: const Color(0xFF1F2937)));
      }
    }
    return GridView.count(
      crossAxisCount: 2,
      mainAxisSpacing: 2,
      crossAxisSpacing: 2,
      physics: const NeverScrollableScrollPhysics(),
      children: cells,
    );
  }
}
