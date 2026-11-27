# Default on every new USD Render ROP: husk-side post-render script converting
# EXR -> ACES SDR PNG (+ mp4 for sequences). Runs inside husk so it fires for
# scheduler/background/blocking renders alike. Logic: scripts/python/
# nscr_husk_post_convert.py (paths from stage RenderProducts).
node = kwargs["node"]
if not node.evalParm("husk_postrender"):
    node.parm("husk_tpostrender").set(1)
    node.parm("husk_postrender").set(
        "Z:/Houdini/packages/houdini-nscr-tools/scripts/python/nscr_husk_post_convert.py")
