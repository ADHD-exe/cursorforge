You are continuing work on the repository:

~/Git/win2kde-cursor-converter

GitHub repo:
ADHD-exe/-win2kde-cursor-converter

Branch:
main

Scope:
This project converts Windows .ani and .cur cursor packs into Linux Xcursor themes with a GUI-assisted workflow for KDE Plasma, Wayland, and X11.

Current architecture:
- Windows source packs are prepared into a slot-mapping JSON, not flattened rasters
- original source paths are preserved in the mapping
- build stage re-inspects original .cur / .ani sources
- multi-entry CUR data is preserved
- .ani frames preserve their embedded CUR entries and frame delays
- builder chooses the smallest native entry >= target size, else the largest available entry
- same-size native entries are ranked by payload quality
- scaling filter is configurable
- current default output sizes are 24, 32, 36, 48, 64, 96, 128, 192
- GUI is now staged into:
  - Source Pack Analysis
  - Slot Review / Correction
  - Build / Export
- GUI supports pack analysis, animated previews, predicted Linux output previews, slot correction, build, and tarball packaging
- GUI includes named build presets and quality forecasts

Recent fixes already completed locally:
- backend stabilization pass
  - malformed ANI sequence/icon indices now fail clearly
  - generic prepare no longer silently replaces default pointer with progress
  - non-square output behavior is explicit and correct
  - scaled cache names include a digest to avoid collisions
  - same-size native entries rank by payload quality instead of only order
- GUI/workflow upgrade pass
  - staged GUI workflow replaces the old path-first layout
  - source pack analysis now surfaces counts, INF reasoning, HiDPI potential, duplicates, ambiguities, and warnings
  - slot review is visual-first with slot cards and ranked candidate browsing
  - animated source previews now use real frame order and delay_ms
  - predicted Linux output previews now use the actual builder path
  - build presets and quality forecast warnings were added

Older fixes already completed and pushed:
- b5eae9c Fix GUI size persistence and prep heuristics
  - GUI output sizes are real editable state now
  - save/build preserve custom sizes from loaded mappings
  - CLI builder no longer blindly stomps mapping build_options
  - prepare prefers top-level assets over tmp/build/cache duplicates
  - hand role mapping bug fixed
- 988c7d4 Normalize Windows INF path separators
  - install.inf entries using Windows backslashes now resolve properly on Linux

Key files:
- tools/windows_cursor_tool.py
- tools/xcursor_builder.py
- tools/build_from_slot_mapping.py
- tools/prepare_windows_cursor_set.py
- tools/slot_definitions.py
- tools/source_slot_mapper_gui.py
- README.md

What to do first:
1. Audit the current implementation carefully before editing.
2. Look for remaining real bugs, workflow regressions, and quality bottlenecks.
3. Separate actual defects from desired enhancements.
4. Validate each finding using:
   - a real Windows cursor pack
   - or a small synthetic proof case
5. Implement fixes cleanly.
6. Re-run validation after each important change.
7. Commit and push only after the fixes are proven.

Likely remaining review areas:
- slot auto-assignment heuristics on genuinely ambiguous packs
- GUI performance and caching on larger source packs
- GUI edge cases for malformed manual JSON or missing referenced PNGs
- predicted preview fidelity on unusual non-square or multi-entry assets
- opportunities to improve pack warnings and candidate explanations further
- packaging/install UX improvements that preserve correctness
- alias/symlink coverage gaps for KDE/Qt/X11 interoperability
- regression testing across KDE Plasma, Wayland, and X11 with real packs

Workflow expectations:
- manual JSON / PNG source workflows must keep working
- GUI slot mapping flow must keep working
- staged analysis/review/build workflow must keep working
- tarball packaging must keep working
- hotspots must remain correct
- frame ordering and delays must remain correct
- behavior should stay as compatible as possible across KDE Plasma, Wayland, and X11
- preserve working behavior unless there is a strong reason to change it

How to run:
- GUI:
  python ./cursor-source-slot-mapper.py
- Prepare:
  python ./prepare-windows-cursor-set.py /path/to/windows-pack /path/to/output-root
- Build:
  python ./build-cursor-from-mapping.py /path/to/mapping.json /path/to/output-root --theme-name YourTheme

Important instructions:
- use only ~/Git/win2kde-cursor-converter as the working repo
- do not treat the older animated-cursor workspace as canonical
- explain root cause and impact for each issue
- do not hand-wave
- when returning code, provide complete updated files, not patch snippets
- validate fixes with real evidence
- after validation, commit and push to origin/main
