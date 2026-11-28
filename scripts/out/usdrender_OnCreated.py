# Same husk-side post-render default for the /out-context USD Render ROP.
# See scripts/lop/usdrender_rop_OnCreated.py for how the path is resolved.
import os

import hou

_REL = "scripts/python/nscr_husk_post_convert.py"


def _post_render_script():
    root = os.environ.get("NSCR_TOOLS")
    if root and os.path.isfile(os.path.join(root, _REL)):
        return "$NSCR_TOOLS/" + _REL
    found = hou.findFile(_REL) if hasattr(hou, "findFile") else None
    return found.replace("\\", "/") if found else None


node = kwargs["node"]
if not node.evalParm("husk_postrender"):
    script = _post_render_script()
    if script:
        node.parm("husk_tpostrender").set(1)
        node.parm("husk_postrender").set(script)
