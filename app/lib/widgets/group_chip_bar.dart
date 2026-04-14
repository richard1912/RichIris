import 'package:flutter/material.dart';

import '../models/camera_group.dart';
import '../services/group_api.dart';

class GroupChipBar extends StatelessWidget {
  final List<CameraGroup> groups;
  final int? selectedGroupId;
  final ValueChanged<int?> onGroupSelected;
  final VoidCallback onGroupsChanged;
  final GroupApi groupApi;

  const GroupChipBar({
    super.key,
    required this.groups,
    required this.selectedGroupId,
    required this.onGroupSelected,
    required this.onGroupsChanged,
    required this.groupApi,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 40,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 8),
        children: [
          _chip(context, label: 'All', selected: selectedGroupId == null, onTap: () => onGroupSelected(null)),
          for (final group in groups)
            _chip(
              context,
              label: group.name,
              selected: selectedGroupId == group.id,
              onTap: () => onGroupSelected(group.id),
              onLongPress: () => _showGroupOptions(context, group),
              count: group.cameraCount,
            ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
            child: ActionChip(
              avatar: const Icon(Icons.add, size: 16),
              label: const Text('Group'),
              backgroundColor: const Color(0xFF262626),
              side: const BorderSide(color: Color(0xFF404040)),
              labelStyle: const TextStyle(color: Color(0xFFA3A3A3), fontSize: 12),
              onPressed: () => _showCreateDialog(context),
              materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
              visualDensity: VisualDensity.compact,
            ),
          ),
        ],
      ),
    );
  }

  Widget _chip(
    BuildContext context, {
    required String label,
    required bool selected,
    required VoidCallback onTap,
    VoidCallback? onLongPress,
    int? count,
  }) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
      child: GestureDetector(
        onLongPress: onLongPress,
        child: ChoiceChip(
          label: Text(count != null ? '$label ($count)' : label),
          selected: selected,
          onSelected: (_) => onTap(),
          selectedColor: const Color(0xFF1E3A5F),
          backgroundColor: const Color(0xFF262626),
          side: BorderSide(color: selected ? const Color(0xFF3B82F6) : const Color(0xFF404040)),
          labelStyle: TextStyle(
            color: selected ? const Color(0xFF93C5FD) : const Color(0xFFA3A3A3),
            fontSize: 12,
          ),
          materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
          visualDensity: VisualDensity.compact,
        ),
      ),
    );
  }

  void _showCreateDialog(BuildContext context) {
    final controller = TextEditingController();
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('New Group'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(
            hintText: 'Group name',
            border: OutlineInputBorder(),
          ),
          onSubmitted: (_) async {
            final name = controller.text.trim();
            if (name.isEmpty) return;
            await groupApi.create(name);
            if (ctx.mounted) Navigator.pop(ctx);
            onGroupsChanged();
          },
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () async {
              final name = controller.text.trim();
              if (name.isEmpty) return;
              await groupApi.create(name);
              if (ctx.mounted) Navigator.pop(ctx);
              onGroupsChanged();
            },
            child: const Text('Create'),
          ),
        ],
      ),
    );
  }

  void _showGroupOptions(BuildContext context, CameraGroup group) {
    showModalBottomSheet(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.edit),
              title: const Text('Rename'),
              onTap: () {
                Navigator.pop(ctx);
                _showRenameDialog(context, group);
              },
            ),
            ListTile(
              leading: const Icon(Icons.delete, color: Colors.red),
              title: const Text('Delete', style: TextStyle(color: Colors.red)),
              subtitle: const Text('Cameras will become ungrouped'),
              onTap: () async {
                Navigator.pop(ctx);
                final confirm = await showDialog<bool>(
                  context: context,
                  builder: (ctx) => AlertDialog(
                    title: const Text('Delete Group'),
                    content: Text('Delete "${group.name}"? Cameras in this group will become ungrouped.'),
                    actions: [
                      TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
                      TextButton(
                        onPressed: () => Navigator.pop(ctx, true),
                        style: TextButton.styleFrom(foregroundColor: Colors.red),
                        child: const Text('Delete'),
                      ),
                    ],
                  ),
                );
                if (confirm == true) {
                  await groupApi.delete(group.id);
                  if (selectedGroupId == group.id) {
                    onGroupSelected(null);
                  }
                  onGroupsChanged();
                }
              },
            ),
            const Divider(),
            ListTile(
              leading: const Icon(Icons.videocam),
              title: const Text('Enable all cameras'),
              onTap: () async {
                Navigator.pop(ctx);
                await groupApi.bulkAction(group.id, 'enable');
                onGroupsChanged();
              },
            ),
            ListTile(
              leading: const Icon(Icons.videocam_off),
              title: const Text('Disable all cameras'),
              onTap: () async {
                Navigator.pop(ctx);
                await groupApi.bulkAction(group.id, 'disable');
                onGroupsChanged();
              },
            ),
            ListTile(
              leading: const Icon(Icons.shield),
              title: const Text('Arm motion detection'),
              onTap: () async {
                Navigator.pop(ctx);
                await groupApi.bulkAction(group.id, 'arm_motion');
                onGroupsChanged();
              },
            ),
            ListTile(
              leading: const Icon(Icons.shield_outlined),
              title: const Text('Disarm motion detection'),
              onTap: () async {
                Navigator.pop(ctx);
                await groupApi.bulkAction(group.id, 'disarm_motion');
                onGroupsChanged();
              },
            ),
          ],
        ),
      ),
    );
  }

  void _showRenameDialog(BuildContext context, CameraGroup group) {
    final controller = TextEditingController(text: group.name);
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Rename Group'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(
            hintText: 'Group name',
            border: OutlineInputBorder(),
          ),
          onSubmitted: (_) async {
            final name = controller.text.trim();
            if (name.isEmpty) return;
            await groupApi.update(group.id, name: name);
            if (ctx.mounted) Navigator.pop(ctx);
            onGroupsChanged();
          },
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () async {
              final name = controller.text.trim();
              if (name.isEmpty) return;
              await groupApi.update(group.id, name: name);
              if (ctx.mounted) Navigator.pop(ctx);
              onGroupsChanged();
            },
            child: const Text('Rename'),
          ),
        ],
      ),
    );
  }
}
