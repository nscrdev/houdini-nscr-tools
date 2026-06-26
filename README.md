# NSCR Tools

A personal, ongoing collection of Houdini scripts, digital assets (HDAs), and tools by Nick Scarcella. This package is under active development — assets are added, revised, and refined as new production needs come up, so expect things to change.

## What's inside

- `otls/` — Houdini Digital Assets (HDAs)
  - `Recipes.hda` — bundled procedural recipes
  - `cop_NSCR.make_tile.1.0.hda` — COP tile maker
  - `cop_Nick.oklab_ramp.1.0.hda` — OKLab color ramp for COPs
- `presets/` — Houdini parameter presets
- `toolbar/` — custom shelf tools
  - **COPS Mat Exports** (`nscr_tools.shelf`) — auto-builds texture-export chains downstream of COP Preview Materials
- `scripts/python/` — importable Python modules backing the shelf tools
  - `nscr_material_exports.py` — logic for the COPS Mat Exports tool

## Shelf tools

### COPS Mat Exports

Auto-builds texture-export chains downstream of selected COP **Preview Material**
(`previewmaterial`) nodes, so you don't have to wire up the unpack/null/ROP
plumbing by hand for every material.

**Usage:** in a COP network, select one or more Preview Material nodes and click
**COPS Mat Exports** on the *NSCR Tools* shelf.

**What it does** — for each selected material it detects the *active* channels,
then builds, named after the material:

| Node | Type | Purpose |
|------|------|---------|
| `<MAT>_Unpack` | `cableunpack` | one field per active channel |
| `<MAT>_Unpack_<Channel>` | `null` | one tap per unpacked field |
| `<MAT>_<Channel>` | `rop_image` | one image output per channel |

**Active-channel detection** — a channel is exported when **either** its input is
wired **or** its `default_*` parameter differs from the factory default. Everything
left at default and unconnected is skipped.

**Field types** — color/vector channels (basecolor, normal, the `*_color` tints)
unpack as RGB `vector`; scalar channels (metalness, roughness, coat, …) unpack as
Mono `float`.

**Colorspace / format** — handled per channel:

- **Color channels** (basecolor + all `*_color` tints) → **PNG**, `colorconversion`
  set to *Bake to OpenColorIO Display/View* (display tonemapping applied), **Data
  Size** forced to *16-bit integer*.
- **All other (data) channels** (metalness, roughness, coat, normal, …) → **EXR**,
  `colorconversion` set to *Raw*, **Data Size** forced to *16-bit float*.

Data Size is set explicitly (never left on *Automatic*) so outputs don't silently
drop to 8-bit and band.

ROPs are written to `$HIP/mat/$HIPNAME/$HIPNAME.$OS.$F4.<ext>` at 1024×1024, with
`ACEScg` working space and the `sRGB - Display` / `ACES 1.0 - SDR Video` view
transform.

**Re-running (incremental)** — the tool is safe to run repeatedly. On a material
that already has a `<MAT>_Unpack`, it detects any *newly* active channels (e.g. a
default you've since changed) and appends them — new cableunpack field, null tap,
and ROP — without disturbing the channels already built. Existing fields keep
their output index, so existing taps never shift. A material with nothing new to
add reports *up to date*. (Channels you later revert to default are left in place,
not removed.)

## Installation

NSCR Tools is structured as a standard Houdini package. You can install it one of two ways.

### Option 1: Houdini package file (recommended)

1. Clone this repo somewhere stable on disk:

   ```sh
   git clone https://github.com/nscrdev/houdini-nscr-tools.git
   ```

2. Find your Houdini user prefs directory (the version below is an example — match your Houdini version):

   - Windows: `C:\Users\<you>\Documents\houdini20.5\`
   - macOS: `~/Library/Preferences/houdini/20.5/`
   - Linux: `~/houdini20.5/`

3. Inside that directory, create a `packages/` folder if it doesn't already exist, then add a file named `nscr_tools.json` with this content (update the path to wherever you cloned the repo):

   ```json
   {
       "env": [
           {
               "NSCR_TOOLS": "C:/path/to/houdini-nscr-tools"
           }
       ],
       "path": "$NSCR_TOOLS"
   }
   ```

4. Restart Houdini. The HDAs will load automatically.

### Option 2: Manual `HOUDINI_PATH`

If you'd rather not use packages, add the repo path to your `HOUDINI_PATH` environment variable (semicolon-separated on Windows, colon-separated on macOS/Linux), making sure `&` is preserved at the end so Houdini's defaults still load:

```
HOUDINI_PATH=C:/path/to/houdini-nscr-tools;&
```

## Updating

```sh
cd /path/to/houdini-nscr-tools
git pull
```

Then restart Houdini.

## License

MIT — free and open. See [LICENSE](LICENSE).

## Notes

This is a personal toolkit shared in the open. There is no support guarantee, no stability guarantee, and assets may change without notice. If you find something useful, great — feel free to open an issue or PR.
