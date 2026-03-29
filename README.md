# win2kde-cursor-converter

Convert Windows `.ani` / `.cur` cursor packs into Linux Xcursor themes with a staged GUI workflow.

## What The Tool Does

The project now treats conversion as a full review flow instead of a raw path editor:

1. Analyze the source pack before mapping.
2. Review and correct slot choices visually.
3. Preview real source animation and predicted Linux output behavior.
4. Build and package the final Xcursor theme.

The backend keeps the stronger source-path and native-entry preserving architecture:
- native `.cur` multi-entry preservation
- native `.ani` frame and delay preservation
- deferred source-path mapping
- manual `.json` and `.png` support
- packaging and hash alias generation
- custom GUI output sizes

## Current Status

The project now has both:
- a stabilized backend that preserves native cursor data more faithfully
- a staged GUI that helps users analyze, review, preview, and export cursor themes with less guesswork

Recent product-level upgrades:
- pack analysis before slot mapping
- visual slot cards instead of a raw path table
- real animated source previews
- predicted Linux output previews driven by current build settings
- ranked candidate browser per slot
- named build presets
- quality forecast and validation warnings

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
python ./cursor-source-slot-mapper.py
```

You can also use:

```bash
python ./win2kde-cursor-converter.py
```

## GUI Workflow

### Stage 1: Source Pack Analysis

Choose the Windows cursor folder and click `Analyze Pack`.

The GUI now surfaces pack-level diagnostics before mapping:
- number of `.cur`, `.ani`, and `.png` files
- which `.inf` file was chosen and why
- animated source count
- largest native sizes found
- likely HiDPI potential
- duplicate or temp/build-folder artifacts
- ambiguous slot candidates
- low-quality pack warnings

Click `Auto-Fill From Pack` to run the prepare step and populate the slot review stage using the same analyzed source data.

### Stage 2: Slot Review / Correction

This stage is visual-first instead of path-first.

Each slot card shows:
- slot label
- source preview
- filename
- animated or static badge
- source type
- native size summary
- quality forecast
- first warning if the choice looks suspicious

Selecting a slot opens a richer review panel with:
- selected source details
- validation warnings
- true source animation preview using real frame order and `delay_ms`
- predicted Linux output preview using the current build settings
- ranked candidate browser for that slot
- candidate preview and ranking explanation

Manual correction still works through:
- `Browse Source`
- `Clear Slot`
- `Use Selected Candidate`

Manual `.json` and `.png` sources remain supported.

### Stage 3: Build / Export

Build settings and export are grouped into one final stage:
- theme name
- output sizes
- scale filter
- preview size selector for predicted Linux output
- output root
- mapping load/save
- build and package actions

The stage also shows:
- overall quality forecast
- build warnings
- resolved Linux role map
- final output paths after build

## Animated Preview System

The GUI now previews animation behavior instead of showing only a static thumbnail.

For animated sources, the review stage uses actual extracted metadata to preview:
- frame order
- per-frame timing
- total animation duration
- visible flicker or fast flashing behavior

Two preview paths are shown:
- `Source Animation Preview`
  This shows the source cursor behavior using representative native frames.
- `Predicted Linux Output Preview`
  This runs the current builder path with the selected sizes and scale filter, then previews the expected emitted Linux cursor frames.

The preview panels include:
- play
- pause
- replay
- speed multiplier
- frame count
- duration summary

Static assets still preview correctly.

## Build Presets

The GUI includes named build presets:
- `standard-linux`
- `hidpi-kde`
- `maximum-detail`
- `pixel-glitch`
- `smooth-aa`

Presets configure:
- target sizes
- scale filter

The GUI keeps the preset label synchronized when the current settings match a known preset.

## Validation And Quality Forecast

The GUI now warns earlier about suspicious selections and weak packs.

Slot-level and build-level warnings can include:
- default pointer looks like progress/appstart art
- source detail is too small for requested output sizes
- source contains non-square frames
- slot choice is ambiguous based on filenames
- pack contains duplicate artifacts under `tmp/`, `build/`, or similar folders
- pack has weak HiDPI potential

Quality forecast labels:
- `excellent`
- `good`
- `acceptable`
- `likely blurry`
- `redraw recommended`

The forecast is based on native source detail relative to the current requested output sizes.

## Output

The GUI creates:
- `_prepared/<pack-name>/mapping.json`
- `_prepared/<pack-name>/prep-summary.json`
- `_mappings/<theme-name>.json`
- `_builds/<theme-name>/` temporary extracted and built assets
- `<theme-name>/` built Linux cursor theme
- `<theme-name>.tar.gz` installable cursor archive

## Install A Built Theme

```bash
mkdir -p ~/.icons
tar -xzf /path/to/YourTheme.tar.gz -C ~/.icons
plasma-apply-cursortheme YourTheme
```

## Native Entry Selection And Scaling

The backend still preserves the corrected builder behavior:
- the mapping JSON stores original source paths, not flattened PNG guesses
- build inspects the original `.cur`, `.ani`, `.json`, or `.png` source at build time
- for each requested Linux cursor size, the builder picks the smallest native Windows entry whose width and height are both at least that size
- if no native entry is large enough, the builder falls back to the largest available native entry
- same-size native entries are ranked by payload quality:
  - best size fit
  - larger embedded image payload
  - richer color metadata when available
  - stable entry order

## Non-Square Behavior

The output path now makes the non-square rule explicit:
- ImageMagick resize remains aspect-preserving
- the emitted PNG keeps its real width and height
- hotspots scale per axis
- the requested Linux cursor size is still written as the Xcursor nominal size

The GUI highlights non-square sources so users can inspect the predicted Linux preview before building.

## Malformed ANI Handling

Malformed `.ani` files fail clearly instead of falling through to indexing errors.

Examples:
- out-of-bounds `seq ` references
- invalid embedded icon references
- malformed chunk lengths

## CLI Helpers

Prepare a Windows cursor set:

```bash
python ./prepare-windows-cursor-set.py /path/to/windows-pack /path/to/output-root
```

Build from a saved mapping:

```bash
python ./build-cursor-from-mapping.py /path/to/mapping.json /path/to/output-root --theme-name YourTheme
```

Choose custom output sizes:

```bash
python ./build-cursor-from-mapping.py /path/to/mapping.json /path/to/output-root \
  --theme-name YourTheme \
  --sizes 24,32,36,48,64,96,128,192,256
```

Choose a scaling filter:

```bash
python ./build-cursor-from-mapping.py /path/to/mapping.json /path/to/output-root \
  --theme-name YourTheme \
  --scale-filter point
```

Opt in to the old animated default-pointer override only if you explicitly want it:

```bash
python ./prepare-windows-cursor-set.py /path/to/windows-pack /path/to/output-root \
  --prefer-animated-default-pointer
```

## Defaults

- output sizes: `24, 32, 36, 48, 64, 96, 128, 192`
- scale filters: `point`, `mitchell`, `lanczos`
- default filter: `point`
- `192` is included by default for HiDPI KDE workflows
- `256` remains optional through presets or manual size entry

## JSON Mapping Notes

Saved mapping JSON includes:
- `selected_slots`
- `resolved_role_map`
- `build_options`

Builder metadata JSON can represent multiple native entries per frame:
- each frame keeps `delay_ms`
- each frame can contain `entries[]`
- each entry can carry `png`, `width`, `height`, `hotspot_x`, `hotspot_y`, `entry_index`, `image_size`, and `colors`

Legacy flat JSON frame metadata is still accepted.

Manual metadata JSON validation remains strict:
- `frames` must be a list
- `entries[]` must not be empty
- referenced PNG files must exist
- provided width and height must match the actual PNG dimensions
- hotspots must be within the PNG bounds

## Notes

- generic conversion stays theme-agnostic by default
- the GUI now helps users analyze and correct weak packs earlier instead of waiting until after build
- some Windows packs still need manual slot correction if filenames or `install.inf` metadata are ambiguous
