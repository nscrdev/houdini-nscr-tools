"""Increment and Save for the current hip file (NSCR).

Port of the Axis tools `ver.versioning.incsave()`, made self-contained so it
does not need the Axis package.

Rules (same as Axis):
* The version token is `_v` or `_V` followed by digits (scene_v012.hip).
* The new version is one above the highest version of the same file name
  that exists in the hip folder, so it never overwrites a newer file.
* Zero padding follows the current file name.
* A file with no version gets `_v001` (scene.hip -> scene_v001.hip).
"""

import os
import re

import hou

_FORMATS = ("_v", "_V")


def _natural_key(text):
    return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", text)]


def _split_version(name, formats):
    """Return ([prefix, digits, suffix], fmt), or (None, None) if no version."""
    for fmt in formats:
        if re.search(r"%s(?=\d+)" % fmt, name):
            head, tail = re.split(r"%s(?=\d+)" % fmt, name, maxsplit=1)
            digits = re.match(r"\d+", tail).group(0)
            return [head, digits, tail[len(digits):]], fmt
    return None, None


def _scan_folder(folder, versions, formats, sec):
    """Add versions of the same file name found on disk. Returns (versions, padding, fmt)."""
    padding = 3
    fmt = "_v"
    for f in os.listdir(folder):
        found, ffmt = _split_version(f, formats)
        if found and found[0] == sec[0] and found[2] == sec[2]:
            versions.append(found[1])
            padding = len(found[1])
            fmt = ffmt
    versions = sorted(set(versions), key=_natural_key, reverse=True)
    return versions, padding, fmt


def next_path():
    """Return the path the next increment save will write to."""
    folder, name = hou.hipFile.path().rsplit("/", 1)

    sec, fmt = _split_version(name, _FORMATS)
    if sec:
        padding = len(sec[1])
        versions = _scan_folder(folder, [sec[1]], [fmt], sec)[0]
    else:
        base, ext = os.path.splitext(name)
        sec = [base, "", ext]
        versions, padding, fmt = _scan_folder(folder, ["000"], list(_FORMATS), sec)

    new = str(max(int(v) for v in versions) + 1).zfill(padding)
    return "%s/%s%s%s%s" % (folder, sec[0], fmt, new, sec[2])


def incsave():
    """Save the hip file as the next version."""
    path = next_path()
    hou.hipFile.save(path, True)
    print("Increment save: %s" % path)
    return path


run = incsave
