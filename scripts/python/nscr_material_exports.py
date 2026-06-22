"""
Build texture-export chains downstream of COP Preview Material nodes.

For each SELECTED `previewmaterial` COP, detect which channels are "active"
(input wired OR a default_* parm changed from factory), then build, in the
exact style of the hand-made CHIP example:

    <MAT>_Unpack                 cableunpack with one field per active channel
    <MAT>_Unpack_<Channel>       one null tap per field
    <MAT>_<Channel>              one rop_image per channel

Color channels (basecolor + all *_color tints) -> PNG, "Bake to OCIO Display/View"
(display tonemapping). All other channels (metalness, roughness, normal, ...) ->
EXR, "Raw". Output path/res/frame-range mirror an existing sibling rop_image when
one is present, otherwise fall back to a $HIP-based default.

Behavior: operates on the SELECTED preview material(s); SKIPS any material that
already has a <MAT>_Unpack node.

Usage (shelf tool):
    import importlib, nscr_material_exports
    importlib.reload(nscr_material_exports)
    nscr_material_exports.run()
"""

import hou

# ---------------------------------------------------------------------------
# Preview Material channel schema (fixed node type, stable across versions).
# (cable_field_name, default_parm_base, arity, is_color)
#   cable_field_name : name the cableunpack field must use (== input label)
#   default_parm_base: parmTuple name holding the channel default (None = input-only)
#   arity            : 'vector' (RGB) or 'float' (Mono) -> cableunpack field type
#   is_color         : True -> display-tonemapped PNG ; False -> raw EXR
# ---------------------------------------------------------------------------
CHANNELS = [
    ("basecolor",          "default_basecolor",           "vector", True),
    ("metalness",          "default_metalness",           "float",  False),
    ("specular",           "default_specular",            "float",  False),
    ("spec_color",         "default_spec_color",          "vector", True),
    ("roughness",          "default_specular_roughness",  "float",  False),
    ("coat",               "default_coat_amount",         "float",  False),
    ("coat_color",         "default_coat_color",          "vector", True),
    ("coat_roughness",     "default_coat_roughness",      "float",  False),
    ("sheen",              "default_sheen_amount",        "float",  False),
    ("sheen_color",        "default_sheen_color",         "vector", True),
    ("sheen_roughness",    "default_sheen_roughness",     "float",  False),
    ("emission",           "default_emission_amount",     "float",  False),
    ("emission_color",     "default_emission_color",      "vector", True),
    ("opacity",            "default_opacity_amount",       "float",  False),
    ("normal",             None,                          "vector", False),
    ("height",             None,                          "float",  False),
    ("transmission",       "default_transmission_amount", "float",  False),
    ("transmission_color", "default_transmission_color",  "vector", True),
    ("sss_amount",         "default_sss_amount",          "float",  False),
    ("sss_color",          "default_sss_color",           "vector", True),
    ("sss_radius",         "default_sss_radius",          "vector", False),
]

_TOL = 1e-6


def _title(field_name):
    """basecolor -> Basecolor, spec_color -> Spec_color (matches CHIP naming)."""
    return field_name[:1].upper() + field_name[1:]


def _input_index_by_label(mat):
    """Map input label -> connector index for this preview material."""
    return {lbl: i for i, lbl in enumerate(mat.inputLabels())}


def _default_changed(mat, base):
    """True if any component of the default_* parmTuple differs from factory."""
    if not base:
        return False
    pt = mat.parmTuple(base)
    if pt is None:
        return False
    defaults = pt.parmTemplate().defaultValue()
    for p, d in zip(pt, defaults):
        v = p.eval()
        if isinstance(v, float) or isinstance(d, float):
            if abs(float(v) - float(d)) > _TOL:
                return True
        elif v != d:
            return True
    return False


def _active_channels(mat):
    """Ordered list of (field, arity, is_color) channels to export."""
    idx_by_label = _input_index_by_label(mat)
    inputs = mat.inputs()
    active = []
    for field, base, arity, is_color in CHANNELS:
        connected = False
        if field in idx_by_label:
            ii = idx_by_label[field]
            connected = ii < len(inputs) and inputs[ii] is not None
        if connected or _default_changed(mat, base):
            active.append((field, arity, is_color))
    return active


def _output_path(ext):
    # Matches the CHIP convention; $OS resolves to the rop node name (<MAT>_<Channel>).
    return "$HIP/mat/$HIPNAME/$HIPNAME.$OS.$F4.{}".format(ext)


def _build_for_material(mat):
    parent = mat.parent()
    prefix = mat.name()
    unpack_name = prefix + "_Unpack"

    if parent.node(unpack_name) is not None:
        return "skipped (already built): " + prefix

    active = _active_channels(mat)
    if not active:
        return "skipped (no active channels): " + prefix

    cc_items = None

    # --- cable unpack -------------------------------------------------------
    cu = parent.createNode("cableunpack", unpack_name)
    cu.setInput(0, mat)
    cu.parm("fields").set(len(active))
    ft_items = cu.parm("fieldtype1").parmTemplate().menuItems()
    type_idx = {"vector": ft_items.index("vector"), "float": ft_items.index("float")}
    for i, (field, arity, _is_color) in enumerate(active, start=1):
        cu.parm("fieldname%d" % i).set(field)
        cu.parm("fieldtype%d" % i).set(type_idx[arity])
    cu.setDisplayFlag(False)
    mpos = mat.position()
    cu.setPosition(mpos + hou.Vector2(3.0, 0.0))

    for out_i, (field, _arity, is_color) in enumerate(active):
        ch_title = _title(field)
        ystep = -1.2 * out_i

        # --- null tap -------------------------------------------------------
        nl = parent.createNode("null", prefix + "_Unpack_" + ch_title)
        nl.setInput(0, cu, out_i)
        if nl.parm("outputs") is not None:
            nl.parm("outputs").set(1)
        nl.setColor(hou.Color((0.6, 0.7, 0.77)))
        nl.setDisplayFlag(False)
        nl.setPosition(mpos + hou.Vector2(6.0, ystep))

        # --- rop image ------------------------------------------------------
        rop_name = prefix + "_" + ch_title
        rop = parent.createNode("rop_image", rop_name)
        if cc_items is None:
            cc_items = rop.parm("colorconversion").parmTemplate().menuItems()

        rop.parm("coppath").set("../" + prefix + "_Unpack_" + ch_title)
        rop.parm("useport1").set(True)
        rop.parm("aov1").set("output1")
        rop.parmTuple("res").set((1024, 1024))
        rop.parm("f1").set(1)
        rop.parm("f2").set(120)
        rop.parm("f3").set(1)

        if is_color:
            ext = "png"
            rop.parm("colorconversion").set(cc_items.index("bakeocio"))
        else:
            ext = "exr"
            rop.parm("colorconversion").set(cc_items.index("raw"))
        rop.parm("ociocolorspace").set("ACEScg")
        rop.parm("ociodisplay").set("sRGB - Display")
        rop.parm("ocioview").set("ACES 1.0 - SDR Video")
        rop.parm("copoutput").set(_output_path(ext))

        rop.setColor(hou.Color((0.65, 0.4, 0.5)))
        rop.setPosition(mpos + hou.Vector2(9.0, ystep))

    fields = ", ".join(f for f, _a, _c in active)
    return "built {} ({}): {}".format(prefix, len(active), fields)


def run():
    sel = [n for n in hou.selectedNodes() if n.type().name() == "previewmaterial"]
    if not sel:
        hou.ui.displayMessage("Select one or more Preview Material COP nodes first.")
        return
    results = [_build_for_material(m) for m in sel]
    hou.ui.displayMessage("Material exports:\n\n" + "\n".join(results))
