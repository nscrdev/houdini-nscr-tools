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


def _existing_fields(cu):
    """Map existing cableunpack field name -> output index (field order - 1)."""
    n = cu.parm("fields").eval()
    return {cu.parm("fieldname%d" % i).eval(): i - 1 for i in range(1, n + 1)}


def _ensure_channel(mat, cu, field, arity, is_color, field_map):
    """Create whatever is missing for one channel (field / null tap / ROP).

    Returns True if anything was created. Existing nodes are left untouched, and
    new cableunpack fields are appended so existing output->null taps don't shift.
    """
    parent = mat.parent()
    prefix = mat.name()
    title = _title(field)
    null_name = prefix + "_Unpack_" + title
    rop_name = prefix + "_" + title

    nl = parent.node(null_name)
    rop = parent.node(rop_name)
    if nl is not None and rop is not None:
        return False  # already fully built

    # ensure a cableunpack field exists for this channel (append if new)
    if field in field_map:
        out_index = field_map[field]
    else:
        new = cu.parm("fields").eval() + 1
        cu.parm("fields").set(new)
        cu.parm("fieldname%d" % new).set(field)
        ft = cu.parm("fieldtype%d" % new).parmTemplate().menuItems()
        cu.parm("fieldtype%d" % new).set(ft.index("vector" if arity == "vector" else "float"))
        out_index = new - 1
        field_map[field] = out_index

    mpos = mat.position()
    ystep = -1.2 * out_index

    if nl is None:
        nl = parent.createNode("null", null_name)
        nl.setInput(0, cu, out_index)
        if nl.parm("outputs") is not None:
            nl.parm("outputs").set(1)
        nl.setColor(hou.Color((0.6, 0.7, 0.77)))
        nl.setDisplayFlag(False)
        nl.setPosition(mpos + hou.Vector2(6.0, ystep))

    if rop is None:
        rop = parent.createNode("rop_image", rop_name)
        cc_items = rop.parm("colorconversion").parmTemplate().menuItems()
        rop.parm("coppath").set("../" + null_name)
        rop.parm("useport1").set(True)
        rop.parm("aov1").set("output1")
        rop.parmTuple("res").set((1024, 1024))
        rop.parm("f1").set(1)
        rop.parm("f2").set(120)
        rop.parm("f3").set(1)
        if is_color:
            ext = "png"
            rop.parm("colorconversion").set(cc_items.index("bakeocio"))
            rop.parm("size1").set("int16")   # 16-bit integer PNG (avoid 8-bit banding)
        else:
            ext = "exr"
            rop.parm("colorconversion").set(cc_items.index("raw"))
            rop.parm("size1").set("float16")  # 16-bit float EXR
            rop.parm("vm_image_exr_compression").set("piz")  # lossless, smallest on grainy data
        rop.parm("ociocolorspace").set("ACEScg")
        rop.parm("ociodisplay").set("sRGB - Display")
        rop.parm("ocioview").set("ACES 1.0 - SDR Video")
        rop.parm("copoutput").set(_output_path(ext))
        rop.setColor(hou.Color((0.65, 0.4, 0.5)))
        rop.setPosition(mpos + hou.Vector2(9.0, ystep))

    return True


def _build_for_material(mat):
    parent = mat.parent()
    prefix = mat.name()
    unpack_name = prefix + "_Unpack"

    active = _active_channels(mat)
    if not active:
        return "skipped (no active channels): " + prefix

    # create the cable unpack on first run; reuse it on later (incremental) runs
    cu = parent.node(unpack_name)
    fresh = cu is None
    if fresh:
        cu = parent.createNode("cableunpack", unpack_name)
        cu.setInput(0, mat, 1)  # output 1 = "material" cable (output 0 is "geo")
        cu.parm("fields").set(0)
        cu.setDisplayFlag(False)
        cu.setPosition(mat.position() + hou.Vector2(3.0, 0.0))

    field_map = _existing_fields(cu)
    added = [f for (f, a, c) in active
             if _ensure_channel(mat, cu, f, a, c, field_map)]

    if fresh:
        return "built {} ({}): {}".format(prefix, len(added), ", ".join(added))
    if added:
        return "updated {} (+{}): {}".format(prefix, len(added), ", ".join(added))
    return "up to date: " + prefix


def run():
    sel = [n for n in hou.selectedNodes() if n.type().name() == "previewmaterial"]
    if not sel:
        hou.ui.displayMessage("Select one or more Preview Material COP nodes first.")
        return
    results = [_build_for_material(m) for m in sel]
    hou.ui.displayMessage("Material exports:\n\n" + "\n".join(results))
