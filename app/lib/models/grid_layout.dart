/// Layout definitions for the camera grid video wall.
///
/// Each [GridLayout] is a list of [GridSlot]s, where slot rects are
/// expressed as fractions of the available grid area (0.0 – 1.0). The
/// renderer multiplies these by the grid's pixel size at build time.
library;

class GridSlot {
  final double x;
  final double y;
  final double w;
  final double h;
  const GridSlot(this.x, this.y, this.w, this.h);
}

class GridLayout {
  final String id;
  final String label;
  final List<GridSlot> slots;
  const GridLayout({required this.id, required this.label, required this.slots});

  int get slotCount => slots.length;
}

// Uniform N×M grids — slots enumerated row-major (left-to-right, top-to-bottom).
List<GridSlot> _uniform(int cols, int rows) {
  final w = 1.0 / cols;
  final h = 1.0 / rows;
  final slots = <GridSlot>[];
  for (var r = 0; r < rows; r++) {
    for (var c = 0; c < cols; c++) {
      slots.add(GridSlot(c * w, r * h, w, h));
    }
  }
  return slots;
}

// Featured-6: 3×3 grid with the top-left 2×2 block merged into one big tile.
// Slot 0 = big tile. Slots 1–2 = right column. Slots 3–5 = bottom row.
final _featured6 = <GridSlot>[
  const GridSlot(0.0, 0.0, 2.0 / 3.0, 2.0 / 3.0),
  const GridSlot(2.0 / 3.0, 0.0, 1.0 / 3.0, 1.0 / 3.0),
  GridSlot(2.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
  const GridSlot(0.0, 2.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
  GridSlot(1.0 / 3.0, 2.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
  GridSlot(2.0 / 3.0, 2.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
];

// Featured-9: 4×3 grid with the top-left 2×2 block merged.
// Slot 0 = big tile (cols 0–1, rows 0–1). Slots 1–4 = rows 0–1 right half.
// Slots 5–8 = bottom row.
final _featured9 = <GridSlot>[
  const GridSlot(0.0, 0.0, 0.5, 2.0 / 3.0),
  const GridSlot(0.5, 0.0, 0.25, 1.0 / 3.0),
  const GridSlot(0.75, 0.0, 0.25, 1.0 / 3.0),
  GridSlot(0.5, 1.0 / 3.0, 0.25, 1.0 / 3.0),
  GridSlot(0.75, 1.0 / 3.0, 0.25, 1.0 / 3.0),
  const GridSlot(0.0, 2.0 / 3.0, 0.25, 1.0 / 3.0),
  const GridSlot(0.25, 2.0 / 3.0, 0.25, 1.0 / 3.0),
  const GridSlot(0.5, 2.0 / 3.0, 0.25, 1.0 / 3.0),
  const GridSlot(0.75, 2.0 / 3.0, 0.25, 1.0 / 3.0),
];

final kGridLayouts = <GridLayout>[
  GridLayout(id: 'uniform-1', label: '1', slots: _uniform(1, 1)),
  GridLayout(id: 'uniform-2x2', label: '4', slots: _uniform(2, 2)),
  GridLayout(id: 'uniform-3x2', label: '6', slots: _uniform(3, 2)),
  GridLayout(id: 'uniform-3x3', label: '9', slots: _uniform(3, 3)),
  GridLayout(id: 'uniform-4x3', label: '12', slots: _uniform(4, 3)),
  GridLayout(id: 'uniform-4x4', label: '16', slots: _uniform(4, 4)),
  GridLayout(id: 'uniform-5x5', label: '25', slots: _uniform(5, 5)),
  GridLayout(id: 'featured-6', label: 'Featured 6', slots: _featured6),
  GridLayout(id: 'featured-9', label: 'Featured 9', slots: _featured9),
];

const kDefaultLayoutId = 'uniform-3x3';

GridLayout gridLayoutById(String? id) {
  for (final l in kGridLayouts) {
    if (l.id == id) return l;
  }
  return kGridLayouts.firstWhere((l) => l.id == kDefaultLayoutId);
}
