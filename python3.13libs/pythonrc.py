"""nscr-tools Python startup (runs once per Houdini process).

Attaches helpers to the hou module itself so expressions can call them with
no nested quotes, e.g. `pythonexprs("hou.flipcam()")`. hou.session is not
used because it is rebuilt from the hip file on every scene load; attributes
on hou persist for the whole process.
"""

import os
import sys

import hou

# pythonrc.py is exec'd by Houdini without __file__, so locate this
# package's scripts/python dir by scanning HOUDINI_PATH instead.
for _root in (hou.getenv("HOUDINI_PATH") or "").split(";"):
    _scripts = os.path.normpath(os.path.join(_root, "scripts", "python"))
    if (os.path.isfile(os.path.join(_scripts, "nscr_flipbook.py"))
            and _scripts not in sys.path):
        sys.path.insert(0, _scripts)
        break

import nscr_flipbook

hou.flipcam = nscr_flipbook.flipcam
