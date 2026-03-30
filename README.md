# CursorForge

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
- at-a-glance analysis snapshot cards for source count, animation coverage, HiDPI potential, and attention items
- actionable analysis queue with jump-to-review, compare, and preset guidance
- visual slot cards instead of a raw path table
- real animated source previews
- predicted Linux output previews driven by current build settings
- ranked candidate browser per slot with current-choice provenance and ranking explanations
- compare workspace for source/output, current/alternate candidate, and preset-to-preset checks
- named build presets
- richer quality forecast, confidence, and redraw guidance

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
python ./cursorforge.py
```

You can also use:

```bash
python ./cursorforge-gui.py
```

## Validation

Run validation from the repo root:

```bash
python -m unittest -v
```

Explicit discovery still works too:

```bash
python -m unittest discover -s tests -v
```

For a fast syntax check across the main GUI/build modules:

```bash
python -m py_compile tools/source_slot_mapper_gui.py tools/gui_task_runner.py tools/prepare_windows_cursor_set.py tools/preview_cache.py
```

## GUI Workflow

The GUI now keeps long-running analysis, preview preparation, and build/export work off the Tk main thread.
Instead of freezing during extraction or preview generation, the relevant panels show loading states such as:
- `Analyzing pack...`
- `Preparing source preview...`
- `Preparing Linux output preview...`
- `Loading candidate preview...`

If you change the selected slot, preview size, scale filter, source pack, or work root while a background job is still running, older results are discarded so they cannot overwrite the current view.

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

The analysis stage now turns those warnings into actions:
- `Open Target` jumps into the relevant slot or build stage
- `Open Compare` goes straight into compare mode when a visual decision is needed
- `Apply Suggested Preset` lets weak HiDPI packs fall back to a safer preset immediately
- double-clicking an asset in the source list opens the closest slot/candidate context when one exists

The analysis stage also includes snapshot cards so users can judge pack quality quickly before they start correcting slots:
- total source file count
- animated source coverage
- HiDPI rating
- combined attention signals and action-item count

Click `Auto-Fill From Pack` to run the prepare step and populate the slot review stage using the same analyzed source data.

Generic auto-fill keeps `default_pointer` mapped to actual arrow/default candidates and keeps `progress` / appstart behavior separate. The older animated-default-pointer behavior remains opt-in through the CLI flag described below.

### Stage 2: Slot Review / Correction

This stage is visual-first instead of path-first.

Each slot card shows:
- slot label
- source preview
- filename
- animated or static badge
- source type
- native size summary
- hotspot summary
- quality forecast
- first warning or scaling-quality reason if the choice looks suspicious

Selecting a slot opens a richer review panel with:
- selected source details
- validation warnings, confidence, and next-step guidance
- current-choice provenance so users can see whether the slot came from `install.inf`, heuristic ranking, fallback reuse, or manual override
- true source animation preview using real frame order and `delay_ms`
- predicted Linux output preview using the current build settings
- ranked candidate browser for that slot
- candidate preview, ranking explanation, and why lower-ranked options lost

The review stage now also includes a dedicated compare workspace with three modes:
- `Current vs Candidate`
- `Source vs Linux Output`
- `Current Build vs Compare Preset`

This makes it possible to compare the assigned cursor against an alternate candidate, inspect how conversion changes the motion/output, or judge whether a smaller preset is safer than a large HiDPI build.

Manual correction still works through:
- `Browse Source`
- `Clear Slot`
- `Use Selected Candidate`
- `Open Compare`

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
  This shows the source cursor behavior using the same native-entry chooser the builder would use for the currently selected preview size.
- `Predicted Linux Output Preview`
  This runs the current builder path with the selected sizes and scale filter, then previews the expected emitted Linux cursor frames.

Preview cache behavior:
- preview cache files live under `<work-root>/_preview-cache`
- source metadata and predicted-output preview keys include the selected source path, the active work root, and the current preview settings
- manual `.json` sources also include the referenced PNG dependency tokens, so editing a PNG refreshes both source and predicted-output previews even when the JSON file itself does not change
- in-memory preview caches are bounded, and older on-disk preview artifacts are pruned gradually during long GUI sessions

The preview panels include:
- play
- pause
- replay
- previous/next frame stepping
- speed multiplier
- frame count
- duration summary
- timing profile summary
- per-frame strip with start time, delay, size, hotspot, and timing note
- warnings for suspicious animation behavior such as very short loops, sharp timing jumps, or non-square frames
- loop-shape checks for size jitter or moving hotspots

Static assets still preview correctly.

## Build Presets

The GUI includes named build presets:
- `Standard Linux`
- `HiDPI KDE`
- `Maximum Detail`
- `Pixel / Glitch`
- `Smooth / Anti-aliased`

Presets configure:
- target sizes
- scale filter

The preset picker shows the user-facing labels while still syncing automatically when the current settings match a known preset.

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

The forecast is now paired with a confidence hint and decision guidance such as:
- build-ready
- build-ready with review
- compare before export
- reduce preset or replace art
- manual replacement required

The forecast still stays heuristic, but it now considers:
- native detail versus requested output ceiling
- native size coverage across the requested size list
- ambiguous slot ranking
- generated-folder / duplicate-source risk
- non-square source behavior

The build stage also highlights:
- slots that should be reviewed before export
- a redraw/manual replacement queue
- a safer preset suggestion when large output sizes are likely to blur badly

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
- mismatched declared ANI frame or step counts
- empty `rate` / `seq ` chunks
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
- referenced PNG changes invalidate preview metadata and preview output caches automatically
- referenced PNG files must exist
- provided width and height must match the actual PNG dimensions
- hotspots must be within the PNG bounds

## Validation

Quick local validation commands:

```bash
python -m py_compile tools/source_slot_mapper_gui.py tools/gui_task_runner.py tools/preview_cache.py
python -m unittest discover -s tests -q
```

## Notes

- generic conversion stays theme-agnostic by default
- the GUI now helps users analyze and correct weak packs earlier instead of waiting until after build
- compare mode and actionable warnings are meant to reduce “score guessing” during review
- some Windows packs still need manual slot correction if filenames or `install.inf` metadata are ambiguous
