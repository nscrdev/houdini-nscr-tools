# Default on every new USD Render ROP: husk-side post-render script converting
# EXR -> ACES SDR PNG (+ mp4 for sequences). Runs inside husk so it fires for
# scheduler/background/blocking renders alike. Logic: scripts/python/
# nscr_husk_post_convert.py (paths from stage RenderProducts).
#
# The script path comes from wherever this package is installed: $NSCR_TOOLS
# when the package JSON sets it (kept as a variable so .hip files stay
# portable between machines), otherwise the file found on HOUDINI_PATH.
# If neither finds it, the parm is left alone rather than set to a dead path.
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
