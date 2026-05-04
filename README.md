# NSCR Tools

A personal, ongoing collection of Houdini scripts, digital assets (HDAs), and tools by Nick Scarcella. This package is under active development — assets are added, revised, and refined as new production needs come up, so expect things to change.

## What's inside

- `otls/` — Houdini Digital Assets (HDAs)
  - `Recipes.hda` — bundled procedural recipes
  - `cop_NSCR.make_tile.1.0.hda` — COP tile maker
  - `cop_Nick.oklab_ramp.1.0.hda` — OKLab color ramp for COPs
- `presets/` — Houdini parameter presets
- `toolbar/` — custom shelf tools

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
