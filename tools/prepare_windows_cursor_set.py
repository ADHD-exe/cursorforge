#!/usr/bin/env python3
"""Prepare a Windows cursor set for the slot-mapper GUI and Linux Xcursor builder."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from slot_definitions import (
    DEFAULT_CURSOR_SIZES,
    DEFAULT_SCALE_FILTER,
    SLOT_BY_KEY,
    SLOT_DEFS,
    WINDOWS_ROLE_TO_SLOT,
    score_slot_match,
)


WINDOWS_CURSOR_EXTENSIONS = {".ani", ".cur", ".png"}


def discover_cursor_files(source_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in source_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in WINDOWS_CURSOR_EXTENSIONS
    )


def parse_install_inf(source_dir: Path) -> tuple[Path | None, dict[str, Path]]:
    inf_files = sorted(source_dir.glob("*.inf"))
    if not inf_files:
        return None, {}

    inf_path = inf_files[0]
    lines = inf_path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_strings = False
    string_pairs: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_strings = line.strip().lower() == "[strings]"
            continue
        if not in_strings or "=" not in line:
            continue
        key, value = line.split("=", 1)
        string_pairs[key.strip().lower()] = value.strip().strip('"')

    mapping: dict[str, Path] = {}
    for role_name, file_name in string_pairs.items():
        slot_key = WINDOWS_ROLE_TO_SLOT.get(role_name.lower())
        if not slot_key:
            continue
        candidate = source_dir / file_name.strip().strip('"')
        if candidate.exists():
            mapping[slot_key] = candidate.resolve()
    return inf_path, mapping


def heuristic_slot_candidates(cursor_files: list[Path]) -> dict[str, list[dict]]:
    candidates: dict[str, list[dict]] = {slot["key"]: [] for slot in SLOT_DEFS}

    for path in cursor_files:
        for slot in SLOT_DEFS:
            score = score_slot_match(path.stem, slot)
            if score <= 0:
                continue
            candidates[slot["key"]].append(
                {
                    "path": path.resolve(),
                    "score": score,
                    "reason": f"filename heuristic matched {slot['label']}",
                }
            )

    for slot_key in candidates:
        candidates[slot_key].sort(key=lambda item: (-item["score"], item["path"].name))
    return candidates


def choose_slot_assignments(source_dir: Path, cursor_files: list[Path]) -> tuple[dict[str, Path], dict]:
    inf_path, inf_mapping = parse_install_inf(source_dir)
    heuristic_candidates = heuristic_slot_candidates(cursor_files)
    chosen: dict[str, Path] = {}
    diagnostics = {
        "install_inf": str(inf_path) if inf_path else None,
        "chosen_by_inf": {},
        "chosen_by_heuristic": {},
        "unmatched_files": [],
        "fallbacks": [],
    }

    used_paths: set[Path] = set()

    for slot_key, path in inf_mapping.items():
        chosen[slot_key] = path
        used_paths.add(path)
        diagnostics["chosen_by_inf"][slot_key] = str(path)

    for slot in SLOT_DEFS:
        slot_key = slot["key"]
        if slot_key in chosen:
            continue
        for candidate in heuristic_candidates[slot_key]:
            if candidate["path"] in used_paths:
                continue
            chosen[slot_key] = candidate["path"]
            used_paths.add(candidate["path"])
            diagnostics["chosen_by_heuristic"][slot_key] = {
                "path": str(candidate["path"]),
                "score": candidate["score"],
            }
            break

    fallback_pairs = [
        ("hand", "link_alias"),
        ("link_alias", "hand"),
        ("progress", "wait"),
        ("wait", "progress"),
        ("help", "default_pointer"),
        ("special_misc", "default_pointer"),
    ]
    for target_key, source_key in fallback_pairs:
        if target_key in chosen or source_key not in chosen:
            continue
        chosen[target_key] = chosen[source_key]
        diagnostics["fallbacks"].append({"target": target_key, "source": source_key, "path": str(chosen[source_key])})

    for path in cursor_files:
        if path.resolve() not in used_paths:
            diagnostics["unmatched_files"].append(str(path.resolve()))

    return chosen, diagnostics


def build_mapping_payload(
    selected_slots: dict[str, Path],
    target_sizes: list[int] | None = None,
    scale_filter: str = DEFAULT_SCALE_FILTER,
) -> dict:
    selected_payload = {}
    resolved = {}
    for slot_key, source_path in selected_slots.items():
        slot = SLOT_BY_KEY[slot_key]
        selected_payload[slot_key] = {
            "label": slot["label"],
            "path": str(source_path),
            "roles": slot["roles"],
        }
        for role in slot["roles"]:
            resolved[role] = str(source_path)
    return {
        "mapping_format_version": 2,
        "build_options": {
            "target_sizes": list(target_sizes or DEFAULT_CURSOR_SIZES),
            "scale_filter": scale_filter,
        },
        "selected_slots": selected_payload,
        "resolved_role_map": resolved,
    }


def prepare_windows_cursor_set(source_dir: Path, output_dir: Path) -> dict:
    source_dir = source_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    cursor_files = discover_cursor_files(source_dir)
    if not cursor_files:
        raise FileNotFoundError(f"no .ani/.cur/.png files found under {source_dir}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    chosen, diagnostics = choose_slot_assignments(source_dir, cursor_files)
    selected_slots = {slot_key: source_path.resolve() for slot_key, source_path in sorted(chosen.items())}

    payload = build_mapping_payload(selected_slots)
    mapping_path = output_dir / "mapping.json"
    mapping_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    summary = {
        "source_dir": str(source_dir),
        "output_dir": str(output_dir),
        "cursor_files_found": [str(path) for path in cursor_files],
        "selected_slot_count": len(selected_slots),
        "mapping_json": str(mapping_path),
        "selected_slots": {key: str(path) for key, path in selected_slots.items()},
        "diagnostics": diagnostics,
        "prepare_mode": "source-paths-only",
        "build_options": payload["build_options"],
    }
    summary_path = output_dir / "prep-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    summary = prepare_windows_cursor_set(args.source_dir, args.output_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
