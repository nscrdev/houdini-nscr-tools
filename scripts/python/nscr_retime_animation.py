"""Retime every keyframed parameter in the scene into a new frame range.

Scales keyframe positions linearly from an old range to a new range so the
animation plays back identically, just faster/slower. Only parameters that
actually carry keyframes are touched; expression-only parameters (ch(),
custom hscript/python expressions with no keys) have no keyframes and are
skipped automatically.

Entry points:
    retime_scene(old_start, old_end, new_start, new_end, snap=True, dry_run=False)
    run()   -- interactive dialog version used by the shelf tool
"""

import hou


def _iter_keyframed_parms():
    """Yield (parm, keyframes) for every parm in the scene with keyframes.

    Recurses the whole node tree but stays out of locked HDAs (their
    parms can't be edited anyway).
    """
    for node in hou.node("/").allSubChildren(recurse_in_locked_nodes=False):
        if node.isInsideLockedHDA() and not node.isEditableInsideLockedHDA():
            continue
        for parm in node.parms():
            if parm.isLocked():
                continue
            try:
                keys = parm.keyframes()
            except hou.OperationFailed:
                continue
            if keys:
                yield parm, keys


def retime_scene(old_start, old_end, new_start, new_end, snap=True, dry_run=False):
    """Linearly remap all scene keyframes from [old_start, old_end] to
    [new_start, new_end].

    Keys outside the old range are remapped with the same linear function
    (they keep their relative position past the ends). Slopes and accels
    are rescaled so tangent shapes keep the same intent in the new timing.

    Args:
        snap: round remapped keys to whole frames. If two keys on the same
            channel collapse onto one frame, the later original key wins.
        dry_run: report what would change without modifying anything.

    Returns a report dict:
        parms      -- number of channels retimed
        keys       -- number of keyframes written
        collisions -- list of "path (frame)" strings where snapped keys collided
        errors     -- list of "path: error" strings for channels that failed
    """
    old_len = float(old_end) - float(old_start)
    new_len = float(new_end) - float(new_start)
    if old_len == 0:
        raise ValueError("Old range has zero length.")
    if new_len == 0:
        raise ValueError("New range has zero length.")
    ratio = new_len / old_len

    report = {"parms": 0, "keys": 0, "static": 0, "collisions": [], "errors": []}

    def remap(frame):
        newf = new_start + (frame - old_start) * ratio
        return float(round(newf)) if snap else newf

    work = []
    for parm, keys in _iter_keyframed_parms():
        # Channels where no key actually moves (e.g. a single static key at
        # the start frame) are left completely untouched.
        if all(abs(remap(k.frame()) - k.frame()) < 1e-6 for k in keys):
            report["static"] += 1
            continue
        new_keys = {}  # frame -> keyframe, so snapped collisions keep the last key
        for k in keys:  # parm.keyframes() already returns detached copies
            newf = remap(k.frame())
            if newf in new_keys:
                report["collisions"].append("%s (frame %g)" % (parm.path(), newf))
            k.setFrame(newf)
            # Slopes are value-per-second; compressing time steepens them.
            try:
                if k.isSlopeSet():
                    k.setSlope(k.slope() / ratio)
                if k.isInSlopeSet():
                    k.setInSlope(k.inSlope() / ratio)
                if k.isAccelSet():
                    k.setAccel(k.accel() * ratio)
                if k.isInAccelSet():
                    k.setInAccel(k.inAccel() * ratio)
            except (AttributeError, hou.OperationFailed):
                pass  # non-float keyframes (string/int) have no slope handles
            new_keys[newf] = k
        work.append((parm, sorted(new_keys.values(), key=lambda x: x.frame())))
        report["parms"] += 1
        report["keys"] += len(new_keys)

    if dry_run:
        return report

    with hou.undos.group("Retime scene animation"):
        for parm, new_keys in work:
            try:
                parm.deleteAllKeyframes()
                parm.setKeyframes(new_keys)
            except (hou.OperationFailed, hou.PermissionError) as exc:
                report["errors"].append("%s: %s" % (parm.path(), exc))
    return report


def run():
    """Interactive front-end: prompt for old/new range and snap, then retime."""
    start, end = hou.playbar.frameRange()
    button, values = hou.ui.readMultiInput(
        "Scale all scene keyframes from the old range into the new range.\n"
        "Expression-only parameters are left untouched.",
        ("Old start frame", "Old end frame", "New start frame", "New end frame"),
        initial_contents=("%g" % start, "%g" % end, "%g" % start, ""),
        buttons=("Retime", "Cancel"),
        default_choice=0,
        close_choice=1,
        title="Retime Scene Animation",
    )
    if button != 0:
        return
    try:
        old_start, old_end, new_start, new_end = (float(v) for v in values)
    except ValueError:
        hou.ui.displayMessage("All four frames must be numbers.",
                              severity=hou.severityType.Error)
        return

    snap = hou.ui.displayMessage(
        "Snap retimed keys to whole frames?",
        buttons=("Snap (recommended)", "Keep fractional", "Cancel"),
        default_choice=0, close_choice=2,
        title="Retime Scene Animation",
    )
    if snap == 2:
        return

    try:
        report = retime_scene(old_start, old_end, new_start, new_end,
                              snap=(snap == 0))
    except ValueError as exc:
        hou.ui.displayMessage(str(exc), severity=hou.severityType.Error)
        return

    msg = ("Retimed %d keyframes on %d channels.\n"
           "%d channels left untouched (no key needed to move)."
           % (report["keys"], report["parms"], report["static"]))
    details = ""
    if report["collisions"]:
        msg += "\n%d snapped keys collided (later key kept)." % len(report["collisions"])
        details += "Collisions:\n  " + "\n  ".join(report["collisions"]) + "\n"
    if report["errors"]:
        msg += "\n%d channels FAILED." % len(report["errors"])
        details += "Errors:\n  " + "\n  ".join(report["errors"])
    hou.ui.displayMessage(msg, details=details or None,
                          severity=(hou.severityType.Warning if report["errors"]
                                    else hou.severityType.Message),
                          title="Retime Scene Animation")
