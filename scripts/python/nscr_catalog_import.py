"""
Rebuild referenced MaterialX materials as native, editable VOP subnets.

Materials that arrive by *reference* -- from a Material-Linker catalog, or from an
imported/converted USD asset on a live LOP stage -- are read-only. The only in-UI way
to tweak them is Edit-Material overrides, which means ticking an ``__activate__``
checkbox for every single parameter you want to touch.

This tool walks the material's MaterialX shader graph and rebuilds it as native VOP
nodes inside a Solaris ``materiallibrary``. The container is a real **MaterialX
Builder** (Houdini's own ``voptoolutils._setupMtlXBuilderSubnet`` --
ShaderLanguageName=MaterialX, mtlx render context, ``surface_output`` subnetconnector),
not a generic Subnetwork. Texture reads use ``mtlxtiledimage`` (tiling/offset controls)
for every map, normals included. The artist gets a real, openable material with no
override cruft, and texture paths are stored ``$HIP``-relative.

Two sources, one rebuild core (``build_material_from_prim``):

  **Catalog** -- the Material Linker's "Catalog" tab, a ``hou.AssetGalleryDataSource``
  SQLite ``.db`` whose entries point at material USDs on disk. Validated against
  KitBash3D Cargo exports, 2026-07-08. Such a material may be ``instanceable`` with
  texture paths driven by a ``texture_variant`` variant set (Cargo: jpg/png * 1k/2k/4k);
  we de-instance in the session layer and pin the requested variant so ``inputs:file``
  composes to a real path.

  **Live stage** -- material prims on a LOP's cooked stage (e.g. the output of an
  MDL->MaterialX conversion). The stage is read *composed and as-is*; it is never
  mutated. Instanced materials are followed to their prototype.

Common to both:
  - Only MaterialX materials (``outputs:mtlx:surface``) are rebuilt. The shader graph is
    collected by walking *backwards from the mtlx surface/displacement terminals*, so a
    UsdPreviewSurface graph sitting alongside it under the same Material prim is ignored
    rather than reported as unsupported.
  - The ``info:id`` -> VOP-type map is derived generically (strip ``ND_``, peel signature
    suffixes), so this is not hard-coded to any one asset's node set. Anything that
    still doesn't resolve is skipped and listed in the new subnet's comment.

Usage (shelf tools):
    import importlib, nscr_catalog_import
    importlib.reload(nscr_catalog_import)
    nscr_catalog_import.run()        # "Catalog to Native"
    nscr_catalog_import.run_stage()  # "Stage to Native"
    nscr_catalog_import.run_edit_material()  # "Stage to Edit Material"
    nscr_catalog_import.run_stage_takeover()  # "Stage to Native (Replace)"

Scriptable entry points:
    read_catalog(db_path)                            -> list[CatalogEntry]
    build_material(usd, prim, matlib, variant=)      -> hou.Node (the new subnet)
    import_entries(db_path, matlib, ids=, variant=)  -> list[hou.Node]
    read_stage_materials(lop)                        -> list[Usd.Prim]
    build_material_from_prim(prim, matlib, name=)    -> hou.Node
    import_stage(lop, matlib, paths=)                -> list[hou.Node]
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

# Nodes where Houdini bakes the signature into the TYPE NAME instead of exposing a
# ``signature`` parm, so the generic "peel the suffix" rule can't find them. The channel
# splitters are the ones that matter: there is no ``mtlxseparate3`` -- it ships as
# ``mtlxseparate3c`` (colour) and ``mtlxseparate3v`` (vector). Note the asymmetry with
# combine, which *is* generic (``mtlxcombine3`` + signature parm).
_INFO_ID_TYPE_OVERRIDES = {
    "ND_separate2_vector2": ("mtlxseparate2", None),
    "ND_separate3_color3": ("mtlxseparate3c", None),
    "ND_separate3_vector3": ("mtlxseparate3v", None),
    "ND_separate4_color4": ("mtlxseparate4c", None),
    "ND_separate4_vector4": ("mtlxseparate4v", None),
}


def _resolve_vop_type(info_id):
    """Map a MaterialX ``info:id`` to ``(vop_type_name, signature_or_None)`` or None."""
    if info_id in _INFO_ID_TYPE_OVERRIDES:
        return _INFO_ID_TYPE_OVERRIDES[info_id]

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


def _parm_name_candidates(node, base_name):
    """Parm-name stems to try for the USD input ``base_name``, best first.

    Multi-signature MaterialX VOPs carry one parm set *per signature*, named
    ``<input>_<signature><component>`` -- an mtlxconstant on signature ``color3`` holds
    its value in ``value_color3r/g/b``, and the bare ``value`` is the *float* variant.
    So the node's active signature has to be consulted; matching on the bare name alone
    silently drops every colour and vector constant.

    Single-signature nodes (and float inputs on any node) just use the bare name.
    """
    sig_parm = node.parm("signature")
    sig = sig_parm.evalAsString() if sig_parm is not None else ""
    if sig and sig != "default":
        return [base_name + "_" + sig, base_name]
    return [base_name]


def _set_vop_parm(node, base_name, value):
    """Set the VOP parm(s) for a USD input ``base_name`` to ``value``.

    Handles scalar parms, ``r/g/b`` colour tuples, ``x/y/z`` vector tuples, and
    ``Sdf.AssetPath`` file inputs, across signature-suffixed parm sets.
    """
    if value is None:
        return

    if isinstance(value, Sdf.AssetPath):
        p = node.parm("file") or node.parm(base_name)
        if p is not None:
            p.set(_hip_relative(value.resolvedPath or value.path))
        return

    stems = _parm_name_candidates(node, base_name)

    # Tuple-valued (colour / vector): try r/g/b then x/y/z component parms.
    is_tuple = isinstance(value, (tuple, list)) or (
        hasattr(value, "__len__") and not isinstance(value, str)
    )
    if is_tuple:
        comps = list(value)
        for stem in stems:
            for suffixes in (("r", "g", "b", "a"), ("x", "y", "z", "w")):
                parms = [node.parm(stem + s) for s in suffixes[: len(comps)]]
                if all(parms):
                    for p, v in zip(parms, comps):
                        p.set(float(v))
                    return
        return

    # Scalar.
    p = next((node.parm(s) for s in stems if node.parm(s) is not None), None)
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

    This is the *file* source: it opens its own throwaway stage, so de-instancing and
    variant pinning happen in that stage's session layer and touch nothing live.
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

    source_note = "source: {}\nprim:   {}".format(usd_file, ref_prim_path)
    if variant:
        source_note += "\nvariant: {}".format(variant)
    return build_material_from_prim(mat_prim, matlib, name=name, source_note=source_note)


def build_material_from_prim(mat_prim, matlib, name=None, source_note=None,
                             avoid_names=()):
    """Rebuild the MaterialX material prim ``mat_prim`` as a native VOP ``subnet``
    inside the ``materiallibrary`` LOP ``matlib``. Returns the subnet.

    ``avoid_names``: node names the new shaders must not take. The in-place takeover
    passes the original material's child prim names, so the native shaders land on new
    prims instead of merging into the old ones (which would keep the old wires alive).

    This is the shared rebuild core. ``mat_prim`` may come from a stage opened off disk
    (see ``build_material``) or from a live LOP's cooked stage (see ``import_stage``);
    it is only ever *read*. An instanced material is followed to its prototype, since an
    instance proxy exposes no traversable shader children.
    """
    if mat_prim.IsInstance():
        proto = mat_prim.GetPrototype()
        if proto and proto.IsValid():
            source_note = (source_note or "") + "\n(followed instance -> prototype)"
            mat_prim = proto

    mat = UsdShade.Material(mat_prim)
    if not mat:
        raise RuntimeError("{} is not a UsdShade.Material".format(mat_prim.GetPath()))

    subnet_name = _clean_name(name or mat_prim.GetName())
    subnet, surface_output, _disp_output = _make_mtlx_builder(matlib, subnet_name)

    shader_prims = _mtlx_graph_prims(mat_prim, mat)

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
        node_name = _clean_name(sp.GetName())
        if node_name in avoid_names:
            node_name += "_native"
        vop = subnet.createNode(vop_type, node_name=node_name)
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
                if src_prim.GetTypeName() != "Shader":
                    # Value lives on a Material or NodeGraph interface input.
                    iface = UsdShade.ConnectableAPI(src_prim).GetInput(src_name)
                    val = iface.Get() if iface else None
                    if val is not None:
                        _set_vop_parm(dst, base, val)
                        if isinstance(val, Sdf.AssetPath):
                            _apply_colorspace(dst, iface.GetAttr())
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
    subnet.setColor(hou.Color((0.35, 0.55, 0.30)))  # green: rebuilt-from-reference tint
    subnet.setComment(
        "Native rebuild of referenced material (nscr_catalog_import)\n"
        + (source_note or "prim:   {}".format(mat_prim.GetPath()))
        + ("\nSKIPPED nodes: {}".format(", ".join(skipped)) if skipped else "")
    )
    return subnet


def _mtlx_graph_prims(mat_prim, mat):
    """Shader prims that actually feed the material's **mtlx** terminals.

    Walks backwards from ``outputs:mtlx:surface`` / ``outputs:mtlx:displacement``
    through input connections. This matters for materials that carry a UsdPreviewSurface
    graph alongside the MaterialX one under the same Material prim (MDL->MatX conversions
    do this): a blind ``Usd.PrimRange`` would sweep up every ``UsdUVTexture`` /
    ``UsdPreviewSurface`` sibling and report them all as unsupported.

    Falls back to the full child traversal if the mtlx terminals are unconnected, so a
    material whose graph is only reachable structurally still rebuilds.
    """
    frontier = []
    for context_out in (mat.GetSurfaceOutput("mtlx"), mat.GetDisplacementOutput("mtlx")):
        attr = _value_source(context_out, shader_outputs_only=True) if context_out else None
        if attr is not None:
            frontier.append(attr.GetPrim())

    if not frontier:
        return [p for p in Usd.PrimRange(mat_prim)
                if p != mat_prim and p.GetTypeName() == "Shader"]

    seen = {}
    while frontier:
        prim = frontier.pop()
        if not prim or not prim.IsValid() or prim.GetPath().pathString in seen:
            continue
        if prim.GetTypeName() != "Shader":
            continue  # Material / NodeGraph interface input; resolved to a value later
        seen[prim.GetPath().pathString] = prim
        for usd_in in UsdShade.Shader(prim).GetInputs():
            src_prim, _src_name = _connected_source(usd_in)
            if src_prim is not None:
                frontier.append(src_prim)
    return list(seen.values())


def has_mtlx_surface(prim):
    """True if ``prim`` is a Material with a connected ``outputs:mtlx:surface``."""
    if not prim or not prim.IsValid() or prim.GetTypeName() != "Material":
        return False
    surf = UsdShade.Material(prim).GetSurfaceOutput("mtlx")
    return bool(surf) and surf.HasConnectedSource()


def _value_source(usd_port, shader_outputs_only=False):
    """The attribute that really feeds ``usd_port`` (a shader input or material output),
    followed through any NodeGraph boundaries. ``None`` if nothing is connected.

    A plain ``GetConnectedSources`` stops at the NodeGraph's own output, so the nodes
    inside the graph (texcoord, image, normal map ...) would never be found.
    """
    if not usd_port.HasConnectedSource():
        return None
    attrs = UsdShade.Utils.GetValueProducingAttributes(
        usd_port, shaderOutputsOnly=shader_outputs_only)
    return attrs[0] if attrs else None


def _connected_source(usd_input):
    """Return ``(source_prim, source_name)`` or ``(None, None)``.

    The source is the shader whose output feeds ``usd_input``, or the Material /
    NodeGraph whose interface input holds the value. NodeGraphs are looked through.
    """
    attr = _value_source(usd_input)
    if attr is None:
        return None, None
    name, _kind = UsdShade.Utils.GetBaseNameAndType(attr.GetName())
    return attr.GetPrim(), name


def _find_surface_shader(mat, vop_by_path):
    """Find the VOP that drives the material's surface output."""
    surf_out = mat.GetSurfaceOutput("mtlx") or mat.GetSurfaceOutput()
    attr = _value_source(surf_out, shader_outputs_only=True) if surf_out else None
    if attr is not None:
        return vop_by_path.get(attr.GetPrim().GetPath().pathString)
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


# ---------------------------------------------------------------------------
# Live-stage source
# ---------------------------------------------------------------------------

def read_stage_materials(lop):
    """Return every MaterialX material prim on ``lop``'s cooked stage, path-sorted.

    Read-only: the LOP is cooked and its composed stage traversed, nothing is authored.
    Materials without a connected ``outputs:mtlx:surface`` (UsdPreviewSurface-only, MDL-
    only) are left out -- there is no MaterialX graph to rebuild from.
    """
    stage = lop.stage()
    if stage is None:
        raise RuntimeError("{} has no cooked stage".format(lop.path()))
    mats = [p for p in stage.Traverse() if has_mtlx_surface(p)]
    return sorted(mats, key=lambda p: p.GetPath().pathString)


def import_stage(lop, matlib, paths=None):
    """Rebuild ``lop``'s stage materials (all, or the subset named in ``paths``) into
    the ``materiallibrary`` LOP ``matlib``. Returns the new subnets.
    """
    mats = read_stage_materials(lop)
    if paths is not None:
        wanted = set(paths)
        mats = [m for m in mats if m.GetPath().pathString in wanted]

    built = []
    for prim in mats:
        built.append(
            build_material_from_prim(
                prim, matlib, name=prim.GetName(),
                source_note="source: {} (live stage)\nprim:   {}".format(
                    lop.path(), prim.GetPath()),
            )
        )
    return built


def _resolve_stage_source(matlib):
    """Find the LOP to read materials from.

    Prefers an explicitly selected LOP other than ``matlib``; otherwise falls back to
    whatever feeds ``matlib``'s first input, which is the usual wiring -- you drop the
    materiallibrary downstream of the asset you want to take ownership of.
    """
    for node in hou.selectedNodes():
        if node == matlib or node.type().category() != hou.lopNodeTypeCategory():
            continue
        try:
            if node.stage() is not None:
                return node
        except hou.OperationFailed:
            continue

    upstream = matlib.input(0)
    if upstream is not None:
        try:
            if upstream.stage() is not None:
                return upstream
        except hou.OperationFailed:
            pass
    return None


def run_stage():
    """Shelf entry: rebuild MaterialX materials off a live LOP stage into the selected
    ``materiallibrary``.

    Select the target materiallibrary (wired downstream of the asset) and run. To read
    from somewhere other than the materiallibrary's input, select that LOP too.
    """
    matlib = _resolve_materiallibrary()
    if matlib is None:
        hou.ui.displayMessage(
            "Select a materiallibrary LOP (the target for the rebuilt materials), "
            "then run again.",
            severity=hou.severityType.Warning,
            title="Stage -> Native Materials",
        )
        return

    source = _resolve_stage_source(matlib)
    if source is None:
        hou.ui.displayMessage(
            "No source stage found. Wire {} downstream of the materials you want to "
            "rebuild, or select the source LOP along with it.".format(matlib.name()),
            severity=hou.severityType.Warning,
            title="Stage -> Native Materials",
        )
        return

    try:
        mats = read_stage_materials(source)
    except (RuntimeError, hou.OperationFailed) as err:
        hou.ui.displayMessage(str(err), severity=hou.severityType.Error,
                              title="Stage -> Native Materials")
        return

    if not mats:
        hou.ui.displayMessage(
            "No MaterialX materials on {}.\n\nOnly materials with a connected "
            "outputs:mtlx:surface can be rebuilt -- UsdPreviewSurface-only and MDL-only "
            "materials have no MaterialX graph to read.".format(source.path()),
            title="Stage -> Native Materials",
        )
        return

    picks = hou.ui.selectFromList(
        [p.GetPath().pathString for p in mats],
        title="Stage -> Native Materials",
        message="{} MaterialX material(s) on {}\n\nRebuild these as native VOP subnets "
                "in {}:".format(len(mats), source.path(), matlib.name()),
        clear_on_cancel=True,
    )
    if not picks:
        return

    built = []
    failed = []
    for i in picks:
        prim = mats[i]
        try:
            built.append(
                build_material_from_prim(
                    prim, matlib, name=prim.GetName(),
                    source_note="source: {} (live stage)\nprim:   {}".format(
                        source.path(), prim.GetPath()),
                )
            )
        except (RuntimeError, hou.OperationFailed) as err:
            failed.append("{}: {}".format(prim.GetName(), err))

    msg = "Rebuilt {} material(s) into {}:\n".format(len(built), matlib.path())
    msg += "\n".join("  " + n.name() for n in built)
    if failed:
        msg += "\n\nFAILED {}:\n".format(len(failed)) + "\n".join("  " + f for f in failed)
    hou.ui.displayMessage(msg, title="Stage -> Native Materials")
    return built


# ---------------------------------------------------------------------------
# Live-stage -> downstream LOP helpers
# ---------------------------------------------------------------------------

def _resolve_selected_lop():
    """The first selected LOP that has a cooked stage, else None."""
    for node in hou.selectedNodes():
        if node.type().category() != hou.lopNodeTypeCategory():
            continue
        try:
            if node.stage() is not None:
                return node
        except hou.OperationFailed:
            continue
    return None


def _insert_lop_after(source, node_type, name, inline=True):
    """Create ``node_type`` just below ``source`` and feed it from ``source``.

    With ``inline`` the new node is spliced between ``source`` and everything that was
    wired to its output, so its edits reach the rest of the graph. No other node moves.
    """
    downstream = list(source.outputConnections()) if inline else []
    node = source.parent().createNode(node_type, name)
    pos = source.position()
    node.setPosition(hou.Vector2(pos.x(), pos.y() - 1.0))
    node.setInput(0, source)
    for conn in downstream:
        conn.outputNode().setInput(conn.inputIndex(), node)
    return node


def _pick_stage_materials(source, title, verb):
    """Shared UI: list the MaterialX materials on ``source`` (all pre-selected) and
    return the chosen prims, or None if the user cancelled / nothing to do."""
    try:
        mats = read_stage_materials(source)
    except (RuntimeError, hou.OperationFailed) as err:
        hou.ui.displayMessage(str(err), severity=hou.severityType.Error, title=title)
        return None
    if not mats:
        hou.ui.displayMessage(
            "No MaterialX materials on {}.\n\nOnly materials with a connected "
            "outputs:mtlx:surface are listed.".format(source.path()),
            title=title,
        )
        return None

    labels = [p.GetPath().pathString for p in mats]
    picks = hou.ui.selectFromList(
        labels,
        default_choices=tuple(range(len(labels))),
        title=title,
        message="{} MaterialX material(s) on {}\n\nAll are pre-selected. Deselect any "
                "you do not want to {}:".format(len(labels), source.path(), verb),
        clear_on_cancel=True,
    )
    if not picks:
        return None
    return [mats[i] for i in picks]


# ---------------------------------------------------------------------------
# Live-stage -> Edit Material Network (override route)
# ---------------------------------------------------------------------------

_EDITMAT_TITLE = "Stage -> Edit Material"


def build_edit_material(source, paths, name=None, inline=True):
    """Create an ``editmaterial`` LOP fed by ``source`` and Load every material in
    ``paths`` into it (one multiparm entry each). Returns ``(node, failed)`` where
    ``failed`` is a list of ``"path: error"`` strings.
    """
    edit = _insert_lop_after(source, "editmaterial",
                             name or "edit_{}".format(source.name()), inline=inline)
    edit.parm("materials").set(len(paths))
    failed = []
    for i, path in enumerate(paths, start=1):
        edit.parm("enable{}".format(i)).set(1)
        edit.parm("matpath{}".format(i)).set(path)
        try:
            edit.parm("load{}".format(i)).pressButton()
        except hou.OperationFailed as err:
            failed.append("{}: {}".format(path, err))
            continue
        if not edit.parm("matnode{}".format(i)).evalAsString():
            failed.append("{}: Load produced no material VOP".format(path))
    return edit, failed


def run_edit_material():
    """Shelf entry: select a LOP, get an Edit Material Network downstream of it with
    every MaterialX material on its stage already Loaded.
    """
    source = _resolve_selected_lop()
    if source is None:
        hou.ui.displayMessage(
            "Select the LOP whose materials you want to edit, then run again.",
            severity=hou.severityType.Warning, title=_EDITMAT_TITLE,
        )
        return

    mats = _pick_stage_materials(source, _EDITMAT_TITLE, "load into the Edit Material Network")
    if not mats:
        return

    paths = [p.GetPath().pathString for p in mats]
    with hou.undos.group("Stage -> Edit Material"):
        edit, failed = build_edit_material(source, paths)

    msg = "Created {} with {} material(s) loaded.".format(
        edit.path(), len(paths) - len(failed))
    if failed:
        msg += "\n\nFAILED {}:\n".format(len(failed)) + "\n".join("  " + f for f in failed)
    hou.ui.displayMessage(msg, title=_EDITMAT_TITLE)
    return edit


# ---------------------------------------------------------------------------
# Live-stage -> native materials that REPLACE the originals (one step)
# ---------------------------------------------------------------------------

_TAKEOVER_TITLE = "Stage -> Native (Replace)"


def unused_material_paths(stage, keep=()):
    """Material prims on ``stage`` that nothing binds and that are not in ``keep``.

    A material counts as used if any ``material:binding`` relationship (direct,
    purpose-qualified, or collection-based) on any prim targets it. Returns sorted paths.
    """
    keep = set(keep)
    used = set()
    for prim in stage.Traverse():
        for rel in prim.GetRelationships():
            if not rel.GetName().startswith("material:binding"):
                continue
            for t in rel.GetTargets():
                used.add(t.GetPrimPath().pathString)
    unused = []
    for prim in stage.Traverse():
        if prim.GetTypeName() != "Material":
            continue
        path = prim.GetPath().pathString
        if path in keep or path in used:
            continue
        unused.append(path)
    return sorted(unused)


def add_prune_lop(source, paths, name=None, inline=True):
    """Insert a ``prune`` LOP (method Deactivate) below ``source`` targeting ``paths``."""
    prune = _insert_lop_after(source, "prune", name or "prune_old_materials", inline=inline)
    prune.parm("method").set("deactivate")
    prune.parm("num_rules").set(1)
    prune.parm("enable1").set(1)
    prune.parm("primpattern1").set(" ".join(paths))
    prune.setComment("Old materials nothing binds, and the old shaders inside the materials "
                     "{} rebuilt (nscr_catalog_import)".format(source.name()))
    return prune


def build_native_override_library(source, mats, name=None, inline=True,
                                  prune_unused=False):
    """Create a ``materiallibrary`` LOP fed by ``source`` and rebuild every material prim
    in ``mats`` inside it as a native MaterialX Builder, authored back at the ORIGINAL
    prim path. The library's opinion is stronger than the referenced one, so existing
    bindings pick up the native material with no re-assignment and no Edit Material.

    With ``prune_unused`` a Prune LOP (Deactivate) is added below the library that
    removes every other Material prim nothing binds -- e.g. the MDL originals left behind
    by an MDL->MaterialX conversion, which otherwise double up the scene graph tree.

    Returns ``(matlib, built, failed, pruned)``; ``failed`` is a list of ``"path: error"``
    and ``pruned`` the list of deactivated material paths (empty if not pruning).
    """
    # Build with cooking paused: every multiparm edit below would otherwise recook the
    # whole downstream stage (minutes for ~90 materials instead of seconds).
    # Read before building: the source stage may be recooked (and these prims expire).
    old_children = [c.GetPath().pathString for prim in mats for c in prim.GetChildren()]

    prev_mode = hou.updateModeSetting()
    hou.setUpdateMode(hou.updateMode.Manual)
    try:
        matlib, built, failed = _build_native_override_library(source, mats, name, inline)
    finally:
        hou.setUpdateMode(prev_mode)

    # Pruning needs the library's cooked stage, so it runs after cooking is back on.
    pruned = []
    if prune_unused:
        matlib.cook(force=True)
        stage = matlib.stage()
        if stage is None:
            raise RuntimeError("{} did not produce a stage; cannot work out which old "
                               "materials are unused".format(matlib.path()))
        keep = [prim.GetPath().pathString for prim in mats]
        pruned = unused_material_paths(stage, keep=keep)
        # The old shaders and NodeGraphs inside each rebuilt material: the library can
        # only add to a referenced prim, never remove, so they are deactivated here.
        pruned.extend(old_children)
        if pruned:
            add_prune_lop(matlib, pruned, inline=inline)
    return matlib, built, failed, pruned


def _build_native_override_library(source, mats, name, inline):
    matlib = _insert_lop_after(source, "materiallibrary",
                               name or "native_{}".format(source.name()), inline=inline)
    matlib.parm("matnet").set(".")
    matlib.parm("materials").set(0)

    built = []
    failed = []
    for prim in mats:
        path = prim.GetPath().pathString
        try:
            subnet = build_material_from_prim(
                prim, matlib, name=prim.GetName(),
                source_note="source: {} (live stage)\nprim:   {}\n"
                            "authored back at the same path (replaces the original)".format(
                                source.path(), path),
                avoid_names={c.GetName() for c in prim.GetChildren()},
            )
        except (RuntimeError, hou.OperationFailed) as err:
            failed.append("{}: {}".format(path, err))
            continue
        i = matlib.parm("materials").eval() + 1
        matlib.parm("materials").set(i)
        matlib.parm("enable{}".format(i)).set(1)
        matlib.parm("matflag{}".format(i)).set(0)
        matlib.parm("matnode{}".format(i)).set(subnet.name())
        matlib.parm("matpath{}".format(i)).set(path)
        built.append(subnet)
    return matlib, built, failed


def run_stage_takeover():
    """Shelf entry: select a LOP, get a Material Library downstream of it holding native
    MaterialX Builders that replace the referenced materials in place.
    """
    source = _resolve_selected_lop()
    if source is None:
        hou.ui.displayMessage(
            "Select the LOP whose materials you want to take over, then run again.",
            severity=hou.severityType.Warning, title=_TAKEOVER_TITLE,
        )
        return

    mats = _pick_stage_materials(source, _TAKEOVER_TITLE, "rebuild as native")
    if not mats:
        return

    choice = hou.ui.displayMessage(
        "Remove old materials and old shaders?\n\nAfter the takeover, other Material "
        "prims with no binding (for example the MDL originals next to their _mtlx "
        "conversions) and the old shaders and NodeGraphs left inside each rebuilt "
        "material are dead weight that doubles up the scene graph tree. \"Prune\" "
        "adds a Prune LOP (Deactivate) below the library for them.",
        buttons=("Prune unused", "Keep everything", "Cancel"),
        default_choice=0, close_choice=2, title=_TAKEOVER_TITLE,
    )
    if choice == 2:
        return
    prune_unused = choice == 0

    with hou.undos.group("Stage -> Native (Replace)"):
        matlib, built, failed, pruned = build_native_override_library(
            source, mats, prune_unused=prune_unused)

    msg = ("Created {} with {} native material(s).\nEach is authored at its original "
           "prim path, so existing bindings now use the native version.").format(
               matlib.path(), len(built))
    if prune_unused:
        msg += "\n\nPruned {} old material(s) and shader(s) via {}.".format(
            len(pruned), "a Prune LOP below the library" if pruned else "nothing (none found)")
    if failed:
        msg += "\n\nFAILED {}:\n".format(len(failed)) + "\n".join("  " + f for f in failed)
    hou.ui.displayMessage(msg, title=_TAKEOVER_TITLE)
    return matlib


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

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
