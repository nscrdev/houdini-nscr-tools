"""
Version-up the textures referenced by selected MaterialX image or COP file nodes.

Nuke-style "alt + up": select one or more MaterialX image nodes inside a MaterialX
builder (e.g. /stage/materiallibrary1/chip/basecolor) and/or COP file nodes (e.g.
/obj/copnet1/file1), run the tool, and each node's file parm is bumped to the
LATEST version that exists on disk. A summary dialog reports what changed.

Version tokens are detected anywhere in the path -- in a folder (.../v003/...) OR
in the filename (..._v003.exr), or both. Frame patterns ($F, $F4, %04d, ####) are
masked out so a sequence still counts as present. The original parm string format
is preserved: a relative path stays relative, an absolute path stays absolute, and
zero-padding width is kept (v3 -> v12, v003 -> v012).

Usage (shelf tool):
    import importlib, nscr_texture_version_up
    importlib.reload(nscr_texture_version_up)
    nscr_texture_version_up.run()
"""

import glob
import os
import re

import hou

# A version token: a "v" (either case) not preceded by an alnum char, then digits.
# Capture groups: 1 = the "v"/"V" letter, 2 = the digits.
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])([vV])(\d+)")

# Frame-sequence patterns to mask with "*" before globbing for existence.
_FRAME_PATTERNS = [
    re.compile(r"\$F\d*"),         # $F, $F4
    re.compile(r"%0?\d*d"),        # %04d, %d
    re.compile(r"#+"),             # ####
    re.compile(r"<UDIM>", re.I),   # UDIM tile token
    re.compile(r"%\(UDIM\)d"),     # %(UDIM)d
]

# A literal frame/UDIM number sitting just before the extension (e.g. ".0001.png").
# The actual frame can differ between versions, so mask it too when scanning disk;
# capture group 1 = the frame digits, used to adopt the new version's real frame.
_LITERAL_FRAME_RE = re.compile(r"\.(\d+)(?=\.[^.\\/]+$)")


# Parm names that hold a texture path, in priority order:
#   file       -> MaterialX image nodes (mtlximage / mtlxtiledimage)
#   filename   -> Copernicus COP `file` node
#   filename1  -> classic COP2 `file` node
_FILE_PARM_NAMES = ("file", "filename", "filename1")


def _file_parm(node):
    """Return the texture-file parm of a MaterialX image or COP file node.

    Accepts MaterialX image nodes (parm `file`) and COP file nodes (parm
    `filename` / `filename1`). Returns None for anything else.
    """
    if node is None:
        return None
    type_name = node.type().name()
    if not (type_name.startswith("mtlx") or type_name == "file"):
        return None
    for name in _FILE_PARM_NAMES:
        parm = node.parm(name)
        if parm is not None:
            return parm
    return None


def _to_disk_pattern(raw):
    """Expand vars and mask frame tokens so the path can be globbed on disk.

    Returns an absolute glob-ready string, or None if it can't be resolved.
    The version tokens are left intact here (the caller replaces them).
    """
    expanded = hou.expandString(raw)
    for pat in _FRAME_PATTERNS:
        expanded = pat.sub("*", expanded)
    # Mask a literal trailing frame number (frame can differ between versions).
    expanded = _LITERAL_FRAME_RE.sub(".*", expanded)
    if not os.path.isabs(expanded):
        # Houdini resolves relative texture paths against $HIP.
        expanded = os.path.join(hou.expandString("$HIP"), expanded)
    return os.path.normpath(expanded)


def _latest_version(raw):
    """Find the highest on-disk version for a path that contains version tokens.

    Returns (latest_int, current_int, latest_files) or None if there is no
    version token. `current_int` is the max version currently in the string;
    `latest_files` are the disk paths matching `latest_int` (used to adopt the
    new version's real frame).
    """
    tokens = list(_VERSION_RE.finditer(raw))
    if not tokens:
        return None
    current = max(int(m.group(2)) for m in tokens)

    disk = _to_disk_pattern(raw)
    if disk is None:
        return (current, current, [])

    # Build a glob by replacing every version token's digits with "*".
    glob_pat = _VERSION_RE.sub(lambda m: m.group(1) + "*", disk)

    by_version = {}
    for hit in glob.glob(glob_pat):
        nums = [int(m.group(2)) for m in _VERSION_RE.finditer(hit)]
        if nums:
            by_version.setdefault(max(nums), []).append(hit)

    latest = max([current] + list(by_version.keys()))
    return (latest, current, by_version.get(latest, []))


def _bump_string(raw, new_version):
    """Replace every version token in `raw` with `new_version`, preserving the
    letter case and zero-padding width of each token."""
    def repl(m):
        letter, digits = m.group(1), m.group(2)
        width = max(len(digits), len(str(new_version)))
        return "{}{:0{}d}".format(letter, new_version, width)
    return _VERSION_RE.sub(repl, raw)


def _has_frame_token(raw):
    """True if the path uses a frame/UDIM token ($F4, %04d, ####, <UDIM>) that
    should be left untouched rather than adopting a literal frame number."""
    return any(pat.search(raw) for pat in _FRAME_PATTERNS)


def _new_value(raw, latest, latest_files):
    """Build the bumped parm string. Version tokens go to `latest`. If the path
    carries a LITERAL frame number (no frame token), adopt the actual frame from
    the new version's file on disk (lowest, if several) so the path resolves."""
    bumped = _bump_string(raw, latest)
    if _has_frame_token(raw) or not _LITERAL_FRAME_RE.search(raw) or not latest_files:
        return bumped

    frames = [m.group(1) for m in (_LITERAL_FRAME_RE.search(f) for f in latest_files) if m]
    if not frames:
        return bumped
    chosen = min(frames, key=int)
    return _LITERAL_FRAME_RE.sub("." + chosen, bumped)


def run():
    nodes = hou.selectedNodes()
    if not nodes:
        hou.ui.displayMessage(
            "Select one or more MaterialX image nodes first.",
            severity=hou.severityType.Warning,
        )
        return

    updated, latest_already, no_version, skipped = [], [], [], []

    for node in nodes:
        parm = _file_parm(node)
        if parm is None:
            skipped.append("{}  (not a MaterialX image / COP file node)".format(node.path()))
            continue

        raw = parm.unexpandedString()
        if not raw:
            skipped.append("{}  (empty file parm)".format(node.path()))
            continue

        result = _latest_version(raw)
        if result is None:
            no_version.append("{}  ->  {}".format(node.name(), raw))
            continue

        latest, current, latest_files = result
        if latest <= current:
            latest_already.append("{}  (v{})".format(node.name(), current))
            continue

        parm.set(_new_value(raw, latest, latest_files))
        updated.append("{}  v{} -> v{}".format(node.name(), current, latest))

    lines = []
    if updated:
        lines.append("Updated to latest:")
        lines += ["    " + u for u in updated]
    if latest_already:
        lines.append("\nAlready latest:")
        lines += ["    " + u for u in latest_already]
    if no_version:
        lines.append("\nNo version token found:")
        lines += ["    " + u for u in no_version]
    if skipped:
        lines.append("\nSkipped:")
        lines += ["    " + u for u in skipped]

    hou.ui.displayMessage("\n".join(lines) or "Nothing to do.", title="Texture Version Up")
