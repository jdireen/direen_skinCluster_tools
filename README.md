# direen_skinCluster_tools

Standalone skinCluster tools for Autodesk Maya. Provides a comprehensive set of skinCluster operations with zero external dependencies beyond Maya's standard libraries. Distributed as a single Python file.

## Features

### Public API

- **add_skin_cluster** - Create a skinCluster on geometry, even if one already exists
- **delete_skin_clusters** - Remove skinClusters from selected or specified nodes
- **rebuild_skin_cluster** - Delete and recreate skinClusters, preserving weights
- **export_skin_weights** - Export skin weights to file (index, position, or barycentric mapping)
- **import_skin_weights** - Import skin weights from file with flexible matching options (can isolate import to selected components)
- **match_influences_on_skin_clusters** - Synchronize influences across multiple skinClusters
- **copy_skin_cluster** - Copy a skinCluster from one mesh to another (matching influences if necessary)
- **average_vert_skin_weights_with_neighbors** - Smooth vertex weights using neighbor averaging
- **mirror_skin_weights / mirror_skin_clusters** - Mirror weights across an axis

### SlideVertexWeightsTool

An interactive dragger-context tool for sliding vertex skin weights between influences. Select vertices, activate the tool, then drag left/right to redistribute weight toward or away from the nearest joint influence to the cursor.

- Supports soft selection for gradual falloff
- **Ctrl + drag** for fine control (0.1x multiplier)
- **Shift + drag** for coarse control (10x multiplier)
- Full undo/redo support via a built-in lightweight MPxCommand

### SkinningMarkingMenu

A radial marking menu that provides quick access to all skinning operations directly in the Maya viewport. When triggered (via hotkey press-and-hold), it displays:

**Radial positions:**

| Position | Action |
|----------|--------|
| **N** | Slide Weights Tool |
| **NW** | Mirror Weights (negative X direction) |
| **NE** | Mirror Weights (positive X direction) |
| **W** | Smooth Weights |
| **E** | Hammer Weights |
| **SW** | Export to Temp (fully-qualified names) |
| **SE** | Import from Temp (fully-qualified names) |
| **S** | Copy skinCluster |

Note: the mirror weights operations temporarily set the time to `-1` assuming the rig will be at a default pose there.

**Sub-menus:**

- **Import / Export** - Export/import weights to temp or file, with optional fully-qualified naming
- **Influences** - Select, match, add, remove, lock/unlock influences
- **skinCluster** - Copy, mirror, add, delete, rebuild skinClusters; set normalize mode

Usage:

```python
import direen_skinCluster_tools

# Show the marking menu on left-click (bind to a hotkey press/release)
mm = direen_skinCluster_tools.SkinningMarkingMenu()

# Remove the marking menu on release
mm.remove()
```

## Installation

### Drag-and-Drop (Recommended)

1. Download or clone this repository.
2. Drag `drag_and_drop_install.py` into the Maya viewport.
3. The installer copies the module files into your Maya modules directory (`<MAYA_APP_DIR>/modules/`).
4. You will be prompted to optionally bind the SkinningMarkingMenu to a hotkey (suggested: **m** with no modifiers).
5. Restart Maya.

### Manual Installation

1. Copy `direen_skinCluster_tools.mod` to your Maya modules directory (e.g. `~/maya/modules/`).
2. Create a folder `direen_skinCluster_tools/scripts/` alongside the `.mod` file.
3. Copy `scripts/direen_skinCluster_tools.py` into that `scripts/` folder.
4. Restart Maya.

The resulting layout should be:

```
<MAYA_APP_DIR>/modules/
    direen_skinCluster_tools.mod
    direen_skinCluster_tools/
        scripts/
            direen_skinCluster_tools.py
```

### Binding the Hotkey Manually

If you skipped the hotkey prompt during installation, you can bind the SkinningMarkingMenu manually. Create a press/release hotkey pair in Maya:

**Press command** (Python):
```python
import direen_skinCluster_tools
mm = direen_skinCluster_tools.SkinningMarkingMenu()
```

**Release command** (Python):
```python
mm.remove()
```

## Requirements

- Autodesk Maya (Python 3 / Maya 2022+)
- No external dependencies

## License

MIT License - see [LICENSE](LICENSE) for details.

## Author

James Direen
