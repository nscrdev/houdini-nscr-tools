"""husk --postrender-script: EXR -> ACES SDR PNG (+ mp4 for sequences).

Runs inside husk after ALL frames complete, so it fires no matter how the
render was launched (H22 render scheduler, background, blocking, or even if
the Houdini session closes mid-render). Wire it via the USD Render ROP's
Husk tab > Post-Render Script parm; the OnCreated hooks set it by default
on new ROPs.

husk executes this with a `stage` global; the rendered EXR paths come from
the stage's RenderProduct productName attributes (time samples cover
sequences). Grading lives in nscr_post_convert (same folder).
"""
import os
import sys
import traceback


def _module_dir():
    """Find this file's folder. husk execs it without __file__, so fall back
    to the code object's filename, then $NSCR_TOOLS, then HOUDINI_PATH."""
    candidates = []
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    try:
        import inspect
        fname = inspect.currentframe().f_code.co_filename
        if fname and not fname.startswith("<"):
            candidates.append(os.path.dirname(os.path.abspath(fname)))
    except Exception:
        pass
    root = os.environ.get("NSCR_TOOLS")
    if root:
        candidates.append(os.path.join(root, "scripts", "python"))
    for entry in os.environ.get("HOUDINI_PATH", "").split(os.pathsep):
        entry = entry.strip()
        if entry and entry != "&":
            candidates.append(os.path.join(entry, "scripts", "python"))
    for d in candidates:
        if os.path.isfile(os.path.join(d, "nscr_post_convert.py")):
            return d
    return None


_here = _module_dir()
if _here and _here not in sys.path:
    sys.path.insert(0, _here)


def _exr_paths(stage):
    paths = []
    for prim in stage.Traverse():
        if prim.GetTypeName() != "RenderProduct":
            continue
        attr = prim.GetAttribute("productName")
        if not attr:
            continue
        times = attr.GetTimeSamples()
        vals = [attr.Get(t) for t in times] if times else [attr.Get()]
        for v in vals:
            v = str(v) if v else ""
            if v.lower().endswith(".exr") and v not in paths:
                paths.append(v)
    return paths


def main():
    import nscr_post_convert as pc
    hfs = os.environ.get("HFS") or os.path.join(os.path.dirname(sys.executable), "..")
    hoiiotool = pc.hoiiotool_path(hfs)
    exrs = [p for p in _exr_paths(stage) if os.path.exists(p)]
    if not exrs:
        print("[post_convert/husk] no rendered EXRs found on stage; nothing to convert")
        return
    pngs = []
    for exr in exrs:
        png = pc._make_png(hoiiotool, exr, pc.DEFAULT_COLORCONFIG)
        if png:
            pngs.append(png)
    print("[post_convert/husk] %d/%d PNGs written (%s)"
          % (len(pngs), len(exrs), pc.OIIO_VIEW))
    if len(pngs) > 1:
        mp4 = pc._make_mp4(pngs, stage.GetTimeCodesPerSecond() or 24.0)
        if mp4:
            print("[post_convert/husk] mp4: %s" % mp4)


try:
    main()
except Exception:
    traceback.print_exc()   # never fail the render over a conversion problem
