"""
Rebuild Material-Linker catalog entries as native, editable VOP subnets.

The Material Linker's "Catalog" tab is backed by a ``hou.AssetGalleryDataSource``
(a SQLite ``.db``). Each entry is a *reference* to a material USD on disk -- when you
use it via the linker it comes in as a read-only reference, so the only in-UI way to
tweak it is Edit-Material overrides.

This tool reads the catalog DB, opens each entry's USD material, walks its MaterialX
shader graph, and rebuilds it as native VOP nodes inside a Solaris ``materiallibrary``.
The container is a real **MaterialX Builder** (Houdini's own
``voptoolutils._setupMtlXBuilderSubnet`` -- ShaderLanguageName=MaterialX, mtlx render
context, ``surface_output`` subnetconnector), not a generic Subnetwork. Texture reads
use ``mtlxtiledimage`` (tiling/offset controls) for every map, normals included. The
artist gets a real, openable material with no override cruft, and texture paths are
stored ``$HIP``-relative.

Scope / assumptions (validated against KitBash3D Cargo exports, 2026-07-08):
  - Entries are MaterialX materials (``outputs:mtlx:surface``). Non-mtlx entries are
    skipped and noted in the subnet comment.
  - The material may be ``instanceable`` and its texture paths may be driven by a
    ``texture_variant`` variant set (Cargo: jpg/png * 1k/2k/4k). We de-instance in the
    session layer and read the composed graph under the chosen variant, so texture
    ``inputs:file`` come through already resolved.
  - The ``info:id`` -> VOP-type map is derived generically (strip ``ND_``, peel
    signature suffixes), so this is not hard-coded to Cargo's node set.

Usage (shelf tool):
    import importlib, nscr_catalog_import
    importlib.reload(nscr_catalog_import)
    nscr_catalog_import.run()

Scriptable entry points:
    read_catalog(db_path)                            -> list[CatalogEntry]
    build_material(usd, prim, matlib, variant=)      -> hou.Node (the new subnet)
    import_entries(db_path, matlib, ids=, variant=)  -> list[hou.Node]
"""

import glob
import json
import os
from dataclasses import dataclass

import hou
from pxr import Sdf, Usd, UsdShade


# ---------------------------------------------------------------------------
# Catalog DB reading
# ---------------------------------------------------------------------------

@dataclass
class CatalogEntry:
    """One material entry in a Material-Linker catalog database."""

    item_id: str
    label: str
    type_name: str          # "material" for material entries
    file_path: str          # raw (may contain $HIP etc.)
    ref_prim_path: str      # prim path of the material inside file_path

    @property
    def resolved_file(self):
        """``file_path`` with Houdini vars expanded to an absolute path."""
        return hou.text.expandString(self.file_path)


# Render galleries are AssetGallery DBs too but hold rendered frames, not materials.
_NON_MATERIAL_DB_HINTS = ("rendergallery",)


def find_catalogs(root=None, max_depth=3):
    """Find material-catalog ``.db`` files near ``root`` (defaults to ``$HIP``).

    Scans ``root`` and its subfolders down to ``max_depth`` for ``*.db`` files that are
    AssetGallery databases containing at least one ``material`` entry. Render-gallery
    DBs and anything that fails to open are skipped. Returns a de-duplicated list of
    forward-slash paths.
    """
    root = (root or hou.text.expandString("$HIP")).replace("\\", "/")
    seen = set()
    found = []
    for depth in range(max_depth + 1):
        pattern = os.path.join(root, *(["*"] * depth), "*.db")
        for path in glob.glob(pattern):
            path = path.replace("\\", "/")
            if path in seen:
                continue
            seen.add(path)
            if any(h in os.path.basename(path).lower() for h in _NON_MATERIAL_DB_HINTS):
                continue
            try:
                src = hou.AssetGalleryDataSource(path)
                if any(src.typeName(i) == "material" for i in src.itemIds()):
                    found.append(path)
            except Exception:
                continue
    return found


def read_catalog(db_path):
    """Return every material entry in the AssetGallery ``.db`` at ``db_path``."""
    src = hou.AssetGalleryDataSource(db_path)
    entries = []
    for item_id in src.itemIds():
        ref_prim = ""
        blind = src.blindData(item_id)
        if blind:
            try:
                ref_prim = json.loads(bytes(blind).decode("utf-8")).get("refprimpath", "")
            except (ValueError, UnicodeDecodeError):
                ref_prim = ""
        entries.append(
            CatalogEntry(
                item_id=item_id,
                label=src.label(item_id),
                type_name=src.typeName(item_id),
                file_path=src.filePath(item_id),
                ref_prim_path=ref_prim,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# info:id  ->  VOP type / signature resolution
# ---------------------------------------------------------------------------

# MaterialX nodedef ids are ``ND_<name>_<signature...>``. The Houdini VOP type is
# ``mtlx<name>`` and the signature lands in the node's ``signature`` parm. These are the
# type tokens that appear as a trailing suffix we must peel off ``<name>``.
_SIGNATURE_TOKENS = {
    "float", "color3", "color4", "vector2", "vector3", "vector4",
    "integer", "boolean", "matrix33", "matrix44", "string", "filename",
    "color", "vector",
}
# Shader-terminal suffixes are part of the node identity, not a signature parm.
_TERMINAL_SUFFIXES = ("surfaceshader", "displacementshader", "volumeshader")

# MaterialX signature token -> the token Houdini's ``signature`` menu actually uses.
# (mtlximage/mtlxnormalmap spell the float signature "default" -- menu label "Float".)
_SIGNATURE_PARM_MAP = {"float": "default"}

# Prefer mtlxtiledimage over mtlximage everywhere (gives artists tiling/offset controls).
# Its signature parm accepts every image signature (float/color3/vector3/...) even though
# the menu only lists a couple -- same non-exhaustive-menu quirk as the "float" token.
_PREFERRED_IMAGE_TYPE = "mtlxtiledimage"


def _resolve_vop_type(info_id):
    """Map a MaterialX ``info:id`` to ``(vop_type_name, signature_or_None)`` or None."""
    vop_types = hou.vopNodeTypeCategory().nodeTypes()

    def _exists(name):
        # Prefer an exact/bare match; fall back to the highest namespaced version.
        if name in vop_types:
            return name
        versioned = sorted(t for t in vop_types if t.split("::")[0] == name)
        return versioned[-1] if versioned else None

    name = info_id
    for pfx in ("ND_", "IM_", "NG_"):
        if name.startswith(pfx):
            name = name[len(pfx):]
            break

    # Shader-terminal nodes (standard_surface_surfaceshader -> mtlxstandard_surface).
    for suffix in _TERMINAL_SUFFIXES:
        if name.endswith("_" + suffix):
            base = name[: -(len(suffix) + 1)]
            resolved = _exists("mtlx" + base)
            return (resolved, None) if resolved else None

    # Peel signature suffixes until the remainder names a real VOP type.
    parts = name.split("_")
    signature = None
    while parts:
        candidate = _exists("mtlx" + "_".join(parts))
        if candidate:
            return candidate, signature
        tail = parts[-1]
        if tail in _SIGNATURE_TOKENS:
            signature = tail
            parts = parts[:-1]
        else:
            break
    return None


# ---------------------------------------------------------------------------
# USD value  ->  VOP parm
# ---------------------------------------------------------------------------

# USD colorSpace token -> mtlximage filecolorspace menu token.
_COLORSPACE_MAP = {
    "srgb_texture": "srgb_texture",
    "sRGB": "srgb_texture",
    "raw": "Raw",
    "data": "Raw",
    "": "",
}


def _hip_relative(path):
    """Collapse an absolute texture path to ``$HIP``/``$JOB``-relative when possible."""
    if not path:
        return path
    return hou.text.collapseCommonVars(path.replace("\\", "/"), vars=["$HIP", "$JOB"])


def _set_vop_parm(node, base_name, value):
    """Set the VOP parm(s) for a USD input ``base_name`` to ``value``.

    Handles scalar parms, ``r/g/b`` colour tuples, ``x/y/z`` vector tuples, and
    ``Sdf.AssetPath`` file inputs.
    """
    if value is None:
        return

    if isinstance(value, Sdf.AssetPath):
        p = node.parm("file") or node.parm(base_name)
        if p is not None:
            p.set(_hip_relative(value.resolvedPath or value.path))
        return

    # Tuple-valued (colour / vector): try r/g/b then x/y/z component parms.
    is_tuple = isinstance(value, (tuple, list)) or (
        hasattr(value, "__len__") and not isinstance(value, str)
    )
    if is_tuple:
        comps = list(value)
        for suffixes in (("r", "g", "b", "a"), ("x", "y", "z", "w")):
            parms = [node.parm(base_name + s) for s in suffixes[: len(comps)]]
            if all(parms):
                for p, v in zip(parms, comps):
                    p.set(float(v))
                return
        return

    # Scalar.
    p = node.parm(base_name)
    if p is None:
        return
    try:
        p.set(value)
    except (TypeError, hou.OperationFailed):
        try:
            p.set(float(value))
        except (TypeError, ValueError):
            pass


def _apply_colorspace(node, attr):
    cs = attr.GetColorSpace()
    token = _COLORSPACE_MAP.get(str(cs), str(cs) if cs else "")
    csp = node.parm("filecolorspace")
    if csp is not None and token:
        try:
            csp.set(token)
        except hou.OperationFailed:
            pass


# ---------------------------------------------------------------------------
# The core: rebuild one USD material as a native VOP subnet
# ---------------------------------------------------------------------------

def _make_mtlx_builder(matlib, name):
    """Create a native **MaterialX Builder** subnet shell in ``matlib``.

    Uses Houdini's own ``voptoolutils._setupMtlXBuilderSubnet`` (the code behind the
    TAB-menu "MaterialX Builder") so the subnet gets ``ShaderLanguageName='MaterialX'``,
    the mtlx render-context / tab-mask spare parms, the ``inputs`` node, and the
    ``surface_output`` / ``displacement_output`` subnetconnectors -- not a generic
    Subnetwork. The starter standard_surface/displacement shaders are removed so the
    catalog graph can be built in their place.

    Returns ``(subnet, surface_output, displacement_output)``. If the (private) setup
    API is unavailable, returns ``(bare_subnet, None, None)`` and the caller falls back
    to an ``mtlxsurfacematerial`` + material-flag shell.
    """
    subnet = matlib.createNode("subnet", node_name=name)
    try:
        import voptoolutils

        voptoolutils._setupMtlXBuilderSubnet(subnet_node=subnet, name=name)
        for junk in ("mtlxstandard_surface", "mtlxdisplacement"):
            n = subnet.node(junk)
            if n is not None:
                n.destroy()
        return subnet, subnet.node("surface_output"), subnet.node("displacement_output")
    except Exception:
        return subnet, None, None


def build_material(usd_file, ref_prim_path, matlib, name=None, variant=None):
    """Rebuild the MaterialX material at ``ref_prim_path`` in ``usd_file`` as a native
    VOP ``subnet`` inside the ``materiallibrary`` LOP ``matlib``. Returns the subnet.
    """
    stage = Usd.Stage.Open(usd_file, Usd.Stage.LoadAll)
    mat_prim = stage.GetPrimAtPath(ref_prim_path)
    if not mat_prim or not mat_prim.IsValid():
        raise RuntimeError("No prim at {} in {}".format(ref_prim_path, usd_file))

    # De-instance so the composed shader children (and variant-driven file paths) are
    # visible to traversal, and pin the requested texture variant.
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        mat_prim.SetInstanceable(False)
        if variant is not None:
            vset = mat_prim.GetVariantSets().GetVariantSet("texture_variant")
            if vset and variant in vset.GetVariantNames():
                vset.SetVariantSelection(variant)

    mat = UsdShade.Material(mat_prim)
    if not mat:
        raise RuntimeError("{} is not a UsdShade.Material".format(ref_prim_path))

    subnet_name = _clean_name(name or mat_prim.GetName())
    subnet, surface_output, _disp_output = _make_mtlx_builder(matlib, subnet_name)

    shader_prims = [p for p in Usd.PrimRange(mat_prim)
                    if p != mat_prim and p.GetTypeName() == "Shader"]

    # Create one VOP per shader; remember the mapping and skip unsupported ids.
    vop_by_path = {}
    skipped = []
    for sp in shader_prims:
        info_id = UsdShade.Shader(sp).GetIdAttr().Get()
        resolved = _resolve_vop_type(info_id) if info_id else None
        if resolved is None:
            skipped.append("{} ({})".format(sp.GetName(), info_id))
            continue
        vop_type, signature = resolved
        if (vop_type == "mtlximage"
                and _PREFERRED_IMAGE_TYPE in hou.vopNodeTypeCategory().nodeTypes()):
            vop_type = _PREFERRED_IMAGE_TYPE
        vop = subnet.createNode(vop_type, node_name=_clean_name(sp.GetName()))
        if signature and vop.parm("signature") is not None:
            token = _SIGNATURE_PARM_MAP.get(signature, signature)
            try:
                vop.parm("signature").set(token)
            except hou.OperationFailed:
                pass
        vop_by_path[sp.GetPath().pathString] = vop

    # Second pass: set input values and record inter-node wires.
    pending_wires = []
    for sp in shader_prims:
        dst = vop_by_path.get(sp.GetPath().pathString)
        if dst is None:
            continue
        for usd_in in UsdShade.Shader(sp).GetInputs():
            base = usd_in.GetBaseName()
            src_prim, src_name = _connected_source(usd_in)
            if src_prim is not None:
                src_path = src_prim.GetPath().pathString
                if src_path in vop_by_path:
                    pending_wires.append((dst, base, vop_by_path[src_path], src_name))
                    continue
                if src_prim.GetPath() == mat_prim.GetPath():
                    iface = mat.GetInput(src_name)
                    if iface:
                        _set_vop_parm(dst, base, iface.Get())
                    continue
            val = usd_in.Get()
            if val is not None:
                _set_vop_parm(dst, base, val)
                if isinstance(val, Sdf.AssetPath):
                    _apply_colorspace(dst, usd_in.GetAttr())

    # Third pass: wire inter-node connections.
    for dst, dst_input, src_vop, src_output in pending_wires:
        if dst_input in dst.inputNames():
            outs = src_vop.outputNames()
            out_idx = outs.index(src_output) if src_output in outs else 0
            dst.setInput(dst.inputIndex(dst_input), src_vop, out_idx)

    # Wire the surface shader into the builder's Surface output.
    surface_vop = _find_surface_shader(mat, vop_by_path)
    terminal = surface_output
    if surface_output is not None:
        if surface_vop is not None:
            surface_output.setInput(0, surface_vop, 0)
    else:
        # Fallback shell (native builder setup unavailable): mtlxsurfacematerial + flag.
        terminal = subnet.createNode("mtlxsurfacematerial", node_name=subnet_name)
        if surface_vop is not None:
            terminal.setNamedInput("surfaceshader", surface_vop, "out")
        subnet.setMaterialFlag(True)

    _layout_subnet(subnet, terminal)
    subnet.setColor(hou.Color((0.35, 0.55, 0.30)))  # green: rebuilt-from-catalog tint
    subnet.setComment(
        "Native rebuild of catalog material (nscr_catalog_import)\n"
        "source: {}\nprim:   {}".format(usd_file, ref_prim_path)
        + ("\nvariant: {}".format(variant) if variant else "")
        + ("\nSKIPPED nodes: {}".format(", ".join(skipped)) if skipped else "")
    )
    return subnet


def _connected_source(usd_input):
    """Return ``(source_prim, source_output_name)`` or ``(None, None)``."""
    if not usd_input.HasConnectedSource():
        return None, None
    infos = usd_input.GetConnectedSources()[0]
    if not infos:
        return None, None
    info = infos[0]
    return info.source.GetPrim(), info.sourceName


def _find_surface_shader(mat, vop_by_path):
    """Find the VOP that drives the material's surface output."""
    surf_out = mat.GetSurfaceOutput("mtlx") or mat.GetSurfaceOutput()
    if surf_out and surf_out.HasConnectedSource():
        info = surf_out.GetConnectedSources()[0][0]
        return vop_by_path.get(info.source.GetPrim().GetPath().pathString)
    for _path, vop in vop_by_path.items():
        if "surface" in vop.type().name():
            return vop
    return None


def _clean_name(raw):
    """Houdini-safe node name."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw)
    if safe and safe[0].isdigit():
        safe = "m_" + safe
    return safe or "material"


def _layout_subnet(subnet, terminal):
    """Deterministic manual placement, right-to-left by distance from the terminal.

    Never calls ``layoutChildren`` -- node placement is authored explicitly so the
    tool can't ever disturb a deliberate layout (nscr-tools house rule).
    """
    depth = {terminal.path(): 0}
    frontier = [terminal]
    while frontier:
        nxt = []
        for node in frontier:
            d = depth[node.path()]
            for src in node.inputs():
                if src is not None and src.path() not in depth:
                    depth[src.path()] = d + 1
                    nxt.append(src)
        frontier = nxt

    max_d = max(depth.values()) if depth else 0
    by_depth = {}
    for child in subnet.children():
        d = depth.get(child.path(), max_d + 1)
        by_depth.setdefault(d, []).append(child)

    for d, nodes in by_depth.items():
        nodes.sort(key=lambda n: n.name())
        for row, node in enumerate(nodes):
            node.setPosition(hou.Vector2(-3.0 * d, -1.2 * row))


# ---------------------------------------------------------------------------
# Batch + UI entry points
# ---------------------------------------------------------------------------

def import_entries(db_path, matlib, ids=None, variant=None):
    """Rebuild catalog entries (all, or the subset in ``ids``) into ``matlib``."""
    entries = read_catalog(db_path)
    if ids is not None:
        wanted = set(ids)
        entries = [e for e in entries if e.item_id in wanted]

    built = []
    for e in entries:
        if e.type_name != "material":
            continue
        built.append(
            build_material(e.resolved_file, e.ref_prim_path, matlib,
                           name=e.label, variant=variant)
        )
    return built


def _resolve_materiallibrary():
    """Selected materiallibrary, or any selected VOP-child container, else None."""
    for node in hou.selectedNodes():
        if node.type().name() == "materiallibrary":
            return node
    for node in hou.selectedNodes():
        cat = node.childTypeCategory()
        if cat is not None and cat.name() == "Vop":
            return node
    return None


def run():
    """Shelf entry: auto-detect catalog DB(s) near ``$HIP``, pool their entries, and
    rebuild the picked materials into the selected ``materiallibrary``.

    If no catalog is found next to the hip file, falls back to a manual file picker.
    """
    matlib = _resolve_materiallibrary()
    if matlib is None:
        hou.ui.displayMessage(
            "Select a materiallibrary LOP (the target for the rebuilt materials), "
            "then run again.",
            severity=hou.severityType.Warning,
            title="Catalog -> Native Materials",
        )
        return

    catalogs = find_catalogs()
    if not catalogs:
        db_path = hou.ui.selectFile(
            title="No catalog found near $HIP — choose one manually (.db)",
            pattern="*.db",
            chooser_mode=hou.fileChooserMode.Read,
        )
        if not db_path:
            return
        catalogs = [hou.text.expandString(db_path)]

    # Pool material entries across every catalog found.
    pooled = []  # (db_path, CatalogEntry)
    for db in catalogs:
        for e in read_catalog(db):
            if e.type_name == "material":
                pooled.append((db, e))
    if not pooled:
        hou.ui.displayMessage("No material entries found in the catalog(s).",
                              title="Catalog -> Native Materials")
        return

    multi = len(catalogs) > 1
    labels = []
    for db, e in pooled:
        labels.append(
            "[{}]  {}".format(os.path.basename(db), e.label) if multi else e.label
        )

    src_note = (
        "{} catalog(s) found near $HIP:\n  ".format(len(catalogs))
        + "\n  ".join(os.path.relpath(c, hou.text.expandString("$HIP")) for c in catalogs)
        + "\n\nRebuild these as native VOP subnets in {}:".format(matlib.name())
    )
    picks = hou.ui.selectFromList(
        labels,
        title="Catalog -> Native Materials",
        message=src_note,
        clear_on_cancel=True,
    )
    if not picks:
        return

    built = []
    for i in picks:
        db, e = pooled[i]
        built.append(
            build_material(e.resolved_file, e.ref_prim_path, matlib, name=e.label)
        )
    hou.ui.displayMessage(
        "Rebuilt {} material(s) into {}:\n".format(len(built), matlib.path())
        + "\n".join("  " + n.name() for n in built),
        title="Catalog -> Native Materials",
    )
