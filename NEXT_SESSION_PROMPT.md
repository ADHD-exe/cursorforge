You are continuing work on the repository:

~/Git/cursorforge (branch: main)

Scope
- Converts Windows `.ani` / `.cur` cursor packs into Linux Xcursor themes with a staged GUI workflow.
- Backend preserves native entries, hotspots, frame order, and per-frame delays; build picks best native size per requested output and ranks same-size entries by payload quality.
- Packaging adds hash-based aliases; manual `.json` / `.png` sources stay supported.

State of play (cached tasks reviewed)
- Previous cached prompts incorrectly pointed at `win2kde-cursor-converter`; no other cached tasks found. Work is now on CursorForge and the feature set described below is already implemented.
- Backend stabilization already done: clearer malformed ANI errors, explicit non-square behavior, digest-hardened scaled caches, progress/default separation, quality-based tie-breaking.
- GUI overhaul already done: staged flow (Analyze → Review/Compare → Build/Export), analysis snapshot cards and action queue, ranked candidate browser with provenance, real animated source previews, predicted Linux output previews, compare workspace, named build presets, quality forecasts, background task runner to keep Tk responsive.

Key files
- tools/source_slot_mapper_gui.py (GUI, analysis, review, compare, build)
- tools/prepare_windows_cursor_set.py (prepare/auto-fill)
- tools/build_from_slot_mapping.py, tools/xcursor_builder.py (builder + packaging)
- tools/windows_cursor_tool.py (parsing)
- tools/gui_task_runner.py, tools/preview_cache.py (background + caching)
- README.md (workflow overview)

Run
- GUI: `python ./cursorforge.py` (or `python ./cursorforge-gui.py`)
- Prepare: `python ./prepare-windows_cursor_set.py /path/to/windows-pack /path/to/output-root`
- Build: `python ./build_from_slot_mapping.py /path/to/mapping.json /path/to/output-root --theme-name YourTheme`

Validation
- `python -m unittest -v`
- `python -m py_compile tools/source_slot_mapper_gui.py tools/gui_task_runner.py tools/prepare_windows_cursor_set.py tools/preview_cache.py`

Next steps (only if you choose to do more)
- Re-audit slot auto-assignment on ambiguous packs, large-pack GUI performance, malformed manual JSON handling, and preview fidelity on non-square/multi-entry assets.
- Keep manual JSON/PNG workflows, staged GUI flow, packaging, hotspots, and frame timing correct; prefer evidence-backed fixes.
