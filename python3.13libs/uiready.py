"""Warm Houdini's viewport OCIO view transforms at startup (NSCR).

Install location: Z:/Houdini/packages/houdini-nscr-tools/python3.13libs/uiready.py
(loaded via the NSCR_Tools package; runs automatically once the UI is ready,
interactive sessions only).

Houdini builds each OCIO view's GPU transform lazily on first use and caches
it for the session, so the first switch to every view stalls. This flips the
Scene Viewer through all ARRI REVEAL views once, one per event-loop tick
(hou.GeometryViewport.draw() calls merge within a single script, so a plain
loop would warm only one view), then restores the original view.

Off by default: set NSCR_OCIO_WARMUP=1 to enable (the NSCR_Tools package
JSON sets it to 0).
"""
import os

import hou

_state = {"sv": None, "pairs": None, "orig": None, "tries": 0}
_DISPLAY = "sRGB - Display"


def _warmup_tick():
    st = _state
    sv = st["sv"] or hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if sv is None:
        st["tries"] += 1
        if st["tries"] > 100:  # no scene viewer after ~5 s; give up
            hou.ui.removeEventLoopCallback(_warmup_tick)
        return
    st["sv"] = sv

    if st["pairs"] is None:  # first tick with a viewer: snapshot + plan
        if "arri-reveal" not in hou.Color.ocio_configPath().replace("\\", "/"):
            hou.ui.removeEventLoopCallback(_warmup_tick)
            return
        st["orig"] = (sv.usingOCIO(), sv.getOCIODisplay(), sv.getOCIOView())
        st["pairs"] = [(_DISPLAY, v) for v in hou.Color.ocio_views(_DISPLAY)
                       if v.startswith(("ARRI REVEAL", "REVEAL "))]
        sv.setUsingOCIO(True)
        return

    if st["pairs"]:  # warm one view per event-loop tick
        display, view = st["pairs"].pop(0)
        try:
            sv.setOCIODisplayView(display, view)
            for vp in sv.viewports():
                vp.draw()
            hou.ui.postRedrawFence()
        except hou.Error:
            pass
        return

    # done: restore original state and unregister
    enabled, disp, view = st["orig"]
    try:
        if disp:
            sv.setOCIODisplayView(disp, view)
        sv.setUsingOCIO(enabled)
        for vp in sv.viewports():
            vp.draw()
    except hou.Error:
        pass
    hou.ui.removeEventLoopCallback(_warmup_tick)
    print("[nscr] OCIO REVEAL view warm-up complete")


if hou.isUIAvailable() and os.environ.get("NSCR_OCIO_WARMUP", "0") == "1":
    hou.ui.addEventLoopCallback(_warmup_tick)
