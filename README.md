# win2kde-cursor-converter

Convert Windows `.ani` / `.cur` cursor packs into Linux Xcursor themes with a GUI workflow.

## Dependencies

Arch / CachyOS:

```bash
sudo pacman -S --needed python tk icoutils xorg-xcursorgen imagemagick
```

Or run:

```bash
./scripts/install-deps-arch.sh
```

Required tools:
- `python`
- `tk`
- `icotool`
- `xcursorgen`
- `magick` or `convert`

## Run

From the repo root:

```bash
python ./win2kde-cursor-converter.py
```

You can also use:

```bash
python ./cursor-source-slot-mapper.py
```

## GUI Workflow

1. Choose the Windows cursor folder.
2. Click `Auto-Fill From Pack`.
3. Fix any slot paths if needed.
4. Set the output root and theme name.
5. Click `Build + Package`.
6. Install the generated `.tar.gz` cursor theme.

## Output

The GUI creates:
- `_prepared/<pack-name>/` extracted slot assets and auto-generated mapping
- `_mappings/<theme-name>.json` saved mapping
- `<theme-name>/` built Linux cursor theme
- `<theme-name>.tar.gz` installable cursor archive

## Install A Built Theme

```bash
mkdir -p ~/.icons
tar -xzf /path/to/YourTheme.tar.gz -C ~/.icons
plasma-apply-cursortheme YourTheme
```

## CLI Helpers

Prepare a Windows cursor set:

```bash
python ./prepare-windows-cursor-set.py /path/to/windows-pack /path/to/output-root
```

Build from a saved mapping:

```bash
python ./build-cursor-from-mapping.py /path/to/mapping.json /path/to/output-root --theme-name YourTheme
```

## Notes

- Default output sizes: `24, 32, 36, 48, 64`
- Animated slots preserve frame order, hotspot data, and delays from the Windows source where possible.
- Some Windows packs still need manual slot correction if filenames or `install.inf` metadata are ambiguous.
