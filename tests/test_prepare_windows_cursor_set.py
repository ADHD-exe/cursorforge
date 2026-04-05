import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import prepare_windows_cursor_set as prepare_windows_cursor_set_module


class PrepareWindowsCursorSetTests(unittest.TestCase):
    def test_appstart_is_not_a_generic_default_pointer_candidate(self) -> None:
        # Protects the filename heuristics from treating appstart cursors as normal arrows.
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir)
            appstart = source_dir / "appstart.ani"
            appstart.write_bytes(b"")

            candidates = prepare_windows_cursor_set_module.heuristic_slot_candidates(source_dir, [appstart])

            self.assertEqual(candidates["default_pointer"], [])
            self.assertEqual(len(candidates["progress"]), 1)
            self.assertEqual(candidates["progress"][0]["path"], appstart.resolve())

    def test_animated_progress_only_overrides_default_pointer_when_opted_in(self) -> None:
        # Protects the existing animated-default-pointer opt-in gate.
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir)
            arrow = source_dir / "arrow.cur"
            appstart = source_dir / "appstart.ani"
            arrow.write_bytes(b"")
            appstart.write_bytes(b"")

            analysis = {
                "install_inf": None,
                "warnings": [],
                "slot_candidates": {
                    "default_pointer": [
                        {
                            "path": str(arrow.resolve()),
                            "score": 5,
                            "reason": "filename heuristic matched Default Pointer",
                            "low_priority_hits": 0,
                            "depth": 0,
                        }
                    ],
                    "progress": [
                        {
                            "path": str(appstart.resolve()),
                            "score": 8,
                            "reason": "filename heuristic matched Progress",
                            "low_priority_hits": 0,
                            "depth": 0,
                        }
                    ],
                },
            }

            with mock.patch.object(prepare_windows_cursor_set_module, "parse_install_inf", return_value=(None, {})):
                with mock.patch.object(
                    prepare_windows_cursor_set_module,
                    "analyze_cursor_pack",
                    return_value=analysis,
                ):
                    chosen_default, diagnostics_default = prepare_windows_cursor_set_module.choose_slot_assignments(
                        source_dir,
                        [arrow, appstart],
                    )
                    self.assertEqual(chosen_default["default_pointer"], arrow.resolve())
                    self.assertEqual(chosen_default["progress"], appstart.resolve())
                    self.assertEqual(diagnostics_default["overrides"], [])

                    chosen_opt_in, diagnostics_opt_in = prepare_windows_cursor_set_module.choose_slot_assignments(
                        source_dir,
                        [arrow, appstart],
                        prefer_animated_default_pointer=True,
                    )
                    self.assertEqual(chosen_opt_in["default_pointer"], appstart.resolve())
                    self.assertEqual(chosen_opt_in["progress"], appstart.resolve())
                    self.assertEqual(len(diagnostics_opt_in["overrides"]), 1)
                    self.assertEqual(diagnostics_opt_in["overrides"][0]["target"], "default_pointer")

    def test_prepare_windows_cursor_set_reuses_analysis_for_summary_and_selection(self) -> None:
        # Protects auto-prepare from re-analyzing the same pack after the summary analysis already exists.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            output_dir = root / "prepared"
            source_dir.mkdir()
            arrow = source_dir / "arrow.cur"
            arrow.write_bytes(b"")

            analysis = {
                "install_inf": None,
                "install_inf_mapping": {},
                "warnings": ["review me"],
                "slot_candidates": {slot["key"]: [] for slot in prepare_windows_cursor_set_module.SLOT_DEFS},
            }
            analysis["slot_candidates"]["default_pointer"] = [
                {
                    "path": str(arrow.resolve()),
                    "score": 7,
                    "reason": "filename heuristic matched Default Pointer",
                    "low_priority_hits": 0,
                    "depth": 0,
                }
            ]

            with mock.patch.object(
                prepare_windows_cursor_set_module,
                "analyze_cursor_pack",
                return_value=analysis,
            ) as analyze_cursor_pack:
                summary = prepare_windows_cursor_set_module.prepare_windows_cursor_set(source_dir, output_dir)

            self.assertEqual(analyze_cursor_pack.call_count, 1)
            self.assertEqual(summary["analysis"], analysis)
            actual_output_dir = Path(summary["output_dir"]).resolve()
            self.assertEqual(actual_output_dir, output_dir.resolve())
            written_summary = json.loads((actual_output_dir / "prep-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(written_summary["analysis"], analysis)
            self.assertEqual(summary["selected_slots"]["default_pointer"], str(arrow.resolve()))

    def test_choose_slot_assignments_uses_provided_analysis_without_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir)
            arrow = source_dir / "arrow.cur"
            arrow.write_bytes(b"")

            analysis = {
                "install_inf": None,
                "install_inf_mapping": {},
                "warnings": [],
                "slot_candidates": {slot["key"]: [] for slot in prepare_windows_cursor_set_module.SLOT_DEFS},
            }
            analysis["slot_candidates"]["default_pointer"] = [
                {
                    "path": str(arrow.resolve()),
                    "score": 8,
                    "reason": "filename heuristic matched Default Pointer",
                    "low_priority_hits": 0,
                    "depth": 0,
                }
            ]

            with mock.patch.object(
                prepare_windows_cursor_set_module,
                "analyze_cursor_pack",
                side_effect=AssertionError("choose_slot_assignments should reuse the provided analysis"),
            ):
                with mock.patch.object(
                    prepare_windows_cursor_set_module,
                    "parse_install_inf",
                    side_effect=AssertionError("install.inf data should come from the provided analysis"),
                ):
                    chosen, diagnostics = prepare_windows_cursor_set_module.choose_slot_assignments(
                        source_dir,
                        [arrow],
                        analysis=analysis,
                    )

            self.assertEqual(chosen["default_pointer"], arrow.resolve())
            self.assertEqual(diagnostics["chosen_by_heuristic"]["default_pointer"]["path"], str(arrow.resolve()))


if __name__ == "__main__":
    unittest.main()
