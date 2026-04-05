#!/usr/bin/env python3
"""Prepare a Windows cursor set for the slot-mapper GUI and Linux Xcursor builder."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
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
    explain_slot_match,
    normalize_cursor_sizes,
)
from windows_cursor_tool import inspect_path
from workspace_paths import configure_project_tmp


WINDOWS_CURSOR_EXTENSIONS = {".ani", ".cur", ".png"}
LOW_PRIORITY_DIR_NAMES = {
    "__pycache__",
    "_builds",
    "_prepared",
    "_preview-cache",
    "build",
    "builds",
    "cache",
    "dist",
    "temp",
    "tmp",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def discover_cursor_files(source_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in source_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in WINDOWS_CURSOR_EXTENSIONS
    )


def choose_preferred_inf(source_dir: Path) -> Path | None:
    details = choose_preferred_inf_details(source_dir)
    return None if details is None else Path(details["path"])


def choose_preferred_inf_details(source_dir: Path) -> dict | None:
    inf_files = sorted(source_dir.glob("*.inf"))
    if not inf_files:
        return None

    install_inf = next((path for path in inf_files if path.name.lower() == "install.inf"), None)
    if install_inf is not None:
        return {
            "path": str(install_inf.resolve()),
            "reason": "preferred install.inf when present at the pack root",
            "candidates": [str(path.resolve()) for path in inf_files],
        }

    def inf_priority(path: Path) -> tuple[int, int, str]:
        match = re.fullmatch(r"v(\d+)", path.stem.lower())
        if match:
            return (2, int(match.group(1)), path.name.lower())
        return (1, 0, path.name.lower())

    chosen = max(inf_files, key=inf_priority)
    match = re.fullmatch(r"v(\d+)", chosen.stem.lower())
    if match:
        reason = f"selected highest numbered version-like INF ({chosen.name})"
    else:
        reason = f"selected best available INF by filename priority ({chosen.name})"
    return {
        "path": str(chosen.resolve()),
        "reason": reason,
        "candidates": [str(path.resolve()) for path in inf_files],
    }


def parse_install_inf(source_dir: Path) -> tuple[Path | None, dict[str, Path]]:
    inf_details = choose_preferred_inf_details(source_dir)
    if inf_details is None:
        return None, {}
    inf_path = Path(inf_details["path"])
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
        normalized_name = re.sub(r"[\\/]+", "/", file_name.strip().strip('"'))
        candidate = source_dir.joinpath(*Path(normalized_name).parts)
        if candidate.exists():
            mapping[slot_key] = candidate.resolve()
    return inf_path, mapping


def candidate_path_priority(source_dir: Path, path: Path) -> tuple[int, int, str, str]:
    try:
        relative = path.resolve().relative_to(source_dir.resolve())
    except ValueError:
        relative = path.resolve()

    parents = relative.parts[:-1]
    low_priority_hits = sum(1 for part in parents if part.lower() in LOW_PRIORITY_DIR_NAMES)
    depth = len(parents)
    return (
        low_priority_hits,
        depth,
        relative.name.lower(),
        str(relative).lower(),
    )


def heuristic_slot_candidates(source_dir: Path, cursor_files: list[Path]) -> dict[str, list[dict]]:
    candidates: dict[str, list[dict]] = {slot["key"]: [] for slot in SLOT_DEFS}

    for path in cursor_files:
        for slot in SLOT_DEFS:
            match_details = explain_slot_match(path.stem, slot)
            score = int(match_details["score"])
            if score <= 0:
                continue
            candidates[slot["key"]].append(
                {
                    "path": path.resolve(),
                    "score": score,
                    "reason": f"filename heuristic matched {slot['label']}",
                    "match_details": match_details,
                    "provenance": "heuristic",
                }
            )

    for slot_key in candidates:
        candidates[slot_key].sort(
            key=lambda item: (-item["score"],) + candidate_path_priority(source_dir, item["path"])
        )
    return candidates


def is_animated_default_pointer_candidate(path: Path) -> bool:
    if path.suffix.lower() != ".ani":
        return False

    tokens = {token for token in re.split(r"[^a-z0-9]+", path.stem.lower()) if token}
    positive_tokens = {
        "appstart",
        "appstarting",
        "arrow",
        "background",
        "cursor",
        "default",
        "normal",
        "pointer",
        "select",
        "start",
        "working",
    }
    negative_tokens = {
        "busy",
        "error",
        "hourglass",
        "load",
        "loading",
        "spinner",
        "wait",
    }
    if any(token == negative or token.startswith(negative) for token in tokens for negative in negative_tokens):
        return False
    return any(token == positive or token.startswith(positive) for token in tokens for positive in positive_tokens)


def relative_display_path(source_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(source_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def inspect_png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != PNG_SIGNATURE:
        raise ValueError(f"not a PNG file: {path}")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def summarize_size_pairs(size_pairs: set[tuple[int, int]]) -> str:
    if not size_pairs:
        return "--"
    max_sizes = sorted({max(width, height) for width, height in size_pairs})
    return " / ".join(str(size) for size in max_sizes)


def inspect_cursor_asset(source_dir: Path, path: Path) -> dict:
    resolved = path.resolve()
    summary = {
        "path": str(resolved),
        "relative_path": relative_display_path(source_dir, resolved),
        "filename": resolved.name,
        "stem": resolved.stem,
        "extension": resolved.suffix.lower(),
        "warnings": [],
    }

    try:
        suffix = resolved.suffix.lower()
        if suffix == ".png":
            width, height = inspect_png_size(resolved)
            size_pairs = {(width, height)}
            summary.update(
                {
                    "source_type": "png",
                    "is_animated": False,
                    "frame_count": 1,
                    "entry_count": 1,
                    "delay_ms_total": 0,
                    "native_sizes": [{"width": width, "height": height}],
                    "contains_non_square": width != height,
                }
            )
        else:
            info = inspect_path(resolved)
            if info["type"] == "cur":
                size_pairs = {(entry["width"], entry["height"]) for entry in info["entries"]}
                summary.update(
                    {
                        "source_type": "cur",
                        "is_animated": False,
                        "frame_count": 1,
                        "entry_count": len(info["entries"]),
                        "delay_ms_total": 0,
                        "native_sizes": [
                            {"width": entry["width"], "height": entry["height"]}
                            for entry in info["entries"]
                        ],
                        "contains_non_square": any(entry["width"] != entry["height"] for entry in info["entries"]),
                    }
                )
            else:
                frame_entries = info["frame_entries"]
                size_pairs = {
                    (entry["width"], entry["height"])
                    for frame in frame_entries
                    for entry in frame["entries"]
                }
                summary.update(
                    {
                        "source_type": "ani",
                        "is_animated": True,
                        "frame_count": len(frame_entries),
                        "entry_count": sum(len(frame["entries"]) for frame in frame_entries),
                        "delay_ms_total": sum(int(frame["delay_ms"]) for frame in frame_entries),
                        "native_sizes": [
                            {"width": width, "height": height}
                            for width, height in sorted(size_pairs)
                        ],
                        "contains_non_square": any(width != height for width, height in size_pairs),
                    }
                )
        summary["largest_native_size"] = max(max(width, height) for width, height in size_pairs)
        summary["largest_native_area"] = max(width * height for width, height in size_pairs)
        summary["size_summary"] = summarize_size_pairs(size_pairs)
    except Exception as exc:  # noqa: BLE001
        summary.update(
            {
                "source_type": resolved.suffix.lower().lstrip(".") or "unknown",
                "is_animated": False,
                "frame_count": 0,
                "entry_count": 0,
                "delay_ms_total": 0,
                "native_sizes": [],
                "largest_native_size": 0,
                "largest_native_area": 0,
                "size_summary": "--",
                "contains_non_square": False,
                "error": str(exc),
            }
        )
        summary["warnings"].append(f"inspect failed: {exc}")
        return summary

    relative_parts = Path(summary["relative_path"]).parts[:-1]
    low_priority_hits = [part for part in relative_parts if part.lower() in LOW_PRIORITY_DIR_NAMES]
    summary["low_priority_hits"] = low_priority_hits
    if low_priority_hits:
        summary["warnings"].append(f"stored under low-priority folder(s): {', '.join(sorted(set(low_priority_hits)))}")
    if summary["contains_non_square"]:
        summary["warnings"].append("contains non-square native frames")
    if summary["largest_native_size"] and summary["largest_native_size"] < 32:
        summary["warnings"].append("very small native cursor art")
    elif summary["largest_native_size"] and summary["largest_native_size"] < 64:
        summary["warnings"].append("limited native size range")
    return summary


def enrich_slot_candidates(
    source_dir: Path,
    raw_candidates: dict[str, list[dict]],
    asset_lookup: dict[str, dict],
) -> dict[str, list[dict]]:
    enriched: dict[str, list[dict]] = {}
    for slot_key, candidates in raw_candidates.items():
        enriched_list = []
        for rank, candidate in enumerate(candidates, start=1):
            path = candidate["path"].resolve()
            asset = asset_lookup[str(path)]
            low_priority, depth, _, _ = candidate_path_priority(source_dir, path)
            enriched_list.append(
                {
                    "rank": rank,
                    "path": str(path),
                    "relative_path": asset["relative_path"],
                    "filename": asset["filename"],
                    "score": candidate["score"],
                    "reason": candidate["reason"],
                    "match_details": candidate.get("match_details", {}),
                    "provenance": candidate.get("provenance", "heuristic"),
                    "is_animated": asset["is_animated"],
                    "source_type": asset["source_type"],
                    "size_summary": asset["size_summary"],
                    "largest_native_size": asset["largest_native_size"],
                    "largest_native_area": asset.get("largest_native_area", 0),
                    "native_sizes": list(asset.get("native_sizes", [])),
                    "warnings": list(asset.get("warnings", [])),
                    "low_priority_hits": low_priority,
                    "depth": depth,
                    "duplicate_basename_count": asset.get("duplicate_basename_count", 0),
                }
            )
        enriched[slot_key] = enriched_list
    return enriched


def build_pack_warnings(
    asset_summaries: list[dict],
    inf_details: dict | None,
    slot_candidates: dict[str, list[dict]],
) -> list[str]:
    warnings = []
    if inf_details is None:
        warnings.append("No useful .inf file was found at the pack root; slot assignment will rely on filename heuristics.")

    usable_assets = [asset for asset in asset_summaries if not asset.get("error")]
    if usable_assets:
        max_sizes = [asset["largest_native_size"] for asset in usable_assets]
        if max(max_sizes) < 96:
            warnings.append("This pack has weak HiDPI potential: no source asset reaches 96px native detail.")
        if sum(1 for size in max_sizes if size < 64) >= max(4, len(max_sizes) // 2):
            warnings.append("Most source assets are 32px or similarly small, so larger Linux sizes will likely blur.")

    low_priority_assets = [asset for asset in asset_summaries if asset.get("low_priority_hits")]
    if low_priority_assets:
        warnings.append(
            "The pack contains duplicate or generated assets under tmp/build/cache-style folders; review candidate choices carefully."
        )

    ambiguous_slots = []
    for slot_key, candidates in slot_candidates.items():
        if len(candidates) < 2:
            continue
        top = candidates[0]["score"]
        second = candidates[1]["score"]
        if top == second or top - second <= 1:
            ambiguous_slots.append(SLOT_BY_KEY[slot_key]["label"])
    if ambiguous_slots:
        warnings.append(
            "Some slots are ambiguous based on filenames alone: " + ", ".join(sorted(ambiguous_slots)) + "."
        )

    error_assets = [asset for asset in asset_summaries if asset.get("error")]
    if error_assets:
        warnings.append(f"{len(error_assets)} source file(s) could not be inspected cleanly.")
    return warnings


def analyze_cursor_pack(source_dir: Path, cursor_files: list[Path] | None = None) -> dict:
    source_dir = source_dir.expanduser().resolve()
    discovered_files = discover_cursor_files(source_dir) if cursor_files is None else [path.resolve() for path in cursor_files]
    if not discovered_files:
        raise FileNotFoundError(f"no .ani/.cur/.png files found under {source_dir}")

    inf_details = choose_preferred_inf_details(source_dir)
    _, inf_mapping = parse_install_inf(source_dir)

    asset_summaries = [inspect_cursor_asset(source_dir, path) for path in discovered_files]
    asset_lookup = {asset["path"]: asset for asset in asset_summaries}

    basename_counts: dict[str, int] = {}
    for asset in asset_summaries:
        basename_counts[asset["filename"].lower()] = basename_counts.get(asset["filename"].lower(), 0) + 1
    for asset in asset_summaries:
        duplicate_count = basename_counts[asset["filename"].lower()]
        asset["duplicate_basename_count"] = duplicate_count
        if duplicate_count > 1:
            asset["warnings"].append("duplicate filename exists elsewhere in the pack")

    heuristic_candidates = heuristic_slot_candidates(source_dir, discovered_files)
    enriched_candidates = enrich_slot_candidates(source_dir, heuristic_candidates, asset_lookup)

    size_values = sorted({asset["largest_native_size"] for asset in asset_summaries if asset["largest_native_size"]}, reverse=True)
    animated_sources = [asset["path"] for asset in asset_summaries if asset["is_animated"]]
    hidpi_96 = sum(1 for asset in asset_summaries if asset["largest_native_size"] >= 96)
    hidpi_128 = sum(1 for asset in asset_summaries if asset["largest_native_size"] >= 128)
    hidpi_192 = sum(1 for asset in asset_summaries if asset["largest_native_size"] >= 192)

    if hidpi_128 >= 4 or hidpi_192 >= 2:
        hidpi_rating = "strong"
    elif hidpi_96 >= 4:
        hidpi_rating = "good"
    elif hidpi_96 >= 1:
        hidpi_rating = "limited"
    else:
        hidpi_rating = "weak"

    duplicate_artifacts = [
        {
            "path": asset["path"],
            "relative_path": asset["relative_path"],
            "reason": ", ".join(sorted(set(asset.get("low_priority_hits", [])))) or "duplicate filename",
        }
        for asset in asset_summaries
        if asset.get("low_priority_hits") or asset.get("duplicate_basename_count", 0) > 1
    ]

    ambiguous_candidates = {}
    for slot_key, candidates in enriched_candidates.items():
        if len(candidates) < 2:
            continue
        top = candidates[0]["score"]
        second = candidates[1]["score"]
        if top == second or top - second <= 1:
            ambiguous_candidates[slot_key] = candidates[:3]

    warnings = build_pack_warnings(asset_summaries, inf_details, enriched_candidates)

    return {
        "source_dir": str(source_dir),
        "cursor_files_found": [str(path) for path in discovered_files],
        "counts": {
            "cur": sum(1 for path in discovered_files if path.suffix.lower() == ".cur"),
            "ani": sum(1 for path in discovered_files if path.suffix.lower() == ".ani"),
            "png": sum(1 for path in discovered_files if path.suffix.lower() == ".png"),
            "total": len(discovered_files),
        },
        "install_inf": inf_details,
        "install_inf_mapping": {slot_key: str(path) for slot_key, path in inf_mapping.items()},
        "install_inf_slots_resolved": len(inf_mapping),
        "animated_sources": animated_sources,
        "largest_native_sizes_found": size_values[:10],
        "hidpi_potential": {
            "rating": hidpi_rating,
            "supports_96_count": hidpi_96,
            "supports_128_count": hidpi_128,
            "supports_192_count": hidpi_192,
        },
        "duplicate_artifacts": duplicate_artifacts,
        "ambiguous_candidates": ambiguous_candidates,
        "slot_candidates": enriched_candidates,
        "asset_summaries": asset_summaries,
        "warnings": warnings,
    }


def choose_slot_assignments(
    source_dir: Path,
    cursor_files: list[Path],
    *,
    prefer_animated_default_pointer: bool = False,
    analysis: dict | None = None,
) -> tuple[dict[str, Path], dict]:
    source_dir = source_dir.expanduser().resolve()
    cursor_files = [path.resolve() for path in cursor_files]
    if analysis is None:
        analysis = analyze_cursor_pack(source_dir, cursor_files)

    inf_details = analysis.get("install_inf")
    install_inf_mapping = analysis.get("install_inf_mapping")
    if install_inf_mapping is None:
        inf_path, inf_mapping = parse_install_inf(source_dir)
    else:
        inf_path = None if inf_details is None else Path(inf_details["path"]).resolve()
        inf_mapping = {
            slot_key: Path(path).expanduser().resolve()
            for slot_key, path in install_inf_mapping.items()
        }
    heuristic_candidates = analysis["slot_candidates"]
    chosen: dict[str, Path] = {}
    diagnostics = {
        "install_inf": str(inf_path) if inf_path else None,
        "install_inf_reason": None if inf_details is None else inf_details["reason"],
        "chosen_by_inf": {},
        "chosen_by_heuristic": {},
        "unmatched_files": [],
        "fallbacks": [],
        "overrides": [],
        "slot_candidates": heuristic_candidates,
        "warnings": analysis["warnings"],
        "options": {
            "prefer_animated_default_pointer": prefer_animated_default_pointer,
        },
    }

    used_paths: set[Path] = set()

    for slot_key, path in inf_mapping.items():
        chosen[slot_key] = path
        used_paths.add(path)
        diagnostics["chosen_by_inf"][slot_key] = str(path)

    slot_order = {slot["key"]: index for index, slot in enumerate(SLOT_DEFS)}
    ranked_pairs: list[tuple[tuple[int, int, int, int, str, str], str, dict]] = []
    for slot_key, candidates in heuristic_candidates.items():
        if slot_key in chosen:
            continue
        for candidate in candidates:
            candidate_path = Path(candidate["path"]).resolve()
            ranked_pairs.append(
                (
                    (
                        -int(candidate["score"]),
                        int(candidate.get("low_priority_hits", 0)),
                        int(candidate.get("depth", 0)),
                        slot_order[slot_key],
                        str(candidate_path).lower(),
                        candidate.get("reason", ""),
                    ),
                    slot_key,
                    candidate,
                )
            )

    ranked_pairs.sort(key=lambda item: item[0])
    for _sort_key, slot_key, candidate in ranked_pairs:
        if slot_key in chosen:
            continue
        candidate_path = Path(candidate["path"]).resolve()
        if candidate_path in used_paths:
            continue
        chosen[slot_key] = candidate_path
        used_paths.add(candidate_path)
        diagnostics["chosen_by_heuristic"][slot_key] = {
            "path": str(candidate_path),
            "score": candidate["score"],
            "reason": candidate["reason"],
        }

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

    if prefer_animated_default_pointer:
        default_pointer = chosen.get("default_pointer")
        progress_pointer = chosen.get("progress")
        if (
            default_pointer is not None
            and default_pointer.suffix.lower() != ".ani"
            and progress_pointer is not None
            and is_animated_default_pointer_candidate(progress_pointer)
            and progress_pointer != default_pointer
        ):
            chosen["default_pointer"] = progress_pointer
            diagnostics["overrides"].append(
                {
                    "target": "default_pointer",
                    "from": str(default_pointer),
                    "to": str(progress_pointer),
                    "reason": "prefer animated progress/start cursor as the Linux default pointer",
                }
            )

    return chosen, diagnostics


def build_mapping_payload(
    selected_slots: dict[str, Path],
    target_sizes: list[int] | None = None,
    scale_filter: str = DEFAULT_SCALE_FILTER,
) -> dict:
    sizes = normalize_cursor_sizes(target_sizes, fallback=DEFAULT_CURSOR_SIZES)
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
            "target_sizes": sizes,
            "scale_filter": scale_filter,
        },
        "selected_slots": selected_payload,
        "resolved_role_map": resolved,
    }


def prepare_windows_cursor_set(
    source_dir: Path,
    output_dir: Path,
    *,
    prefer_animated_default_pointer: bool = False,
) -> dict:
    source_dir = source_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    cursor_files = discover_cursor_files(source_dir)
    if not cursor_files:
        raise FileNotFoundError(f"no .ani/.cur/.png files found under {source_dir}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    analysis = analyze_cursor_pack(source_dir, cursor_files)
    chosen, diagnostics = choose_slot_assignments(
        source_dir,
        cursor_files,
        prefer_animated_default_pointer=prefer_animated_default_pointer,
        analysis=analysis,
    )
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
        "analysis": analysis,
        "prepare_mode": "source-paths-only",
        "prepare_options": diagnostics["options"],
        "build_options": payload["build_options"],
    }
    summary_path = output_dir / "prep-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    configure_project_tmp()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--prefer-animated-default-pointer",
        action="store_true",
        help="opt in to replacing a static default pointer with a suitable animated progress/start cursor",
    )
    args = parser.parse_args()

    summary = prepare_windows_cursor_set(
        args.source_dir,
        args.output_dir,
        prefer_animated_default_pointer=args.prefer_animated_default_pointer,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
