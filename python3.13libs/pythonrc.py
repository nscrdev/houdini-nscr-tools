"""nscr-tools Python startup (runs once per Houdini process).

Attaches helpers to the hou module itself so expressions can call them with
no nested quotes, e.g. `pythonexprs("hou.flipcam()")`. hou.session is not
used because it is rebuilt from the hip file on every scene load; attributes
on hou persist for the whole process.
"""

import os
import sys

import hou

# Houdini normally puts <package>/scripts/python on sys.path already. Some
# builds (Houdini 22) run pythonrc.py without defining __file__, so only fall
# back to a path relative to this file when __file__ is available.
try:
    _scripts = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "python"))
except NameError:
    _scripts = None
if _scripts and _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import nscr_flipbook

hou.flipcam = nscr_flipbook.flipcam
