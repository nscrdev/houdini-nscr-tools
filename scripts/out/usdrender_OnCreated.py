# Same husk-side post-render default for the /out-context USD Render ROP.
node = kwargs["node"]
if not node.evalParm("husk_postrender"):
    node.parm("husk_tpostrender").set(1)
    node.parm("husk_postrender").set(
        "Z:/Houdini/packages/houdini-nscr-tools/scripts/python/nscr_husk_post_convert.py")
