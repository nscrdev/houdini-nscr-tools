"""Flipbook helpers for nscr-tools.

flipcam() returns the name of the camera the active Scene Viewer is looking
through, for use in flipbook output paths via a backtick expression:

    $HIP/flipbook/$HIPNAME/$HIPNAME.`pythonexprs("__import__('nscr_flipbook').flipcam()")`.$F4.jpg

Imported directly in the expression rather than via hou.session, because
hou.session is rebuilt from the hip file on every scene load and would drop
any runtime-registered helpers.

Context-aware: prefers the viewer on the active pane tab, and works in both
OBJ viewports (camera nodes) and Solaris/LOP viewports (USD camera prims),
since cameraPath() covers both. Falls back to 'nocam' when no viewer is
looking through a camera (e.g. free perspective view).
"""

import hou


_DEBUG_LOG = None  # set to a file path to trace evaluation during flipbooks


def flipcam():
    """Name of the active viewport camera, or 'nocam' if not looking through one."""
    try:
        viewers = [p for p in hou.ui.paneTabs()
                   if p.type() == hou.paneTabType.SceneViewer]
        active = [v for v in viewers if v.isCurrentTab()]
        background = [v for v in viewers if not v.isCurrentTab()]
        for viewer in active + background:
            path = viewer.curViewport().cameraPath()
            if path:
                name = path.rstrip("/").rsplit("/", 1)[-1]
                _log("ok: %s" % name)
                return name
        _log("no camera on any viewer")
        return "nocam"
    except Exception as exc:
        _log("EXC: %r" % exc)
        return "nocam"


def _log(msg):
    if _DEBUG_LOG:
        with open(_DEBUG_LOG, "a") as f:
            f.write(msg + "\n")
