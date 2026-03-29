You are working only inside:

~/Git/win2kde-cursor-converter

GitHub repo:
ADHD-exe/-win2kde-cursor-converter

Branch:
main

This repo converts Windows .ani/.cur cursor packs into Linux Xcursor themes through a GUI + JSON mapping + builder pipeline.

Current known-good state:
- preserves all native CUR entries in .cur and inside .ani frames
- defers extraction until build time
- chooses best native size per requested Linux size
- ranks same-size native entries by payload quality
- supports scale filters
- default sizes: 24, 32, 36, 48, 64, 96, 128, 192
- GUI preserves editable output sizes
- CLI respects mapping JSON build_options unless explicitly overridden
- prepare prefers top-level assets over tmp/build duplicates
- install.inf Windows backslash paths are normalized correctly
- hand slot mapping bug is fixed
- malformed ANI sequence/icon errors fail clearly
- generic prepare no longer swaps default pointer to progress silently
- non-square output metadata is explicit and correct
- GUI now has staged analysis/review/build tabs
- GUI previews real animation and predicted Linux output behavior
- GUI includes pack analysis, candidate browser, presets, and quality forecasts

Recent commits:
- b5eae9c Fix GUI size persistence and prep heuristics
- 988c7d4 Normalize Windows INF path separators

What to do:
1. Review the current repo carefully.
2. Find real bugs, regressions, or output-quality issues.
3. Validate findings with actual files or a reproducible proof case.
4. Fix them cleanly.
5. Re-test.
6. Commit and push to origin/main.

Focus files:
- tools/windows_cursor_tool.py
- tools/xcursor_builder.py
- tools/build_from_slot_mapping.py
- tools/prepare_windows_cursor_set.py
- tools/slot_definitions.py
- tools/source_slot_mapper_gui.py
- README.md
- NEXT_SESSION_PROMPT.md
- NEXT_SESSION_PROMPT_SHORT.md

Run:
- GUI: python ./cursor-source-slot-mapper.py
- Prepare: python ./prepare-windows-cursor-set.py /path/to/windows-pack /path/to/output-root
- Build: python ./build-cursor-from-mapping.py /path/to/mapping.json /path/to/output-root --theme-name YourTheme

Rules:
- work only in ~/Git/win2kde-cursor-converter
- do not use the old animated-cursor workspace as the source of truth
- explain root cause and impact clearly
- if you change code, provide complete updated files
- validate fixes before committing
- commit and push only after validation succeeds
