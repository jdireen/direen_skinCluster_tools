from __future__ import annotations

# -------------------------------------------------------------------------------------#
# --------------------------------------------------------------------------- HEADER --#
"""
------------------------------------------------------------------------------

:Authors:
    - James Direen

:License:
    MIT License - see LICENSE file for details.

:Description:
    Standalone skinCluster tools for Maya.

    This module provides a self-contained set of skinCluster operations with
    zero external dependencies beyond Maya's standard libraries and the Python
    standard library. It is designed to be distributed as a single file.

    Public API:
        - add_skin_cluster
        - delete_skin_clusters
        - rebuild_skin_cluster
        - export_skin_weights
        - import_skin_weights
        - match_influences_on_skin_clusters
        - copy_skin_cluster
        - average_vert_skin_weights_with_neighbors

    Public Helpers:
        - find_related_deformers
        - list_skinned_geo
        - export_deformer_weights
        - import_deformer_weights
        - get_deformer_weights_data_from_file
        - get_influences_from_deformer_weights_data
        - map_deformer_weights_by_vertex
        - set_skin_weights_from_weights_data
        - set_mesh_skin_weights
        - get_skin_cluster_influences
        - get_skin_weights_from_selected_components

    Classes:
        - DraggerContext
        - SlideVertexWeightsTool
        - MarkingMenuBase
        - SkinningMarkingMenu

    This module includes a lightweight built-in undo mechanism (``_undo_commit``)
    that registers an undoable MPxCommand on first use, enabling undo/redo for
    operations like the SlideVertexWeightsTool without any external dependencies.

"""

# -------------------------------------------------------------------------------------#
# -------------------------------------------------------------------------- IMPORTS --#
# Built-in
import contextlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from functools import partial, wraps
from pathlib import Path
from typing import Any

# Third Party (Maya standard only)
import maya.OpenMaya as _om1
import maya.OpenMayaUI as _omui
from maya import cmds, mel
from maya.api import OpenMaya as OpenMaya2
from maya.api import OpenMayaAnim as OpenMayaAnim2

# -------------------------------------------------------------------------------------#
# -------------------------------------------------------------------------- GLOBALS --#
log = logging.getLogger(__name__)

GEOMETRY_TYPES = frozenset({"mesh", "lattice", "subdiv", "nurbsSurface", "nurbsCurve"})

# This file doubles as a Maya plugin so it can register a lightweight undoable
# MPxCommand.  The command is auto-loaded on first call to ``_undo_commit``.

_UNDO_CMD = "_skinclusterUtilsUndo"
_undo_shared: dict = {"undo": None, "redo": None}


def maya_useNewAPI():
    """Plugin boilerplate — signals Maya API 2.0."""


class _UndoCommand(OpenMaya2.MPxCommand):
    """Undoable command that delegates to arbitrary Python callbacks."""

    def doIt(self, args):
        self._undo = _undo_shared["undo"]
        self._redo = _undo_shared["redo"]
        _undo_shared["undo"] = None
        _undo_shared["redo"] = None

    def undoIt(self):
        self._undo()

    def redoIt(self):
        self._redo()

    def isUndoable(self):
        return True

    @staticmethod
    def creator():
        return _UndoCommand()


def initializePlugin(plugin):
    """Plugin boilerplate — register the undo command."""
    OpenMaya2.MFnPlugin(plugin).registerCommand(_UNDO_CMD, _UndoCommand.creator)


def uninitializePlugin(plugin):
    """Plugin boilerplate — deregister the undo command."""
    OpenMaya2.MFnPlugin(plugin).deregisterCommand(_UNDO_CMD)


def _undo_commit(undo, redo=lambda: None):
    """Commit *undo* and *redo* callbacks to the Maya undo queue.

    On first call this auto-loads the current file as a Maya plugin so
    that the undoable command is available.
    """
    if not hasattr(cmds, _UNDO_CMD):
        cmds.loadPlugin(__file__.replace(".pyc", ".py"), quiet=True)

    _undo_shared["undo"] = undo
    _undo_shared["redo"] = redo
    getattr(cmds, _UNDO_CMD)()


# ──────────────────────────────────────────────────────────────────────────────────────

# -------------------------------------------------------------------------------------#
# --------------------------------------------------------- PRIVATE UTILITY HELPERS ---#


def _as_list(arg: Any) -> list:
    """Convert *arg* to a list. ``None`` becomes ``[]``."""
    if arg is None:
        return []
    if isinstance(arg, str):
        return [arg]
    try:
        return list(arg)
    except TypeError:
        return [arg]


def _filter_nodes(nodes: list[str], types: str | list[str]) -> list[str]:
    """Return only *nodes* whose Maya type inherits from any of *types*."""
    types = _as_list(types)
    result = []
    for node in nodes:
        for typ in types:
            if cmds.objectType(node, isAType=typ):
                result.append(node)
                break
    return result


def _get_nodes(
    nodes: str | list[str] | None = None,
    types: str | list[str] | None = None,
    long: bool = False,
) -> list[str]:
    """Validate and return *nodes*, falling back to the current selection.

    Args:
        nodes: Explicit node names, or ``None`` to use the Maya selection.
        types: Optional type filter (inheritance-based).
        long: If ``True``, return full DAG path names.

    Raises:
        ValueError: If *nodes* is ``None`` and nothing is selected.
    """
    if not nodes:
        nodes = cmds.ls(sl=True, long=long)
        if not nodes:
            raise ValueError(
                "No nodes were provided. "
                "Either pass them explicitly, or select them within Maya."
            )

    if isinstance(nodes, str) and "*" in nodes:
        nodes = cmds.ls(nodes, long=long)

    nodes = _as_list(nodes)

    if types:
        nodes = _filter_nodes(nodes, types)

    return nodes


def _is_joint(node: str) -> bool:
    """Return ``True`` if *node* is a joint."""
    try:
        return cmds.nodeType(node) == "joint"
    except Exception:
        return False


def _is_geometry(node: str) -> bool:
    """Return ``True`` if *node* (transform or shape) is geometry."""
    try:
        if cmds.nodeType(node) in GEOMETRY_TYPES:
            return True
        shapes = cmds.listRelatives(node, shapes=True, path=True) or []
        return any(cmds.nodeType(s) in GEOMETRY_TYPES for s in shapes)
    except Exception:
        return False


def _get_mobject(node: str) -> OpenMaya2.MObject:
    """Return the API 2.0 ``MObject`` for *node*."""
    sel = OpenMaya2.MSelectionList()
    try:
        sel.add(node)
    except RuntimeError:
        raise ValueError(f"No object matches name '{node}'.")
    return sel.getDependNode(0)


def _fq_name_sanitize(name: str) -> str:
    """Replace ``|  :  .  ' '`` with bracket placeholders for file-safe names."""
    return (
        name.replace("|", "[bar]")
        .replace(":", "[cln]")
        .replace(".", "[dot]")
        .replace(" ", "[spc]")
    )


def _fq_name_desanitize(name: str) -> str:
    """Reverse :func:`_fq_name_sanitize`."""
    return (
        name.replace("[bar]", "|")
        .replace("[cln]", ":")
        .replace("[dot]", ".")
        .replace("[spc]", " ")
    )


def _flatten_components_list(components: list[str]) -> None:
    """Expand component range notation in *components* **in-place**.

    ``['node.vtx[3:6]']`` becomes ``['node.vtx[3]', 'node.vtx[4]', ... 'node.vtx[6]']``.
    """
    if not isinstance(components, list):
        raise TypeError("components must be given as a list")
    for i, entry in enumerate(components):
        if "[" in entry and ":" in entry:
            name = entry.split("[")[0]
            range_ = entry.split("[")[1].split("]")[0].split(":")
            if len(range_) < 2:
                continue
            range_ = [int(x) for x in range_]

            sequence = list(range(range_[0], range_[1] + 1))
            sequence.reverse()

            components[i] = f"{name}[{sequence.pop()}]"
            for each in sequence:
                components.insert(i + 1, f"{name}[{each}]")


def _idx_from_component_name(component_name: str) -> int:
    """Extract the integer index from a component name like ``'mesh.vtx[7]'``."""
    try:
        s = component_name.index("[")
        e = component_name.index("]")
        return int(component_name[s + 1 : e])
    except ValueError:
        return -1


def _get_idx_list(component_names: list[str]) -> list[int]:
    """Return a list of vertex indices from component name strings."""
    component_names = _as_list(component_names)
    _flatten_components_list(component_names)
    return [_idx_from_component_name(c) for c in component_names]


def _restore_selection(func):
    """Decorator that restores the Maya selection after *func* executes."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        sel = cmds.ls(sl=True)
        result = func(*args, **kwargs)
        if sel:
            sel = [s for s in sel if cmds.objExists(s)]
            cmds.select(sel, r=True)
        return result

    return wrapper


def _get_group(name: str) -> str:
    """Return *name* if it already exists, otherwise create an empty group."""
    name = str(name)
    if cmds.objExists(name):
        return name
    return cmds.group(n=name, em=True)


# -------------------------------------------------------------------------------------#
# ----------------------------------------------------------- PUBLIC HELPER FUNCTIONS --#


def find_related_deformers(
    nodes: str | list[str] | None = None,
    types: str | list[str] | None = None,
    check_output: bool = False,
) -> list[str]:
    """Find deformers in the immediate history of the given geometry.

    If an item in *nodes* is itself a valid deformer of the requested type it
    will be included in the return list.

    Args:
        nodes: Geometry or deformer names.  ``None`` uses the selection.
        types: Deformer type filter (default ``"geometryFilter"``).
        check_output: Find deformers *driven by* this geometry (wraps, blendShapes).

    Returns:
        list: Matching deformer names.
    """
    if not types:
        types = "geometryFilter"

    nodes = _get_nodes(nodes)

    matches = []
    for node in nodes:
        if _get_nodes([node], types=types):
            matches.append(node)
            continue

        if cmds.objectType(node, isAType="shape"):
            shapes = [node]
        else:
            shapes = cmds.listRelatives(node, shapes=True, path=True)
            if not shapes:
                continue

        for shape in shapes:
            if check_output:
                temp_def = cmds.listConnections(shape, s=0, d=1, scn=True)
                found = _get_nodes(temp_def, types=types) if temp_def else []
                if found:
                    matches.extend(found)
                continue

            history = _get_nodes(
                cmds.listHistory(shape) or [], types=types
            )
            for item in history:
                result = cmds.deformer(item, query=True, geometry=True)
                if result is None:
                    cmds.warning(
                        f"Deformer {item} not associated with any geometry. "
                        "Possible problems with the dependency graph."
                    )
                    continue
                if shape not in result:
                    continue
                matches.append(item)

    return matches


def list_skinned_geo(
    flat: bool = True, full_node_name: bool = False
) -> list[str] | dict[str, str]:
    """Return skinned geometry in the scene.

    Args:
        flat: If ``True`` return a flat list; otherwise a ``{geo: skinCluster}`` dict.
        full_node_name: If ``True`` use full DAG path names.
    """
    if flat:
        rtn: list | dict = []
    else:
        rtn = {}

    scls = cmds.ls(type="skinCluster")

    for sc in scls:
        try:
            geo = cmds.listConnections(
                sc + ".outputGeometry[0]", fullNodeName=full_node_name
            )[0]
        except Exception:
            log.debug("%s has no outputGeometry", sc)
            continue

        if flat:
            rtn.append(geo)
        else:
            rtn[geo] = sc

    return rtn


# -------------------------------------------- Deformer Weights I/O --------------------#


def export_deformer_weights(
    deformer: str,
    path: str | Path | None = None,
    file_name: str | None = None,
    **kwargs: Any,
) -> None:
    """Export deformer weights to a JSON file via ``cmds.deformerWeights``.

    Args:
        deformer: Name of the deformer node.
        path: Directory (or full file path) for the output.  Defaults to
              Maya's user temp directory.
        file_name: Explicit file name.  Defaults to ``<deformer>.json``.
    """
    if not path:
        path = os.path.join(cmds.internalVar(userTmpDir=True), "deformerWeights")

    path = Path(path)

    if "." in path.name:
        file_name = path.name
        path = path.parent

    if not path.exists():
        os.makedirs(path)

    if not file_name:
        file_name = f"{deformer}.json"

    cmds.deformerWeights(
        file_name, ex=True, format="JSON", deformer=deformer, path=path, **kwargs
    )


def import_deformer_weights(
    deformer: str, file_path: str | Path, **kwargs: Any
) -> None:
    """Import deformer weights from a JSON file via ``cmds.deformerWeights``.

    Args:
        deformer: Name of the deformer node.
        file_path: Full path to the ``.json`` weights file.
    """
    file_path = Path(file_path)
    cmds.deformerWeights(
        file_path.name,
        im=True,
        format="JSON",
        deformer=deformer,
        path=file_path.parent,
        **kwargs,
    )


# ----------------------------------------- Weight Data Parsing ------------------------#


def get_deformer_weights_data_from_file(file_path: str | Path) -> dict:
    """Load deformer weights data from a JSON file, repairing if necessary.

    Maya's ``cmds.deformerWeights`` JSON export is known to produce malformed
    output for large meshes (missing commas, truncated brackets).  This
    function attempts automatic repair when the initial parse fails.
    """
    with open(file_path, "r") as fp:
        content = fp.read()

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.warning(
            "Malformed JSON in '%s' (line %d, col %d): %s. Attempting repair...",
            file_path,
            e.lineno,
            e.colno,
            e.msg,
        )
        content = _repair_maya_json(content)
        data = json.loads(content)
        log.info("JSON repair successful for '%s'", file_path)
        return data


def _repair_maya_json(content: str) -> str:
    """Repair common Maya ``deformerWeights`` JSON export issues.

    Handles missing commas between adjacent objects, mismatched closing
    brackets, and truncated files with missing closing brackets.
    """
    content = re.sub(r"\}(\s*)\{", r"},\1{", content)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    chars = list(content)
    stack: list[str] = []
    in_string = False
    escape_next = False

    for i, c in enumerate(chars):
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c in ("{", "["):
            stack.append(c)
        elif c in ("}", "]"):
            if not stack:
                continue
            expected = "}" if stack[-1] == "{" else "]"
            if c == expected:
                stack.pop()
            else:
                chars[i] = expected
                stack.pop()

    content = "".join(chars)

    if stack:
        closings = {"[": "]", "{": "}"}
        content = (
            content.rstrip()
            + "\n"
            + "".join(closings[c] for c in reversed(stack))
            + "\n"
        )

    return content


def _get_weights_list_from_data(data: dict) -> list[dict]:
    """Extract per-influence weights from deformerWeights data.

    Handles both the legacy format and the Maya 2023+ format.
    """
    dw = data.get("deformerWeight", {})

    if "weights" in dw:
        return dw["weights"]

    deformers = dw.get("deformers", [])
    if deformers:
        attrs = deformers[0].get("attributes", [])
        for i, item in enumerate(attrs):
            if item == "weights" and i + 1 < len(attrs):
                return attrs[i + 1]

    return []


def _get_attribute_dicts_from_data(data: dict) -> dict[str, Any]:
    """Extract skinCluster attribute dicts from deformerWeights data."""
    dw = data.get("deformerWeight", {})

    if "attributes" in dw and isinstance(dw["attributes"], dict):
        return dw["attributes"]

    deformers = dw.get("deformers", [])
    if deformers:
        attrs = deformers[0].get("attributes", [])
        result: dict[str, Any] = {}
        for item in attrs:
            if isinstance(item, dict) and "name" in item and "value" in item:
                value = item["value"]
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    with contextlib.suppress(ValueError, TypeError):
                        value = float(value)
                result[item["name"]] = value
        return result

    return {}


def get_influences_from_deformer_weights_data(data: dict) -> list[str]:
    """Return the influence names stored in deformer weights *data*."""
    return [x["source"] for x in _get_weights_list_from_data(data)]


def map_deformer_weights_by_vertex(data: dict) -> dict:
    """Map deformer weights *data* to ``{vertex_index: [(source, value), ...]}``."""
    weight_map: dict = defaultdict(list)
    for weights in _get_weights_list_from_data(data):
        for vtx in weights["points"]:
            weight_map[vtx["index"]].append((weights["source"], vtx["value"]))
    return weight_map


def filter_deformer_weights_by_vertex_ids(
    data: dict, vert_ids: list[int]
) -> dict:
    """Filter a vertex-mapped weights dict to only the given *vert_ids*."""
    return {k: v for k, v in data.items() if k in vert_ids}


def mapped_weights_to_mdouble_array(
    mapped_weights: dict, influences: list[str]
) -> OpenMaya2.MDoubleArray:
    """Convert a mapped-weights dict to an ``MDoubleArray``."""
    array_length = len(mapped_weights) * len(influences)
    result_weights = OpenMaya2.MDoubleArray(array_length, 0.0)
    for idx in sorted(mapped_weights.keys()):
        weights = mapped_weights[idx]
        for influence, value in weights:
            influence_idx = influences.index(influence)
            result_weights[idx * len(influences) + influence_idx] = value
    return result_weights


# ----------------------------------------- Skin Weight Get/Set (API) ------------------#


def get_skin_cluster_influences(skin_cluster: OpenMaya2.MObject) -> list[str]:
    """Return the influence names for *skin_cluster*.

    Args:
        skin_cluster: The skinCluster ``MObject`` (API 2.0).

    Returns:
        list: Influence joint/transform names in index order.
    """
    fn = OpenMayaAnim2.MFnSkinCluster(skin_cluster)
    influence_objs = fn.influenceObjects()
    return [
        OpenMaya2.MFnDagNode(influence_objs[i]).name()
        for i in range(len(influence_objs))
    ]


def get_skin_weights_from_selected_components(
    skin_cluster: OpenMaya2.MObject,
) -> tuple:
    """Return skin weights for the currently selected vertex components.

    Args:
        skin_cluster: The skinCluster ``MObject`` (API 2.0).

    Returns:
        tuple: ``(weights, inf_num, soft_selection_weights)``

    Raises:
        ValueError: If no components are selected.
    """
    fn = OpenMayaAnim2.MFnSkinCluster(skin_cluster)

    selection = OpenMaya2.MGlobal.getRichSelection().getSelection()

    if selection.length() == 0:
        raise ValueError(
            "No components found in selection. "
            "Please select vertices before using this tool."
        )

    dag_path, components = selection.getComponent(0)
    fn_comp = OpenMaya2.MFnSingleIndexedComponent(components)

    soft_selection_weights: list[float] = []
    if fn_comp.hasWeights:
        selected_idxs = fn_comp.getElements()
        for i in range(len(selected_idxs)):
            soft_selection_weights.append(fn_comp.weight(i).influence)

    weights, inf_num = fn.getWeights(dag_path, components)
    return weights, inf_num, soft_selection_weights


def set_mesh_skin_weights(
    skin_cluster: OpenMaya2.MObject,
    weights: OpenMaya2.MDoubleArray,
    influences: OpenMaya2.MIntArray,
) -> None:
    """Set skin weights for an entire mesh via the API.

    Args:
        skin_cluster: The skinCluster ``MObject``.
        weights: Flat weight array (verts * influences).
        influences: Influence index array.
    """
    fn = OpenMayaAnim2.MFnSkinCluster(skin_cluster)
    shape_dag = fn.getPathAtIndex(0)
    fn.setWeights(shape_dag, OpenMaya2.MObject(), influences, weights, True, False)


def set_skin_weights_to_selected_components(
    skin_cluster: OpenMaya2.MObject,
    weights: OpenMaya2.MDoubleArray,
    influences: OpenMaya2.MIntArray,
) -> None:
    """Set skin weights on the currently selected components via the API."""
    fn = OpenMayaAnim2.MFnSkinCluster(skin_cluster)
    selection = OpenMaya2.MGlobal.getRichSelection().getSelection()
    dag_path, components = selection.getComponent(0)
    fn.setWeights(dag_path, components, influences, weights, True, False)


def set_skin_weights_from_weights_data(
    skin_cluster_name: str,
    weights_data: dict,
    influence_names: list[str],
    vert_ids: list[int] | None = None,
) -> None:
    """Set skin weights on *skin_cluster_name* from parsed weights data.

    Args:
        skin_cluster_name: Name of the skinCluster node.
        weights_data: Parsed deformer-weights dict (from file or in-memory).
        influence_names: Ordered list of influence names.
        vert_ids: Optional subset of vertex IDs to apply to (uses
                  component selection when provided).
    """
    wm = map_deformer_weights_by_vertex(weights_data)

    if vert_ids:
        wm = filter_deformer_weights_by_vertex_ids(wm, vert_ids)
        re_indexed_wm = {}
        for i, idx in enumerate(sorted(wm.keys())):
            re_indexed_wm[i] = wm[idx]
        wm = re_indexed_wm

    weights = mapped_weights_to_mdouble_array(wm, influence_names)
    sc = _get_mobject(skin_cluster_name)
    influences = OpenMaya2.MIntArray(list(range(len(influence_names))))

    if vert_ids:
        set_skin_weights_to_selected_components(sc, weights, influences)
    else:
        set_mesh_skin_weights(sc, weights, influences)


# -------------------------------------------------------------------------------------#
# -------------------------------------------------------------- PUBLIC API FUNCTIONS --#


def add_skin_cluster(
    geo: str | list[str] | None = None,
    joints: list[str] | None = None,
    front_of_chain: bool = False,
    name: str | None = None,
) -> list[str]:
    """Create a skinCluster on *geo* driven by *joints*.

    Works even if *geo* already has an existing skinCluster.

    Args:
        geo: Geometry to skin. ``None`` uses selected geometry.
        joints: Joints to bind. ``None`` uses selected joints.
        front_of_chain: Place the new skinCluster at the front of the
            deformer stack.
        name: Optional skinCluster name.

    Returns:
        list: Names of the newly created skinCluster(s).
    """
    selection = cmds.ls(sl=True)

    if geo is None:
        geo = [
            x
            for x in selection
            if not _is_joint(x) and _is_geometry(x)
        ]
        if not geo:
            raise ValueError("Must specify geometry to be skinned.")

    if joints is None:
        joints = cmds.ls(selection, type="joint")

    if not joints:
        raise ValueError("Must specify joints to be skinned to.")

    joints = sorted(set(joints), key=joints.index)
    new_scls: list[str] = []
    geo = _as_list(geo)

    for g in geo:
        (scls,) = cmds.deformer(g, type="skinCluster", frontOfChain=front_of_chain)
        geom_matrix = cmds.xform(g, query=True, worldSpace=True, matrix=True)

        cmds.setAttr(f"{scls}.geomMatrix", *geom_matrix, type="matrix")

        for i, jnt in enumerate(joints):
            bind = cmds.getAttr(f"{jnt}.worldInverseMatrix")

            if not cmds.attributeQuery("lockInfluenceWeights", node=jnt, exists=True):
                cmds.addAttr(jnt, sn="liw", ln="lockInfluenceWeights", at="bool")

            cmds.connectAttr(f"{jnt}.lockInfluenceWeights", f"{scls}.lockWeights[{i}]")
            cmds.connectAttr(f"{jnt}.objectColorRGB", f"{scls}.influenceColor[{i}]")
            cmds.connectAttr(f"{jnt}.worldMatrix", f"{scls}.matrix[{i}]")
            cmds.setAttr(f"{scls}.bindPreMatrix[{i}]", *bind, type="matrix")

        cmds.skinPercent(scls, g, tv=[joints[0], 1.0], zri=True)
        cmds.skinCluster(scls, edit=True, recacheBindMatrices=True)

        scls = cmds.rename(scls, name or "{}_SCLS".format(g.rsplit("|", 1)[-1]))
        new_scls.append(scls)

    return new_scls


def delete_skin_clusters(nodes: str | list[str] | None = None) -> None:
    """Delete all skinClusters on the given *nodes*.

    Args:
        nodes: Node names. ``None`` uses the selection.
    """
    nodes = _get_nodes(nodes)

    for node in nodes:
        deformers = find_related_deformers(node, "skinCluster")
        for deformer in deformers:
            cmds.delete(deformer)


def rebuild_skin_cluster(nodes: str | list[str] | None = None) -> None:
    """Rebuild the skinClusters on the given *nodes*.

    Exports current weights, deletes the skinCluster, then re-imports.

    Args:
        nodes: Node names. ``None`` uses the selection.
    """
    nodes = _get_nodes(nodes)

    export_skin_weights(nodes, force=True)
    import_skin_weights(nodes, rebuild=True)


def export_skin_weights(
    nodes: str | list[str] | None = None,
    path: str | Path | None = None,
    **kwargs: Any,
) -> dict:
    """Export skinCluster weights to disk.

    Args:
        nodes: Skinned geometry or skinCluster names. ``None`` exports all
               skinClusters in the scene.
        path: Directory or explicit file path for the output. Defaults to
              Maya's user temp directory.

    Keyword Args:
        force (bool): Overwrite existing files. Default ``False``.
        strict (bool): Raise on incompatible skinClusters. Default ``False``.
        create_path (bool): Create missing directories. Default ``False``.
        fq_named (bool): Use fully-qualified names for files. Default ``False``.

    Returns:
        dict: Per-node export information.
    """
    force = kwargs.get("force", False)
    strict = kwargs.get("strict", False)
    create_path = kwargs.get("create_path", False)
    fq_named = kwargs.get("fq_named", False)

    if strict:
        force = False

    try:
        nodes = _get_nodes(nodes, long=fq_named)
    except ValueError as e:
        if "No nodes" in str(e):
            nodes = None
        else:
            raise

    if not nodes:
        nodes = list_skinned_geo(full_node_name=fq_named)

    file_path = None
    if path:
        path = Path(path)
        if path.is_file():
            if len(nodes) > 1:
                raise ValueError(
                    "Multiple nodes given, cannot be written to a single "
                    "file. Specify a directory instead and the files will "
                    f"be auto named: {path}"
                )
            file_path = path
        else:
            if not path.exists() and create_path:
                os.makedirs(path)
            else:
                raise FileNotFoundError(f"Given path does not exist: {path}")
    else:
        path = Path(
            os.path.join(cmds.internalVar(userTmpDir=True), "deformerWeights")
        )
        if not path.exists():
            os.makedirs(path)

    data: dict = {}
    for node in nodes:
        name = node
        if fq_named:
            name = _fq_name_sanitize(node)

        file_name = name + ".weights"
        time_start = time.time()
        data[node] = {}
        skin_clusters = find_related_deformers(node, "skinCluster")

        if not skin_clusters:
            msg = f"No skinCluster found on: {node}"
            if strict:
                raise RuntimeError(msg)
            else:
                log.warning(msg)
                data[node]["failed"] = RuntimeError(msg)
                continue

        if file_path is None:
            file_path = path / file_name

        space = node
        if cmds.nodeType(space) == "mesh":
            space = cmds.listRelatives(node, p=True, pa=True)[0]

        for i, sc in enumerate(skin_clusters):
            if i > 0:
                file_path = Path(str(file_path) + str(i))
                data[node]["skinCluster" + str(i)] = sc
            else:
                data[node]["skinCluster"] = sc

            if not force and file_path.exists():
                msg = (
                    f"{file_path} already exists. call again with "
                    "force=True to ignore check and overwrite file"
                )
                if strict:
                    raise OSError(msg)
                else:
                    log.warning(msg)
                    data[node][sc] = "FAILED: " + msg
                    data[node]["failed"] = OSError(msg)
                    continue

            attributes = [
                "envelope",
                "skinningMethod",
                "normalizeWeights",
                "dropoffRate",
                "maxInfluences",
                "lockWeights",
            ]
            export_deformer_weights(sc, file_path, at=attributes)

            log.info("weights written to: %s", file_path)
            data[node][sc] = file_path

        file_path = None

        elapsed_time = time.time() - time_start
        data[node]["elapsedTime"] = elapsed_time

    return data


@_restore_selection
def import_skin_weights(
    nodes: str | list[str] | None = None,
    path: str | Path | None = None,
    **kwargs: Any,
) -> dict | list[str]:
    """Import weights from a skinCluster weights file.

    Args:
        nodes: Geometry, skinCluster names, or component selections.
               ``None`` attempts to resolve nodes from weight file names.
        path: File or directory path for weights. When a directory is given,
              node names are matched to file names.

    Keyword Args:
        method (str): ``'index'``, ``'position'``, or ``'barycentric'``.
            Default ``'index'``.
        rebuild (bool): Delete existing skinCluster and rebuild. Default ``False``.
        force (bool): Force rebuild when multiple skinClusters exist.
            Default ``False``.
        strict (bool): Raise on incompatible skinClusters. Default ``False``.
        catch_weights (bool): Create placeholder joints for missing
            influences. Default ``False``.
        flat (bool): Return a flat list of skinCluster names instead of
            a rich dict. Default ``False``.
        fq_named (bool): Use fully-qualified names. Default ``False``.
        set_attributes (bool): Restore skinCluster attributes from the
            weight file. Default ``True``.

    Returns:
        dict or list: Per-node import information, or a flat skinCluster list.
    """
    method = kwargs.get("method", "index")
    rebuild = kwargs.get("rebuild", False)
    force = kwargs.get("force", False)
    strict = kwargs.get("strict", False)
    catch_weights = kwargs.get("catch_weights", False)
    flat = kwargs.get("flat", False)
    fq_named = kwargs.get("fq_named", False)
    set_attributes = kwargs.get("set_attributes", True)

    if strict:
        force = False

    explicit = False

    try:
        nodes = _get_nodes(nodes, long=fq_named)
    except ValueError as e:
        if "No nodes" in str(e):
            nodes = None
        else:
            raise

    # validate path
    if not path:
        path = os.path.join(cmds.internalVar(userTmpDir=True), "deformerWeights")

    path = Path(path)

    if path.is_file():
        weight_files = [path]
        explicit = True
    else:
        if not path.exists():
            raise FileNotFoundError(f"Given path does not exist: {path}")
        weight_files = list(path.glob("*.weights"))

    if not nodes:
        nodes = []
        for wf in weight_files:
            name = _fq_name_desanitize(wf.stem)
            if cmds.objExists(name):
                nodes.append(name)

        if not nodes:
            raise ValueError(
                "No nodes given, no objects selected, "
                "no available nodes parsed from weight files."
            )

    data: dict = {}
    weight_files_map = {}
    for wf in weight_files:
        weight_files_map[_fq_name_desanitize(wf.stem)] = wf

    infs_from_file = None

    if "[" in nodes[0]:  # component selection
        node = nodes[0].split(".")[0]
        time_start = time.time()
        data[node] = {}

        weight_file = None
        if not explicit:
            if node not in weight_files_map:
                raise LookupError(f"No .weight file found for: {node}")
            weight_file = weight_files_map[node]
        else:
            weight_file = weight_files[0]

        wgt_data = get_deformer_weights_data_from_file(weight_file)

        skin_clusters = find_related_deformers(node, "skinCluster")
        infs_from_file = get_influences_from_deformer_weights_data(wgt_data)

        bad_influences = [inf for inf in infs_from_file if not cmds.objExists(inf)]

        if bad_influences:
            if catch_weights:
                grp = _get_group("weightCatchers_GRP")
                for inf in bad_influences:
                    cmds.select(cl=True)
                    new_jnt = cmds.joint(n=inf)
                    cmds.parent(new_jnt, grp)
            else:
                raise ValueError(
                    f"Influence objects: {bad_influences} from .weights file: "
                    f"{weight_file} do not exist in scene. Unable to import "
                    f"weights on: {node}"
                )

        missing_infs: list[str] = []
        if rebuild:
            cmds.delete(skin_clusters)
            skin_clusters = None

        if skin_clusters and len(skin_clusters) > 1:
            if force:
                cmds.delete(skin_clusters)
                skin_clusters = None
            else:
                raise IndexError(
                    f"Unable to handle multiple skinClusters on: {node}"
                )

        if not skin_clusters:
            cmds.select(infs_from_file, r=True)
            cmds.select(node, add=True)
            name_sc = node + "_SCLS"
            skin_clusters = cmds.skinCluster(
                tsb=True, mi=2, omi=False, dr=4, rui=False, nw=2, name=name_sc
            )
            cmds.select(cl=True)

        elif len(skin_clusters) == 1:
            crnt_infs = cmds.skinCluster(skin_clusters[0], q=True, inf=True)
            missing_infs = list(set(infs_from_file) - set(crnt_infs))

            if missing_infs:
                if strict:
                    raise IndexError(
                        f"Influences from weight file: {weight_file} do not match "
                        f"influence list for the skinCluster on {node}. "
                        f"Missing Influences: {missing_infs}"
                    )
                cmds.skinCluster(
                    skin_clusters[0], e=True, ai=missing_infs, lw=True, wt=0
                )
                cmds.setAttr(skin_clusters[0] + ".maintainMaxInfluences", 0)
                cmds.skinCluster(skin_clusters[0], e=True, lw=False)

        cmds.select(nodes, r=True)

        _flatten_components_list(nodes)
        vert_ids = _get_idx_list(nodes)
        set_skin_weights_from_weights_data(
            skin_clusters[0], wgt_data, infs_from_file, vert_ids
        )

        elapsed_time = time.time() - time_start

        data[node]["skinCluster"] = skin_clusters[0]
        data[node]["weightFile"] = weight_file
        data[node]["influenceList"] = infs_from_file
        data[node]["elapsedTime"] = elapsed_time
        if rebuild:
            data[node]["rebuilt"] = True
        if bad_influences:
            data[node]["createdInfluences"] = bad_influences
        if missing_infs:
            data[node]["addedInfluences"] = missing_infs

        if flat:
            data = [v["skinCluster"] for _, v in data.items() if "skinCluster" in v]

    else:
        for node in nodes:
            time_start = time.time()
            data[node] = {}

            weight_file = None
            if not explicit:
                infs_from_file = None
                if node not in weight_files_map:
                    msg = f"No .weight file found for: {node}"
                    if strict:
                        raise LookupError(msg)
                    else:
                        log.warning(msg)
                        data[node]["failed"] = msg
                        continue
                weight_file = weight_files_map[node]
            else:
                weight_file = weight_files[0]

            wgt_data = get_deformer_weights_data_from_file(weight_file)

            skin_clusters = find_related_deformers(node, "skinCluster")

            if infs_from_file is None:
                infs_from_file = get_influences_from_deformer_weights_data(wgt_data)

            if not infs_from_file:
                msg = (
                    f"Influence not found in weights file: {weight_file}. "
                    f"Unable to import weights on: {node}"
                )
                if strict:
                    raise LookupError(msg)
                else:
                    log.warning(msg)
                    data[node]["failed"] = msg
                    continue

            bad_influences = [
                inf for inf in infs_from_file if not cmds.objExists(inf)
            ]

            if bad_influences:
                if catch_weights:
                    grp = _get_group("weightCatchers_GRP")
                    for inf in bad_influences:
                        cmds.select(cl=True)
                        new_jnt = cmds.joint(n=inf)
                        cmds.parent(new_jnt, grp)
                else:
                    msg = (
                        f"Influence objects: {bad_influences} from .weights "
                        f"file: {weight_file} do not exist in scene. Unable "
                        f"to import weights on: {node}"
                    )
                    if strict:
                        raise LookupError(msg)
                    else:
                        log.warning(msg)
                        data[node]["failed"] = msg
                        continue

            missing_infs: list[str] = []
            if rebuild:
                cmds.delete(skin_clusters)
                skin_clusters = None

            if skin_clusters and len(skin_clusters) > 1:
                if force:
                    cmds.delete(skin_clusters)
                    skin_clusters = None
                elif strict:
                    raise IndexError(
                        f"Unable to handle multiple skinClusters on: {node}"
                    )
                else:
                    log.warning(
                        "Unable to handle multiple skinClusters on: %s "
                        "skipping weights import on %s",
                        node,
                        node,
                    )

            if not skin_clusters:
                try:
                    cmds.select(infs_from_file, r=True)
                    cmds.select(node, add=True)

                    name_sc = node + "_SCLS"
                    if "|" in name_sc:
                        name_sc = name_sc.split("|")[-1]

                    skin_clusters = cmds.skinCluster(
                        tsb=True, mi=2, omi=False, dr=4, rui=False, nw=2
                    )
                    skin_clusters = [cmds.rename(skin_clusters, name_sc)]

                except Exception as e:
                    if strict:
                        raise e
                    else:
                        log.warning("Unable to create skinCluster on: %s", node)
                        continue

                cmds.select(cl=True)

            elif len(skin_clusters) == 1:
                crnt_infs = cmds.skinCluster(skin_clusters[0], q=True, inf=True)
                missing_infs = list(set(infs_from_file) - set(crnt_infs))

                if missing_infs:
                    if strict:
                        raise ValueError(
                            f"Influences from weight file: {weight_file} do not "
                            f"match influence list for the skinCluster on {node}. "
                            f"Missing Influences: {missing_infs}"
                        )
                    cmds.skinCluster(
                        skin_clusters[0], e=True, ai=missing_infs, lw=True, wt=0
                    )
                    cmds.setAttr(skin_clusters[0] + ".maintainMaxInfluences", 0)
                    cmds.skinCluster(skin_clusters[0], e=True, lw=False)

            if set_attributes:
                attributes = _get_attribute_dicts_from_data(wgt_data)
                for attr, value in attributes.items():
                    if cmds.attributeQuery(attr, node=skin_clusters[0], exists=True):
                        try:
                            cmds.setAttr(f"{skin_clusters[0]}.{attr}", value)
                        except Exception:
                            log.warning(
                                "Could not set attribute %s on %s to %s",
                                attr,
                                skin_clusters[0],
                                value,
                            )
                    else:
                        log.debug(
                            "Attribute %s does not exist on %s",
                            attr,
                            skin_clusters[0],
                        )

            if method == "index":
                set_skin_weights_from_weights_data(
                    skin_clusters[0], wgt_data, infs_from_file
                )
            else:
                import_deformer_weights(skin_clusters[0], weight_file, method=method)

            elapsed_time = time.time() - time_start

            data[node]["skinCluster"] = skin_clusters[0]
            data[node]["weightFile"] = weight_file
            data[node]["influenceList"] = infs_from_file
            data[node]["elapsedTime"] = elapsed_time
            if rebuild:
                data[node]["rebuilt"] = True
            if bad_influences:
                data[node]["createdInfluences"] = bad_influences
            if missing_infs:
                data[node]["addedInfluences"] = missing_infs

            if flat:
                data = [
                    v["skinCluster"] for _, v in data.items() if "skinCluster" in v
                ]

    return data


@_restore_selection
def match_influences_on_skin_clusters(
    nodes: str | list[str] | None = None, **kwargs: Any
) -> dict:
    """Update skinClusters on *nodes* so their influences are congruent.

    Collects a master influence list from all provided skinClusters and adds
    any missing influences to each one.

    Args:
        nodes: Geometry or skinCluster names. ``None`` uses the selection.

    Keyword Args:
        rebuild (bool): Rebuild skinClusters after matching. Default ``False``.
        force (bool): Create skinClusters on nodes that lack one. Default ``False``.
        strict (bool): Raise on missing skinClusters. Default ``False``.

    Returns:
        dict: Per-node information about added influences.
    """
    nodes = _get_nodes(nodes)
    rebuild = kwargs.get("rebuild", False)
    force = kwargs.get("force", False)
    strict = kwargs.get("strict", False)

    data: dict = {}

    # filter out components, keeping only node names
    nodes = list(dict.fromkeys([x.split(".")[0] for x in nodes]))

    skin_map: dict[str, list[str]] = {}
    all_influences: list[str] = []
    skipped_nodes: set[str] = set()

    for node in nodes:
        cmds.select(node, r=True)
        data[node] = {}
        skin_clusters = find_related_deformers(node, "skinCluster")

        if not skin_clusters:
            if force:
                name_sc = node + "_SCLS"
                cmds.select(all_influences, add=True)
                skin_clusters = cmds.skinCluster(
                    tsb=True, mi=2, omi=0, dr=4, rui=False, nw=2, name=name_sc
                )
            else:
                msg = f"No skinCluster found on: {node}"
                if strict:
                    raise ValueError(msg)
                else:
                    log.warning(msg)
                    data[node]["failed"] = ValueError(msg)
                    skipped_nodes.add(node)
                    continue

        elif len(skin_clusters) > 1:
            if force:
                cmds.delete(skin_clusters)
                name_sc = node + "_SCLS"
                cmds.select(all_influences, add=True)
                skin_clusters = cmds.skinCluster(
                    tsb=True, mi=2, omi=0, dr=4, rui=False, nw=2, name=name_sc
                )
            else:
                msg = f"Won't deal with multiple skinClusters found on: {node}"
                if strict:
                    raise IndexError(msg)
                else:
                    log.warning(msg)
                    data[node]["failed"] = IndexError(msg)
                    skipped_nodes.add(node)
                    continue
        else:
            infs = cmds.skinCluster(skin_clusters[0], q=True, inf=True)
            for inf in infs:
                if inf not in all_influences:
                    all_influences.append(inf)

        skin_map[node] = skin_clusters

    data["masterInfluenceList"] = all_influences
    all_influences_set = set(all_influences)

    for node in nodes:
        if node in skipped_nodes:
            continue

        skin_clusters = skin_map[node]
        infs = cmds.skinCluster(skin_clusters[0], q=True, inf=True)
        missing_infs = list(all_influences_set - set(infs))

        if missing_infs:
            cmds.skinCluster(skin_clusters[0], e=True, ai=missing_infs, lw=True, wt=0)
            cmds.setAttr(skin_clusters[0] + ".maintainMaxInfluences", 0)

            if rebuild:
                tmp_path = cmds.internalVar(userTmpDir=True)
                export_skin_weights(node, tmp_path, force=True)
                import_skin_weights(node, tmp_path, rebuild=True)
        else:
            log.info("%s already has all matching influences.", node)

        data[node]["addedInfluences"] = missing_infs
        data[node]["skinClusters"] = skin_clusters

    return data


@_restore_selection
def average_vert_skin_weights_with_neighbors(
    vert_component_names: list[str] | None = None,
) -> None:
    """Replace selected vertex weights with the average of their neighbors.

    Useful for smoothing out weight painting or creating more natural
    weight distributions.

    Args:
        vert_component_names: Vertex component names (e.g.
            ``['pSphere1.vtx[10:20]']``).  ``None`` uses the selection.

    Raises:
        ValueError: If no vertices are selected, no skinCluster is found,
            or multiple skinClusters exist on the object.

    Example::

        cmds.select('pSphere1.vtx[10:20]')
        average_vert_skin_weights_with_neighbors()
    """
    nodes = _get_nodes(vert_component_names)
    if "[" not in nodes[0]:
        raise ValueError("must have vertices selected")

    sel_verts = cmds.ls(nodes, flatten=True)
    sel_node = nodes[0].split(".")[0]

    sc = find_related_deformers(sel_node, "skinCluster")

    if not sc:
        raise ValueError(f"No skinCluster found on selected object: {sel_node}")
    if len(sc) > 1:
        raise ValueError("More than one skinCluster found on selected")

    skin_cluster_m_object = _get_mobject(sc[0])
    fn_skin_cluster = OpenMayaAnim2.MFnSkinCluster(skin_cluster_m_object)
    shape_dag_path = fn_skin_cluster.getPathAtIndex(0)

    cmds.select(sel_verts, r=True)
    selection = OpenMaya2.MGlobal.getActiveSelectionList()
    dag_path, components = selection.getComponent(0)

    get_skin_cluster_influences(skin_cluster_m_object)

    start_weights, inf_num = fn_skin_cluster.getWeights(dag_path, components)
    averaged_weights = OpenMaya2.MDoubleArray(start_weights)

    mit_vert = OpenMaya2.MItMeshVertex(shape_dag_path, components)

    fn_comp = OpenMaya2.MFnSingleIndexedComponent(components)
    fn_comp.getElements()

    vertex_index = 0
    while not mit_vert.isDone():
        neighbor_verts = mit_vert.getConnectedVertices()

        if len(neighbor_verts) > 0:
            fn_neighbor_comp = OpenMaya2.MFnSingleIndexedComponent()
            neighbor_components = fn_neighbor_comp.create(
                OpenMaya2.MFn.kMeshVertComponent
            )
            fn_neighbor_comp.addElements(neighbor_verts)

            neighboring_vert_weights, inf_num = fn_skin_cluster.getWeights(
                dag_path, neighbor_components
            )

            average_neighbor_weights = [0.0] * inf_num
            num_neighbors = len(neighbor_verts)

            for i in range(len(neighboring_vert_weights)):
                average_neighbor_weights[i % inf_num] += neighboring_vert_weights[i]

            for i in range(inf_num):
                average_neighbor_weights[i] /= num_neighbors

            weight_sum = sum(average_neighbor_weights)
            if weight_sum > 0.0:
                for i in range(inf_num):
                    average_neighbor_weights[i] /= weight_sum

            start_idx = vertex_index * inf_num
            for i in range(inf_num):
                averaged_weights[start_idx + i] = average_neighbor_weights[i]

        mit_vert.next()
        vertex_index += 1

    influences = OpenMaya2.MIntArray(list(range(inf_num)))

    normalize_mode = cmds.getAttr(f"{sc[0]}.normalizeWeights")
    cmds.setAttr(f"{sc[0]}.normalizeWeights", 0)

    original_weights = OpenMaya2.MDoubleArray(start_weights)

    try:
        fn_skin_cluster.setWeights(
            dag_path, components, influences, averaged_weights, True, False
        )

        def undo_operation():
            cmds.setAttr(f"{sc[0]}.normalizeWeights", 0)
            fn_skin_cluster.setWeights(
                dag_path, components, influences, original_weights, True, False
            )
            cmds.setAttr(f"{sc[0]}.normalizeWeights", normalize_mode)

        def redo_operation():
            cmds.setAttr(f"{sc[0]}.normalizeWeights", 0)
            fn_skin_cluster.setWeights(
                dag_path, components, influences, averaged_weights, True, False
            )
            cmds.setAttr(f"{sc[0]}.normalizeWeights", normalize_mode)

        _undo_commit(undo_operation, redo_operation)

    finally:
        cmds.setAttr(f"{sc[0]}.normalizeWeights", normalize_mode)


def copy_skin_cluster(
    nodes: list[str] | None = None,
    rebuild: bool = False,
    **kwargs: Any,
) -> None:
    """Copy the skinCluster (and weights) from the first node to all others.

    Supports component selections.

    Args:
        nodes: List of nodes — the first is the source, the rest are targets.
        rebuild: Force a rebuild of the skinCluster on each target.

    Keyword Args:
        Passed through to ``match_influences_on_skin_clusters``.
    """
    nodes = _get_nodes(nodes)
    if len(nodes) < 2:
        raise ValueError("Must pass 2 or more nodes")

    match_influences_on_skin_clusters(nodes, force=True)

    source = nodes.pop(0)
    targets = nodes

    for tgt in targets:
        cmds.select([source, tgt], r=True)
        cmds.copySkinWeights(
            noMirror=True,
            surfaceAssociation="closestPoint",
            influenceAssociation="closestJoint",
        )

        if rebuild:
            tmp_path = cmds.internalVar(userTmpDir=True)
            export_skin_weights(tgt, tmp_path, force=True)
            import_skin_weights(tgt, tmp_path, rebuild=True)


@_restore_selection
def mirror_skin_weights(nodes: list[str] | None = None, **kwargs: Any) -> None:
    """Mirror skin weights on each node's skinCluster.

    Args:
        nodes: Geometry names. ``None`` uses the selection.

    Keyword Args:
        mirror_mode (str): ``'YZ'``, ``'XY'``, or ``'XZ'``. Default ``'YZ'``.
        surface_association (str): Default ``'closestPoint'``.
        influence_association (str): Default ``'closestJoint'``.
        mirror_inverse (bool): Default ``False``.
    """
    mirror_mode = kwargs.get("mirror_mode", "YZ")
    surface_association = kwargs.get("surface_association", "closestPoint")
    influence_association = kwargs.get("influence_association", "closestJoint")
    mirror_inverse = kwargs.get("mirror_inverse", False)

    nodes = _get_nodes(nodes)
    if not nodes:
        raise ValueError("No nodes provided")
    if "[" in nodes[0]:
        nodes = [nodes[0].split(".")[0]]

    for node in nodes:
        sc = find_related_deformers(node, "skinCluster")
        if len(sc) != 1:
            raise ValueError(f"Expected 1 skinCluster on {node}, found {len(sc)}")
        cmds.copySkinWeights(
            ss=sc[0],
            ds=sc[0],
            mirrorMode=mirror_mode,
            surfaceAssociation=surface_association,
            influenceAssociation=influence_association,
            mirrorInverse=mirror_inverse,
        )


def mirror_skin_clusters(nodes: list[str] | None = None, **kwargs: Any) -> None:
    """Mirror skinClusters to the opposite-side geometry.

    Replaces ``L_`` / ``R_`` prefixes in node and influence names.

    Args:
        nodes: Source geometry. ``None`` uses all skinned geometry.

    Keyword Args:
        direction (str): ``'LtoR'`` or ``'RtoL'``. Default ``'LtoR'``.
        force (bool): Overwrite existing skinClusters on the target side.
            Default ``False``.
    """
    nodes = _get_nodes(nodes)
    force = kwargs.get("force", False)
    direction = kwargs.get("direction", "LtoR")

    if direction.lower() == "ltor":
        side_a = "L_"
        side_b = "R_"
    else:
        side_a = "R_"
        side_b = "L_"

    if not nodes:
        nodes = list_skinned_geo()

    nodes = [node for node in nodes if side_a in node]

    for node in nodes:
        mirror_node = node.replace(side_a, side_b)
        side_a_skin_clusters = find_related_deformers(node, "skinCluster")
        side_b_skin_clusters = find_related_deformers(mirror_node, "skinCluster")

        if side_b_skin_clusters:
            if not force:
                log.warning("Skipping: %s. Geo has existing skinClusters.", mirror_node)
                continue
            cmds.delete(side_b_skin_clusters)

        for sc in side_a_skin_clusters:
            influences = cmds.skinCluster(sc, q=True, inf=True)
            mirror_influences = [inf.replace(side_a, side_b) for inf in influences]

            try:
                cmds.select(mirror_node, r=True)
                cmds.select(mirror_influences, add=True)
                name = mirror_node + "_SCLS"
                mel.eval(
                    'newSkinCluster "-toSelectedBones '
                    "-mi 2 -omi 0 -dr 4 -rui false "
                    f'-n {name}"'
                )
            except Exception:
                log.exception(
                    "Failed to create mirror skinCluster on '%s'", mirror_node
                )
                continue

        try:
            cmds.select([node, mirror_node], r=True)
            cmds.MirrorSkinWeights()
        except Exception:
            log.exception(
                "Failed to mirror skin weights from '%s' to '%s'", node, mirror_node
            )
            continue


def set_all_skin_clusters_normalize_weight_mode(mode: int) -> None:
    """Set the normalize-weights mode on every skinCluster in the scene.

    Args:
        mode: ``0`` = None, ``1`` = Interactive, ``2`` = Post.
    """
    for sc in cmds.ls(type="skinCluster"):
        cmds.setAttr(f"{sc}.normalizeWeights", mode)


# -------------------------------------------------------------------------------------#
# -------------------------------------------------------------------------- CLASSES --#


class DraggerContext:
    """Base class for interactive dragger-context tools in Maya.

    Wraps ``cmds.draggerContext`` and provides press/drag/release hooks with
    automatic undo-chunk management.
    """

    CTX_NAME = "myDraggerCtx"

    def __init__(
        self,
        name: str = "myDraggerCTX",
        title: str = "Dragger",
        default_value: float = 0,
        min_value: float | None = None,
        max_value: float | None = None,
        multiplier: float = 0.01,
        cursor: str = "crossHair",
        space: str = "screen",
    ):
        self.button: int | None = None
        self.modifier: str | None = None

        self.multiplier = multiplier
        self.default_value = default_value
        self.min_value = min_value
        self.max_value = max_value
        self.space = space

        self.x: float = 0.0
        self.y: float = 0.0
        self.anchor_point: tuple[float, float, float] = (0.0, 0.0, 0.0)

        self.CTX_NAME = name

        if cmds.draggerContext(self.CTX_NAME, exists=True):
            cmds.deleteUI(self.CTX_NAME)

        self.CTX_NAME = cmds.draggerContext(
            self.CTX_NAME,
            pressCommand=self._on_press,
            dragCommand=self._on_drag,
            releaseCommand=self._on_release,
            cursor=cursor,
            drawString=title,
            undoMode="all",
            space=self.space,
        )

    def _on_press(self):
        self.anchor_point = cmds.draggerContext(
            self.CTX_NAME, query=True, anchorPoint=True
        )
        self.button = cmds.draggerContext(self.CTX_NAME, query=True, button=True)
        self.modifier = cmds.draggerContext(self.CTX_NAME, query=True, modifier=True)
        cmds.undoInfo(openChunk=True)
        self.on_press()

    def on_press(self):
        pass

    def _on_drag(self):
        drag_point = cmds.draggerContext(self.CTX_NAME, query=True, dragPoint=True)

        self.x = (
            (drag_point[0] - self.anchor_point[0]) * self.multiplier
        ) + self.default_value
        self.y = (
            (drag_point[1] - self.anchor_point[1]) * self.multiplier
        ) + self.default_value

        if self.min_value is not None and self.x < self.min_value:
            self.x = self.min_value
        if self.max_value is not None and self.x > self.max_value:
            self.x = self.max_value
        if self.min_value is not None and self.y < self.min_value:
            self.y = self.min_value
        if self.max_value is not None and self.y > self.max_value:
            self.y = self.max_value

        self.on_drag()
        cmds.refresh()

    def on_drag(self):
        pass

    def _on_release(self):
        self.on_release()
        cmds.undoInfo(closeChunk=True)

    def on_release(self):
        pass

    def draw_string(self, message: str) -> None:
        """Display *message* at the pointer position in the viewport."""
        cmds.draggerContext(self.CTX_NAME, edit=True, drawString=message)

    def set_tool(self) -> None:
        """Activate this dragger context as the current Maya tool."""
        cmds.setToolTo(self.CTX_NAME)


class SlideVertexWeightsTool(DraggerContext):
    """Interactive tool for sliding vertex weights between influences.

    Drag left/right to slide weights toward/away from the closest joint
    influence to the cursor.  Supports soft selection for gradual falloff.

    - **Ctrl** drag for fine control (0.1x)
    - **Shift** drag for coarse control (10x)

    Args:
        nodes: Vertex components to operate on.  ``None`` uses the selection.
        mapped_joints: ``{joint_name: [x, y, z]}`` world positions.
            Auto-calculated if ``None``.
        increment: Base multiplier for weight changes per drag pixel.
        near_inf_threshold: Distance threshold for detecting ambiguous
            (multiple nearby) influences.

    Raises:
        AssertionError: If no vertices are selected or no skinCluster found.
        ValueError: If multiple skinClusters exist on the object.
        RuntimeError: If user cancels due to active selection symmetry.

    Example::

        tool = SlideVertexWeightsTool()
        tool.set_tool()
    """

    def __init__(
        self,
        nodes: list[str] | None = None,
        mapped_joints: dict[str, list[float]] | None = None,
        increment: float = 0.01,
        near_inf_threshold: float = 0.0005,
        **kwargs: Any,
    ):
        super().__init__(name="SlideVertexWeightsToolCTX", **kwargs)

        if cmds.symmetricModelling(query=True, symmetry=True):
            choice = cmds.confirmDialog(
                title="Selection Symmetry Warning",
                message=(
                    "The SlideVertexWeightsTool won't work when selection "
                    "symmetry is enabled."
                    "\n\nWould you like to turn off selection symmetry?"
                ),
                button=["Turn Off Symmetry", "Cancel"],
                defaultButton="Turn Off Symmetry",
                cancelButton="Cancel",
                dismissString="Cancel",
            )

            if choice == "Turn Off Symmetry":
                cmds.symmetricModelling(symmetry=False)
                cmds.inViewMessage(
                    amg="Selection symmetry has been <hl>disabled</hl>.",
                    pos="topCenter",
                    fade=True,
                )
            else:
                raise RuntimeError(
                    "SlideVertexWeightsTool cancelled due to active "
                    "selection symmetry"
                )

        nodes = _get_nodes(nodes)

        assert "[" in nodes[0], "must have vertices selected"

        self.sel_verts = cmds.ls(nodes, flatten=True)
        sel_node = nodes[0].split(".")[0]

        self.increment = increment
        self.near_inf_threshold = near_inf_threshold
        self.closest_joint: str | None = None
        self.closest_value: float = 0.0
        self.closest_joint_idx: int = 0
        self.closest_joint_map: dict[float, str] = {}
        self.slid = False

        sc = find_related_deformers(sel_node, "skinCluster")

        assert sc, f"No skinCluster found on selected object: {sel_node}"
        if len(sc) > 1:
            raise ValueError("More than one skinCluster found on selected")

        self.skin_cluster = sc[0]
        self.skin_cluster_mobject = _get_mobject(sc[0])

        self.original_normalize_weights = cmds.getAttr(
            f"{self.skin_cluster}.normalizeWeights"
        )
        cmds.setAttr(f"{self.skin_cluster}.normalizeWeights", 0)

        joints = get_skin_cluster_influences(self.skin_cluster_mobject)
        self.influence_names = joints

        if not mapped_joints:
            jnt_poses = [cmds.xform(x, q=True, ws=True, t=True) for x in joints]
            mapped_joints = dict(zip(joints, jnt_poses))

        self.mapped_joints = mapped_joints

        self.starting_weights: OpenMaya2.MDoubleArray | None = None
        self.number_of_influences: int = 0
        self.number_of_weights: int = 0
        self.influence_indices: OpenMaya2.MIntArray | None = None
        self.significant_weights: list[bool] = []
        self.soft_selection_weights: list[float] = []

        self._get_starting_weights()
        self.slid_weights = OpenMaya2.MDoubleArray(self.starting_weights)

    def on_press(self):
        if self.closest_joint is not None:
            return

        cmds.scriptEditorInfo(suppressWarnings=True)

        vp_x, vp_y, _ = self.anchor_point

        pos = _om1.MPoint()
        ray = _om1.MVector()
        _omui.M3dView().active3dView().viewToWorld(int(vp_x), int(vp_y), pos, ray)
        ray.normalize()

        last_result = 0.0
        for jnt, point in self.mapped_joints.items():
            test_vector = _om1.MPoint(*point) - pos
            test_vector.normalize()

            result = test_vector * ray

            if result > last_result:
                last_result = result
                self.closest_joint = jnt
                self.closest_value = result
                self.closest_joint_idx = self.influence_names.index(jnt)

            self.closest_joint_map[result] = jnt

        close_joints_keys = [
            x
            for x in self.closest_joint_map
            if self.closest_value - x < self.near_inf_threshold
        ]
        if len(close_joints_keys) > 1:
            close_joints = [self.closest_joint_map[x] for x in close_joints_keys]
            choice = cmds.confirmDialog(
                title="Multiple Influences Near",
                message="Choose Influence",
                button=close_joints,
                cancelButton="Cancel",
                dismissString="Cancel",
            )
            self.closest_joint = choice

        cmds.inViewMessage(
            amg=f"Target Joint Set: <hl>{self.closest_joint}</hl>.",
            pos="topCenter",
            fade=True,
        )

    def on_drag(self):
        increment = self.increment

        if self.modifier == "ctrl":
            increment *= 0.1
        elif self.modifier == "shift":
            increment *= 10

        self._slide_weights()
        self.slid = True

    def on_release(self):
        if not self.slid:
            return

        cmds.setAttr(
            f"{self.skin_cluster}.normalizeWeights",
            self.original_normalize_weights,
        )

        cmds.scriptEditorInfo(suppressWarnings=False)
        cmds.SelectTool()

    def _get_starting_weights(self):
        weights, inf_num, soft_selection_weights = (
            get_skin_weights_from_selected_components(self.skin_cluster_mobject)
        )
        self.starting_weights = weights
        self.number_of_influences = inf_num
        self.number_of_weights = len(weights)

        if not soft_selection_weights:
            soft_selection_weights = [1.0] * self.number_of_weights
        self.soft_selection_weights = soft_selection_weights

        self.influence_indices = OpenMaya2.MIntArray(list(range(inf_num)))
        self.significant_weights = [x != 0.0 for x in self.starting_weights]

    def _update_weights(self):
        set_skin_weights_to_selected_components(
            self.skin_cluster_mobject,
            self.slid_weights,
            self.influence_indices,
        )

    def _slide_weights(self):
        k = 0
        for i in range(0, self.number_of_weights, self.number_of_influences):
            for j in range(self.number_of_influences):
                idx = i + j
                if j == self.closest_joint_idx:
                    new_weight = self.starting_weights[idx] + (
                        ((1.0 - self.starting_weights[idx]) * self.x)
                        * self.soft_selection_weights[k]
                    )
                    self.slid_weights[idx] = new_weight
                else:
                    if not self.significant_weights[idx]:
                        continue
                    new_weight = self.starting_weights[idx] - (
                        (self.starting_weights[idx] * self.x)
                        * self.soft_selection_weights[k]
                    )
                    self.slid_weights[idx] = new_weight
            k += 1

        self._update_weights()
        _undo_commit(self._undo_it, self._redo_it)

    def _undo_it(self):
        set_skin_weights_to_selected_components(
            self.skin_cluster_mobject,
            self.starting_weights,
            self.influence_indices,
        )

    def _redo_it(self):
        self._update_weights()


class MarkingMenuBase:
    """Base class for popup marking menus in Maya viewports.

    Subclasses must implement :meth:`build_marking_menu`.
    """

    MENU_NAME = "tempMM"
    CONTEXTUAL = False

    def __init__(
        self,
        button: int = 1,
        ctrl: bool = False,
        alt: bool = False,
        shift: bool = False,
    ):
        self.menu: str | None = None
        self.cursor: tuple | None = None
        self.sel: list[str] | None = None

        options: dict[str, Any] = {
            "button": button,
            "ctl": ctrl,
            "alt": alt,
            "sh": shift,
            "allowOptionBoxes": True,
            "mm": True,
        }
        self.options = options

        self.remove()
        self.build_menu()

    def build_menu(self) -> None:
        self.menu = cmds.popupMenu(
            self.MENU_NAME,
            pmc=self._build_marking_menu,
            p="viewPanes",
            **self.options,
        )

    def remove(self) -> None:
        if cmds.popupMenu(self.MENU_NAME, ex=1):
            cmds.deleteUI(self.MENU_NAME)

    def add_sub_menu(self, label: str, position: str) -> str:
        if not position:
            return cmds.menuItem(l=label, subMenu=True)
        return cmds.menuItem(l=label, rp=position, subMenu=True)

    def exit_sub_menu(self, parent: str = MENU_NAME) -> str:
        return cmds.setParent(parent, m=True)

    def add_separator(self) -> str:
        return cmds.menuItem(d=True)

    def add_label(self, label: str) -> str:
        return cmds.menuItem(l=f"---- {label} ----", en=False, boldFont=True)

    def add_menu_item(
        self,
        label: str,
        position: str,
        callback: Any,
        icon: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Add a menu item.

        Args:
            label: Display text.
            position: Radial position (``'N'``, ``'NW'``, etc.) or ``''``
                for a list item.
            callback: Python callable.
        """
        if not position:
            return cmds.menuItem(l=label, c=callback, sourceType="python", **kwargs)
        return cmds.menuItem(
            l=label, rp=position, c=callback, sourceType="python", **kwargs
        )

    def handle_callback(self, callback: Any, *args: Any) -> None:
        callback()

    def _build_marking_menu(self, parent: str, menu: str) -> None:
        if self.CONTEXTUAL:
            self.sel = cmds.ls(sl=True)
            self.cursor = cmds.draggerContext("context", q=True, anchorPoint=True)
        self.build_marking_menu(menu, parent)

    def build_marking_menu(self, menu: str, parent: str) -> None:
        raise NotImplementedError("Subclasses must implement this method")


class SkinningMarkingMenu(MarkingMenuBase):
    """Marking menu providing quick access to skinCluster operations.

    Example::

        SkinningMarkingMenu(button=1, ctrl=True)
    """

    def build_marking_menu(self, menu: str, parent: str) -> None:
        self.add_menu_item("Slide Weights Tool", "N", self.slide_vtx_weights_tool)

        self.add_menu_item(
            "Mirror Weights <-X", "NW", partial(self.mirror_weights, False)
        )
        self.add_menu_item(
            "Mirror Weights X->", "NE", partial(self.mirror_weights, True)
        )

        self.add_menu_item(
            "Smooth Weights", "W", average_vert_skin_weights_with_neighbors
        )
        self.add_menu_item("Hammer Weights", "E", cmds.WeightHammer)

        self.add_menu_item(
            "Export to Temp (fqName)",
            "SW",
            partial(export_skin_weights, force=True, fq_named=True),
        )
        self.add_menu_item(
            "Import from Temp (fqName)",
            "SE",
            partial(import_skin_weights, force=True, fq_named=True),
        )

        self.add_menu_item("Copy skinCluster", "S", copy_skin_cluster)

        # -------------------------------------------------- Import / Export SubMenu ---
        self.add_sub_menu("Import / Export", "")

        self.add_menu_item(
            "Export to Temp", "", partial(export_skin_weights, force=True)
        )
        self.add_menu_item(
            "Import from Temp", "", partial(import_skin_weights, force=True)
        )
        self.add_separator()
        self.add_menu_item(
            "Export to Temp (fqName)",
            "",
            partial(export_skin_weights, force=True, fq_named=True),
        )
        self.add_menu_item(
            "Import from Temp (fqName)",
            "",
            partial(import_skin_weights, force=True, fq_named=True),
        )
        self.add_separator()
        self.add_menu_item("Export to File", "", self.export_skin_weights_to_file)
        self.add_menu_item("Import from File", "", self.import_skin_weights_from_file)

        self.exit_sub_menu()
        # ----------------------------------------------------- Influences SubMenu ----
        self.add_sub_menu("Influences", "")

        self.add_menu_item("Select Influences", "", self.select_influences)
        self.add_menu_item(
            "Match Influences",
            "",
            partial(match_influences_on_skin_clusters, force=True),
        )
        self.add_separator()
        self.add_menu_item("Add Influences", "", cmds.AddInfluence)
        self.add_menu_item(
            "Add InfluencesOB", "", cmds.AddInfluenceOptions, optionBox=True
        )
        self.add_menu_item("Remove Influences", "", cmds.RemoveInfluence)
        self.add_menu_item("Remove Unused", "", cmds.RemoveUnusedInfluences)
        self.add_separator()
        self.add_menu_item(
            "Lock All Influences", "", partial(self.set_lock_weights, True)
        )
        self.add_menu_item(
            "Unlock All Influences", "", partial(self.set_lock_weights, False)
        )

        self.exit_sub_menu()
        # ---------------------------------------------------- skinCluster SubMenu ----
        self.add_sub_menu("skinCluster", "")

        self.add_menu_item("Copy skinCluster", "", copy_skin_cluster)
        self.add_menu_item("Mirror skinClusters", "", mirror_skin_clusters)
        self.add_menu_item(
            "MirrorWeightsOB", "", cmds.MirrorSkinWeightsOptions, optionBox=True
        )
        self.add_separator()
        self.add_menu_item(
            "Add skinCluster",
            "",
            partial(self.handle_callback, add_skin_cluster),
        )
        self.add_menu_item("Del skinClusters", "", delete_skin_clusters)
        self.add_separator()
        self.add_menu_item("rebuild skinCluster", "", rebuild_skin_cluster)
        self.add_separator()
        self.add_menu_item(
            "Set All to Post Normalize", "", self.set_all_to_post_normalize
        )

        self.exit_sub_menu()

    def export_skin_weights_to_file(self, *args: Any) -> None:
        file = cmds.fileDialog2(fm=0, ff="*.weights")
        if file:
            export_skin_weights(path=file[0], force=True)

    def import_skin_weights_from_file(self, *args: Any) -> None:
        file = cmds.fileDialog2(fm=1, ff="*.weights")
        if file:
            import_skin_weights(path=file[0], force=True)

    def copy_weights(self, *args: Any) -> None:
        mel.eval("artAttrSkinWeightCopy;")

    def paste_weights(self, *args: Any) -> None:
        mel.eval("artAttrSkinWeightPaste;")

    def slide_vtx_weights_tool(self, *args: Any) -> None:
        tool = SlideVertexWeightsTool(
            multiplier=0.01, min_value=0.0, max_value=1.0, increment=0.01
        )
        tool.set_tool()

    def mirror_weights(self, mirror_inverse: bool, *args: Any) -> None:
        last_ctx = cmds.currentCtx()
        cmds.SelectTool()
        ct = cmds.currentTime(query=True)
        cmds.currentTime(-1)
        mirror_skin_weights(mirror_inverse=mirror_inverse)
        cmds.currentTime(ct)
        cmds.setToolTo(last_ctx)

    def select_influences(self, *args: Any) -> None:
        influences = cmds.skinCluster(q=True, inf=True)
        cmds.select(influences, r=True)

    def set_lock_weights(self, lock: bool = True, *args: Any) -> None:
        influences = cmds.skinCluster(q=True, inf=True)
        for inf in influences:
            cmds.setAttr(f"{inf}.liw", lock)

    def set_all_to_post_normalize(self, *args: Any) -> None:
        set_all_skin_clusters_normalize_weight_mode(2)
