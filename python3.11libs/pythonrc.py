"""nscr-tools Python startup (runs once per Houdini process).

Attaches helpers to the hou module itself so expressions can call them with
no nested quotes, e.g. `pythonexprs("hou.flipcam()")`. hou.session is not
used because it is rebuilt from the hip file on every scene load; attributes
on hou persist for the whole process.
"""

import os
import sys

import hou

_scripts = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "python"))
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import nscr_flipbook

hou.flipcam = nscr_flipbook.flipcam
