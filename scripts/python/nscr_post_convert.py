"""Post-render EXR -> graded PNG (+ mp4 for sequences) for USD Render ROPs.

Hook up as a Post-Render Script (language: python) on the ROP:

    import nscr_post_convert; nscr_post_convert.run(hou.pwd())

Reads the frame range off the ROP itself: a single-frame render gets one
PNG next to the EXR; a sequence gets a PNG per frame plus an H.264 mp4
(<name>_h264.mp4) in the same folder. Grade = standard ACES 2.0 studio
config, "sRGB - Display" / "ACES 2.0 - SDR 100 nits (Rec.709)" ODT,
true 16-bit PNG. Karma EXRs are ACEScg, no exposure gain.
"""
import os
import re
import shutil
import subprocess

import hou

DEFAULT_COLORCONFIG = "ocio://studio-config-v4.0.0_aces-v2.0_ocio-v2.5"
OIIO_INPUT = "ACEScg"
OIIO_DISPLAY = "sRGB - Display"
OIIO_VIEW = "ACES 2.0 - SDR 100 nits (Rec.709)"


def _picture_parm(rop):
    """The parm holding the output image path: the ROP's own override if set,
    else 'picture' on the rendersettings node the ROP actually renders.

    With several karmarendersettings nodes in the chain, the one whose
    primpath matches the ROP's Render Settings parm wins; when that parm is
    empty the nearest upstream settings node wins (it authored last, so it is
    the stage's default RenderSettings)."""
    p = rop.parm("outputimage")
    if p is not None and p.eval().strip():
        return p
    target = ""
    tp = rop.parm("rendersettings")
    if tp is not None:
        target = tp.eval().strip()
    candidates = []
    src = rop.input(0)
    while src is not None:
        if src.parm("picture") is not None:
            candidates.append(src)
        src = src.input(0)
    if not candidates:
        return None
    if target:
        for n in candidates:
            pp = n.parm("primpath")
            if pp is not None and pp.eval().strip() == target:
                return n.parm("picture")
        print("[post_convert] no settings node matches %s; using %s"
              % (target, candidates[0].path()))
    return candidates[0].parm("picture")


def _frames(rop):
    if rop.evalParm("trange") == 0:
        return [hou.frame()]
    f1, f2 = rop.evalParm("f1"), rop.evalParm("f2")
    f3 = max(rop.evalParm("f3"), 1.0)
    out, f = [], f1
    while f <= f2 + 1e-6:
        out.append(f)
        f += f3
    return out


def _make_png(hoiiotool, exr, colorconfig):
    png = os.path.splitext(exr)[0] + ".png"
    try:
        if os.path.exists(png):
            os.remove(png)
    except OSError:
        pass
    cmd = [hoiiotool, exr]
    if colorconfig:
        cmd += ["--colorconfig", colorconfig]
    cmd += ["--iscolorspace", OIIO_INPUT, "--ociodisplay", OIIO_DISPLAY, OIIO_VIEW,
            "-d", "uint16", "-o", png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.exists(png):
        print("[post_convert] oiiotool FAILED on %s:\n%s\n%s" % (exr, r.stdout, r.stderr))
        return None
    return png


def _make_mp4(pngs, fps):
    """Encode the graded PNGs (already carrying the ACES ODT) to H.264,
    tagged sRGB/bt709 so players match the PNG look."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("[post_convert] mp4 skipped: ffmpeg not found on PATH")
        return None
    base = re.sub(r"\.\d+$", "", os.path.splitext(pngs[0])[0])
    final = base + "_h264.mp4"
    try:
        if os.path.exists(final):
            os.remove(final)
    except OSError:
        pass
    lst = os.path.join(os.path.dirname(final), "._mp4_list.txt")
    with open(lst, "w", encoding="utf-8") as fp:
        for p in pngs:
            fp.write("file '%s'\nduration %.6f\n" % (p.replace("\\", "/"), 1.0 / fps))
        fp.write("file '%s'\n" % pngs[-1].replace("\\", "/"))  # concat quirk: repeat last
    try:
        cmd = [ffmpeg, "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", lst,
               "-fps_mode", "cfr", "-r", "%g" % fps,
               "-t", "%.6f" % (len(pngs) / fps),
               "-c:v", "libx264", "-preset", "slow", "-profile:v", "high",
               "-b:v", "50M", "-maxrate", "60M", "-bufsize", "100M",
               "-pix_fmt", "yuv420p",
               "-vf", ("scale=w='min(iw,3840)':h='min(ih,3840)':"
                       "force_original_aspect_ratio=decrease:force_divisible_by=2:"
                       "flags=lanczos"),
               "-color_primaries", "bt709", "-color_trc", "iec61966-2-1",
               "-colorspace", "bt709", "-movflags", "+faststart", final]
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass
    if os.path.exists(final) and os.path.getsize(final) > 0:
        return final
    print("[post_convert] mp4 FAILED:\n%s" % r.stderr[-1000:])
    return None


def run(rop):
    parm = _picture_parm(rop)
    if parm is None:
        print("[post_convert] no output image parm found; skipping")
        return
    frames = _frames(rop)
    exrs = []
    for f in frames:
        exr = parm.evalAtFrame(f)
        if os.path.exists(exr):
            exrs.append(exr)
        else:
            print("[post_convert] missing frame on disk: %s" % exr)
    if not exrs:
        print("[post_convert] no EXRs found; nothing to convert")
        return

    hoiiotool = os.path.join(hou.expandString("$HFS"), "bin", "hoiiotool.exe")
    # ALWAYS the ACES 2.0 studio config, never the scene $OCIO: the 1.0 and
    # 2.0 SDR views ship in SEPARATE built-in configs, so pinning the config
    # pins the look (seqview finding).
    colorconfig = DEFAULT_COLORCONFIG
    pngs = []
    for exr in exrs:
        png = _make_png(hoiiotool, exr, colorconfig)
        if png:
            pngs.append(png)
    print("[post_convert] %d/%d PNGs written (%s)" % (len(pngs), len(exrs), OIIO_VIEW))

    if len(frames) > 1 and len(pngs) > 1:
        mp4 = _make_mp4(pngs, hou.fps())
        if mp4:
            print("[post_convert] mp4: %s" % mp4)
