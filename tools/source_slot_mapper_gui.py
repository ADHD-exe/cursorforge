#!/usr/bin/env python3
"""GUI workflow for analyzing, reviewing, previewing, and exporting cursor packs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_from_slot_mapping import (
    build_theme_from_mapping,
    load_source_metadata,
    prepare_output_preview_metadata,
)
from preview_cache import (
    BoundedCache,
    MAX_OUTPUT_PREVIEW_DIRS,
    MAX_PREVIEW_THUMB_FILES,
    MAX_SOURCE_CACHE_DIRS,
    cache_artifact_dir,
    file_identity,
    normalize_path,
    prune_cache_dir,
    source_cache_identity,
    touch_cache_path,
)
from gui_build_profile import (
    BuildProfileState,
    build_profile_payload,
    resolve_build_profile_state,
    restore_profile_base_preset,
)
from gui_task_runner import GuiTaskRunner, RequestTracker, TaskToken, TkAfterCoalescer
from gui_workflow_summary import build_compare_guidance, build_readiness_snapshot
from xcursor_builder import choose_best_entry
from prepare_windows_cursor_set import analyze_cursor_pack, prepare_windows_cursor_set
from slot_definitions import (
    BUILD_PRESET_LABELS,
    DEFAULT_CURSOR_SIZES,
    DEFAULT_SCALE_FILTER,
    SCALE_FILTER_CHOICES,
    SLOT_BY_KEY,
    SLOT_DEFS,
    describe_build_preset,
    format_cursor_sizes,
    normalize_cursor_sizes,
    resolve_build_preset,
    score_slot_match,
)
from windows_cursor_tool import sanitize_path_component
from workspace_paths import (
    DEFAULT_PREVIEW_ROOT_NAME,
    DEFAULT_WORK_ROOT,
    REPO_ROOT,
    configure_project_tmp,
)

DEFAULT_GUI_PALETTE_PATH = REPO_ROOT / "gui-palette.json"
CARD_PREVIEW_SIZE = 48
PLAYER_PREVIEW_SIZE = 132
CANDIDATE_PREVIEW_SIZE = 96
CANVAS_SLOT_GLYPH_SIZE = 28
BUILD_SETTINGS_REFRESH_MS = 140
PREVIEW_SIZE_REFRESH_MS = 90
SELECTED_DETAIL_REFRESH_MS = 50
CANDIDATE_DETAIL_REFRESH_MS = 70
COMPARE_REFRESH_MS = 60
COMPARE_MODE_CURRENT_VS_CANDIDATE = "Current vs Candidate"
COMPARE_MODE_SOURCE_VS_OUTPUT = "Source vs Linux Output"
COMPARE_MODE_PRESET = "Current Build vs Compare Preset"
COMPARE_MODE_CHOICES = (
    COMPARE_MODE_CURRENT_VS_CANDIDATE,
    COMPARE_MODE_SOURCE_VS_OUTPUT,
    COMPARE_MODE_PRESET,
)
SAFE_PRESET_LABEL = "Standard Linux"
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
DEFAULT_GUI_PALETTE = {
    "root_bg": "#2a2e32",
    "panel_bg": "#31363b",
    "content_bg": "#1b1e20",
    "text": "#fcfcfc",
    "heading_text": "#fcfcfc",
    "muted_text": "#a1a9b1",
    "path_text": "#a1a9b1",
    "accent": "#3daee9",
    "accent_fg": "#fcfcfc",
    "border": "#5f6265",
    "warning": "#f67400",
    "error": "#da4453",
    "success": "#27ae60",
    "selection_bg": "#3daee9",
    "selection_text": "#fcfcfc",
    "preview_bg": "#1b1e20",
    "preview_border": "#5f6265",
    "preview_placeholder": "#7a7f86",
    "preview_counter_text": "#a1a9b1",
    "card_bg": "#31363b",
    "card_selected_bg": "#3daee9",
    "card_text": "#fcfcfc",
    "card_muted_text": "#a1a9b1",
    "card_warning_text": "#f67400",
    "button_bg": "#31363b",
    "button_fg": "#fcfcfc",
    "entry_bg": "#1b1e20",
    "entry_fg": "#fcfcfc",
    "tree_bg": "#1b1e20",
    "tree_fg": "#fcfcfc",
    "tree_selected_bg": "#3daee9",
    "tree_selected_fg": "#fcfcfc",
    "glyph_fg": "#fcfcfc",
    "glyph_accent": "#3daee9",
    "status_text": "#fcfcfc",
}
PALETTE_ALIASES = {
    "app_bg": "root_bg",
    "surface": "panel_bg",
    "accent_text": "accent_fg",
}


def build_payload(
    selected_slots: dict,
    resolved: dict,
    target_sizes: list[int] | None = None,
    scale_filter: str = DEFAULT_SCALE_FILTER,
    selection_context: dict | None = None,
    build_profile: dict | None = None,
) -> dict:
    sizes = normalize_cursor_sizes(target_sizes, fallback=DEFAULT_CURSOR_SIZES)
    payload = {
        "mapping_format_version": 2,
        "build_options": {
            "target_sizes": sizes,
            "scale_filter": scale_filter,
        },
        "selected_slots": {
            item["slot"]["key"]: {
                "label": item["slot"]["label"],
                "path": item["path"],
                "roles": item["slot"]["roles"],
            }
            for item in selected_slots.values()
        },
        "resolved_role_map": resolved,
    }
    if selection_context:
        payload["selection_context"] = selection_context
    if build_profile:
        payload["build_profile"] = build_profile
    return payload


def load_mapping_payload(mapping_path: Path) -> dict:
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    if "selected_slots" not in payload or "resolved_role_map" not in payload:
        raise ValueError("mapping JSON must contain selected_slots and resolved_role_map")
    return payload


def slugify_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "Custom-cursor"


def package_theme(theme_dir: Path, tar_path: Path) -> Path:
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(theme_dir, arcname=theme_dir.name)
    return tar_path


def _normalize_hex_color(value: str) -> str | None:
    value = value.strip()
    if not HEX_COLOR_RE.match(value):
        return None
    if len(value) == 4:
        return "#" + "".join(ch * 2 for ch in value[1:])
    return value.lower()


def resolve_palette_path(explicit_path: Path | None = None) -> Path | None:
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()
    if DEFAULT_GUI_PALETTE_PATH.exists():
        return DEFAULT_GUI_PALETTE_PATH.resolve()
    return None


def load_gui_palette(palette_path: Path | None = None) -> tuple[dict[str, str], Path | None, str]:
    resolved_path = resolve_palette_path(palette_path)
    palette = dict(DEFAULT_GUI_PALETTE)
    if resolved_path is None or not resolved_path.exists():
        return palette, None, "built-in"

    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"GUI palette file must contain a JSON object: {resolved_path}")

    palette_name = str(payload.get("name", resolved_path.stem))
    provided_keys = set()
    for raw_key, raw_value in payload.items():
        if raw_key == "name":
            continue
        key = PALETTE_ALIASES.get(raw_key, raw_key)
        if key not in palette:
            continue
        normalized = _normalize_hex_color(str(raw_value))
        if normalized is None:
            raise ValueError(f"invalid GUI palette color for {raw_key!r}: {raw_value!r}")
        palette[key] = normalized
        provided_keys.add(key)

    if "card_bg" not in provided_keys:
        palette["card_bg"] = palette["panel_bg"]
    if "card_selected_bg" not in provided_keys:
        palette["card_selected_bg"] = palette["selection_bg"]
    if "button_bg" not in provided_keys:
        palette["button_bg"] = palette["panel_bg"]
    if "button_fg" not in provided_keys:
        palette["button_fg"] = palette["text"]
    if "entry_bg" not in provided_keys:
        palette["entry_bg"] = palette["content_bg"]
    if "entry_fg" not in provided_keys:
        palette["entry_fg"] = palette["text"]
    if "tree_bg" not in provided_keys:
        palette["tree_bg"] = palette["content_bg"]
    if "tree_fg" not in provided_keys:
        palette["tree_fg"] = palette["text"]
    if "tree_selected_bg" not in provided_keys:
        palette["tree_selected_bg"] = palette["selection_bg"]
    if "tree_selected_fg" not in provided_keys:
        palette["tree_selected_fg"] = palette["selection_text"]
    if "preview_bg" not in provided_keys:
        palette["preview_bg"] = palette["content_bg"]
    if "preview_border" not in provided_keys:
        palette["preview_border"] = palette["border"]
    if "preview_placeholder" not in provided_keys:
        palette["preview_placeholder"] = palette["muted_text"]
    if "preview_counter_text" not in provided_keys:
        palette["preview_counter_text"] = palette["path_text"]
    if "heading_text" not in provided_keys:
        palette["heading_text"] = palette["text"]
    if "path_text" not in provided_keys:
        palette["path_text"] = palette["muted_text"]
    if "card_text" not in provided_keys:
        palette["card_text"] = palette["heading_text"]
    if "card_muted_text" not in provided_keys:
        palette["card_muted_text"] = palette["path_text"]
    if "card_warning_text" not in provided_keys:
        palette["card_warning_text"] = palette["warning"]
    if "glyph_fg" not in provided_keys:
        palette["glyph_fg"] = palette["heading_text"]
    if "glyph_accent" not in provided_keys:
        palette["glyph_accent"] = palette["accent"]
    if "status_text" not in provided_keys:
        palette["status_text"] = palette["text"]
    return palette, resolved_path, palette_name


def find_image_tool() -> str:
    for tool_name in ("magick", "convert"):
        tool_path = shutil.which(tool_name)
        if tool_path:
            return tool_path
    raise RuntimeError("ImageMagick is required but neither 'magick' nor 'convert' was found")


def render_preview_thumbnail(source_png: Path, preview_root: Path, box_size: int) -> Path:
    preview_root.mkdir(parents=True, exist_ok=True)
    resolved_source, source_token = file_identity(source_png)
    digest = hashlib.sha256(
        f"{resolved_source}::{source_token}::{box_size}".encode("utf-8")
    ).hexdigest()[:12]
    preview_png = preview_root / f"{sanitize_path_component(source_png.stem)}_{digest}_{box_size}.png"
    if preview_png.exists():
        touch_cache_path(preview_png)
        prune_cache_dir(preview_root, MAX_PREVIEW_THUMB_FILES)
        return preview_png

    image_tool = find_image_tool()
    temp_preview_png = preview_png.with_name(f"{preview_png.name}.tmp-{threading.get_ident()}")
    subprocess.run(
        [
            image_tool,
            str(source_png),
            "-background",
            "none",
            "-gravity",
            "center",
            "-resize",
            f"{box_size}x{box_size}",
            "-extent",
            f"{box_size}x{box_size}",
            str(temp_preview_png),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    temp_preview_png.replace(preview_png)
    touch_cache_path(preview_png)
    prune_cache_dir(preview_root, MAX_PREVIEW_THUMB_FILES)
    return preview_png


def set_readonly_text(widget: tk.Text, text: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")


def format_duration_ms(duration_ms: int) -> str:
    if duration_ms <= 0:
        return "--"
    return f"{duration_ms / 1000:.2f}s"


def quality_to_score(label: str) -> int:
    return {
        "excellent": 4,
        "good": 3,
        "acceptable": 2,
        "likely blurry": 1,
        "redraw recommended": 0,
    }.get(label, 0)


def summarize_metadata(source_path: Path, metadata: dict) -> dict:
    frames = metadata.get("frames", [])
    size_pairs = set()
    entry_count = 0
    hotspot_pairs = set()
    for frame in frames:
        entries = frame.get("entries", [])
        if not entries and "png" in frame:
            entries = [frame]
        for entry in entries:
            width = int(entry["width"])
            height = int(entry["height"])
            size_pairs.add((width, height))
            hotspot_pairs.add((int(entry.get("hotspot_x", 0)), int(entry.get("hotspot_y", 0))))
            entry_count += 1
    largest_native_size = max((max(width, height) for width, height in size_pairs), default=0)
    largest_native_area = max((width * height for width, height in size_pairs), default=0)
    size_summary = " / ".join(str(size) for size in sorted({max(width, height) for width, height in size_pairs})) or "--"
    return {
        "path": str(source_path),
        "filename": source_path.name,
        "relative_path": str(source_path),
        "source_type": metadata.get("asset_type", source_path.suffix.lower().lstrip(".") or "unknown"),
        "is_animated": len(frames) > 1,
        "frame_count": len(frames),
        "entry_count": entry_count,
        "delay_ms_total": sum(int(frame.get("delay_ms", 50)) for frame in frames),
        "largest_native_size": largest_native_size,
        "largest_native_area": largest_native_area,
        "native_sizes": [
            {"width": width, "height": height}
            for width, height in sorted(size_pairs)
        ],
        "size_summary": size_summary,
        "contains_non_square": any(width != height for width, height in size_pairs),
        "hotspot_summary": ", ".join(f"{x},{y}" for x, y in sorted(hotspot_pairs)[:3]) or "--",
        "low_priority_hits": [],
        "duplicate_basename_count": 0,
        "warnings": [],
    }


def frames_from_source_metadata(metadata: dict, target_size: int) -> list[dict]:
    frames = []
    for frame_index, frame in enumerate(metadata.get("frames", [])):
        entries = frame.get("entries", [])
        if not entries and "png" in frame:
            entries = [frame]
        if not entries:
            continue
        entry = choose_best_entry(entries, max(1, int(target_size)))
        frames.append(
            {
                "png": str(Path(entry["png"]).expanduser().resolve()),
                "delay_ms": max(1, int(frame.get("delay_ms", 50))),
                "width": int(entry["width"]),
                "height": int(entry["height"]),
                "hotspot_x": int(entry.get("hotspot_x", 0)),
                "hotspot_y": int(entry.get("hotspot_y", 0)),
                "frame_index": int(frame.get("frame_index", frame_index)),
                "nominal_size": int(max(int(entry["width"]), int(entry["height"]))),
            }
        )
    return frames


def badges_for_summary(summary: dict) -> str:
    animated = "ANI" if summary.get("is_animated") else "Static"
    source_type = str(summary.get("source_type", "unknown")).upper()
    frame_count = int(summary.get("frame_count", 0))
    size_summary = summary.get("size_summary", "--")
    return f"{animated} | {source_type} | {size_summary} | {frame_count} frame(s)"


def compact_path(path: str, max_len: int = 78) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3) :]


def native_nominal_sizes(summary: dict) -> list[int]:
    native_sizes = summary.get("native_sizes", [])
    values = sorted(
        {
            max(int(item["width"]), int(item["height"]))
            for item in native_sizes
            if isinstance(item, dict) and item.get("width") and item.get("height")
        }
    )
    if values:
        return values
    size_summary = str(summary.get("size_summary", "")).strip()
    if not size_summary or size_summary == "--":
        return []
    parsed = []
    for part in size_summary.split("/"):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(int(part))
        except ValueError:
            continue
    return sorted(set(parsed))


def quality_label_from_numeric(score: float) -> str:
    if score >= 3.55:
        return "excellent"
    if score >= 2.75:
        return "good"
    if score >= 1.95:
        return "acceptable"
    if score >= 1.0:
        return "likely blurry"
    return "redraw recommended"


def confidence_label(score: int) -> str:
    if score >= 3:
        return "high"
    if score >= 1:
        return "medium"
    return "low"


def recommended_redraw_master_size(target_sizes: list[int]) -> int:
    sizes = sorted(int(size) for size in target_sizes if int(size) > 0)
    if not sizes:
        return 64
    max_target = max(sizes)
    if max_target >= 192:
        return 128
    if max_target >= 128:
        return 128
    if max_target >= 96:
        return 96
    return max(64, max_target)


def summarize_match_details(match_details: dict) -> str:
    keywords = list(match_details.get("matched_keywords", []))
    partials = list(match_details.get("partial_keywords", []))
    label_tokens = list(match_details.get("matched_label_tokens", []))
    parts = []
    if keywords:
        parts.append("keyword hit: " + ", ".join(keywords))
    if partials:
        parts.append("partial name hit: " + ", ".join(partials))
    if label_tokens:
        parts.append("slot-label token hit: " + ", ".join(label_tokens))
    return "; ".join(parts) or "filename heuristic only"


def candidate_rank_gap_reason(candidate: dict, leader: dict | None) -> str:
    if leader is None or candidate.get("path") == leader.get("path"):
        return "top-ranked candidate"

    candidate_score = int(candidate.get("score", 0))
    leader_score = int(leader.get("score", 0))
    if candidate_score != leader_score:
        return f"score trails the leader by {leader_score - candidate_score} point(s)"

    candidate_low_priority = int(candidate.get("low_priority_hits", 0))
    leader_low_priority = int(leader.get("low_priority_hits", 0))
    if candidate_low_priority != leader_low_priority:
        return "same filename score, but folder priority is worse"

    candidate_depth = int(candidate.get("depth", 0))
    leader_depth = int(leader.get("depth", 0))
    if candidate_depth != leader_depth:
        return "same score, but it lives deeper in the pack"

    candidate_size = int(candidate.get("largest_native_size", 0))
    leader_size = int(leader.get("largest_native_size", 0))
    if candidate_size != leader_size:
        if candidate_size < leader_size:
            return "same score, but it offers less native detail"
        return "same score, but it only wins on filename/path ordering"

    return "same score, but it sorts lower after path-priority tie-breaks"


def infer_slot_warnings(
    slot_key: str,
    summary: dict,
    target_sizes: list[int],
    pack_analysis: dict | None = None,
    *,
    ambiguous_candidates: list[dict] | None = None,
) -> list[str]:
    warnings = list(summary.get("warnings", []))
    if summary.get("error"):
        warnings.append(summary["error"])
        return warnings

    max_target = max(target_sizes) if target_sizes else 0
    max_native = int(summary.get("largest_native_size", 0))
    if max_target and max_native < max_target:
        warnings.append(f"largest native detail is {max_native}px; larger outputs will be scaled")
    if max_target and max_native < 64:
        warnings.append("source detail is small for modern HiDPI cursor sizes")
    if summary.get("contains_non_square"):
        warnings.append("contains non-square native art; review the predicted Linux preview")
    if summary.get("low_priority_hits"):
        hits = ", ".join(sorted(set(summary["low_priority_hits"])))
        warnings.append(f"candidate is stored under generated/temp folders: {hits}")
    if int(summary.get("duplicate_basename_count", 0)) > 1:
        warnings.append("same filename appears elsewhere in the pack; confirm you picked the right copy")

    stem = Path(summary["filename"]).stem
    if slot_key == "default_pointer":
        progress_score = score_slot_match(stem, SLOT_BY_KEY["progress"])
        default_score = score_slot_match(stem, SLOT_BY_KEY["default_pointer"])
        if progress_score >= default_score and progress_score > 0:
            warnings.append("this default pointer name also looks like a progress/appstart cursor")

    ambiguous = ambiguous_candidates
    if ambiguous is None and pack_analysis is not None:
        ambiguous = pack_analysis.get("ambiguous_candidates", {}).get(slot_key, [])
    if ambiguous:
        selected_path = summary["path"]
        top_paths = {candidate["path"] for candidate in ambiguous}
        if selected_path in top_paths:
            warnings.append("slot choice is ambiguous based on filename heuristics alone")
    if pack_analysis is not None:
        hidpi = pack_analysis.get("hidpi_potential", {})
        if max_target >= 96 and hidpi.get("rating") in {"weak", "limited"}:
            warnings.append("pack-level HiDPI coverage is weak; smaller presets may be safer than pure conversion")
    return list(dict.fromkeys(warnings))


def build_slot_card_subtitle(summary: dict) -> str:
    animated = "ANI" if summary.get("is_animated") else "Static"
    source_type = str(summary.get("source_type", "unknown")).upper()
    size_summary = summary.get("size_summary", "--")
    hotspot_summary = summary.get("hotspot_summary", "--")
    return f"{animated} | {source_type} | {size_summary} | hotspot {hotspot_summary}"


def evaluate_quality_forecast(
    slot_key: str,
    summary: dict,
    target_sizes: list[int],
    pack_analysis: dict | None = None,
    candidate: dict | None = None,
    selection_context: dict | None = None,
) -> dict:
    if summary.get("error"):
        return {
            "label": "redraw recommended",
            "confidence": "low",
            "reason": "The source metadata could not be inspected cleanly.",
            "warnings": [summary["error"]],
            "actions": ["Inspect or replace the source asset before building."],
            "decision": "manual replacement required",
            "suggested_preset": SAFE_PRESET_LABEL,
        }
    if not target_sizes:
        return {
            "label": "acceptable",
            "confidence": "low",
            "reason": "No target sizes are configured yet.",
            "warnings": infer_slot_warnings(slot_key, summary, target_sizes, pack_analysis),
            "actions": ["Pick output sizes before trusting the forecast."],
            "decision": "configure build sizes first",
            "suggested_preset": None,
        }

    native_sizes = native_nominal_sizes(summary)
    max_target = max(target_sizes)
    max_native = int(summary.get("largest_native_size", 0))
    coverage_ratio = (max_native / max_target) if max_target else 1.0
    exact_hits = sum(1 for size in target_sizes if size in native_sizes)
    near_hits = sum(
        1
        for size in target_sizes
        if any(abs(native - size) <= max(2, int(size * 0.18)) for native in native_sizes)
    )
    scaled_sizes = [size for size in target_sizes if size > max_native]

    numeric_score = 0.0
    if coverage_ratio >= 1.0:
        numeric_score = 3.85
    elif coverage_ratio >= 0.85:
        numeric_score = 3.15
    elif coverage_ratio >= 0.65:
        numeric_score = 2.35
    elif coverage_ratio >= 0.45:
        numeric_score = 1.45
    else:
        numeric_score = 0.55

    if exact_hits:
        numeric_score += min(0.35, exact_hits * 0.08)
    elif near_hits:
        numeric_score += min(0.2, near_hits * 0.04)

    if len(native_sizes) <= 1 and len(target_sizes) >= 4:
        numeric_score -= 0.2
    if summary.get("contains_non_square"):
        numeric_score -= 0.12
    if summary.get("low_priority_hits"):
        numeric_score -= 0.18

    ambiguous = False
    if pack_analysis is not None:
        top_paths = {item["path"] for item in pack_analysis.get("ambiguous_candidates", {}).get(slot_key, [])}
        ambiguous = summary.get("path") in top_paths
        if ambiguous:
            numeric_score -= 0.2

    numeric_score = max(0.0, min(4.0, numeric_score))
    label = quality_label_from_numeric(numeric_score)

    confidence_score = 2
    if native_sizes:
        confidence_score += 1
    if exact_hits == 0 and near_hits == 0:
        confidence_score -= 1
    if ambiguous:
        confidence_score -= 1
    if summary.get("low_priority_hits"):
        confidence_score -= 1
    if candidate is not None and int(candidate.get("rank", 1) or 1) > 3:
        confidence_score -= 1
    confidence = confidence_label(confidence_score)

    if label == "excellent":
        reason = f"Native detail already reaches {max_native}px, so the requested ceiling is covered without upscale."
        decision = "build-ready"
    elif label == "good":
        reason = f"Largest native detail is {max_native}px; only moderate upscale remains for the biggest outputs."
        decision = "build-ready with review"
    elif label == "acceptable":
        scaled_text = ", ".join(str(size) for size in scaled_sizes[:3]) or str(max_target)
        reason = f"Smaller outputs are well-covered, but {scaled_text}px output will rely on visible scaling."
        decision = "compare before export"
    elif label == "likely blurry":
        reason = f"Most requested sizes outrun the {max_native}px source detail, so softness is likely at larger sizes."
        decision = "reduce preset or replace art"
    else:
        reason = f"Requested {max_target}px output is far beyond the available {max_native}px native detail."
        decision = "manual replacement required"

    warnings = infer_slot_warnings(slot_key, summary, target_sizes, pack_analysis)
    actions = []
    if ambiguous:
        actions.append("Compare the current choice against the next-ranked candidate before building.")
    if summary.get("low_priority_hits"):
        actions.append("Prefer the non-generated/root-level source if the visual compare looks equivalent.")
    if label in {"likely blurry", "redraw recommended"} and max_target >= 96:
        actions.append("Try Standard Linux or a smaller custom size set if you want to avoid large upscale.")
    if label == "redraw recommended":
        actions.append(
            f"Manual replacement/redraw is the safer path; aim for at least {recommended_redraw_master_size(target_sizes)}px source art."
        )
    elif label == "likely blurry":
        actions.append("Inspect the predicted Linux preview and decide whether softness is acceptable for this slot.")
    if summary.get("contains_non_square"):
        actions.append("Verify hotspot placement in the predicted Linux output before export.")
    if selection_context and selection_context.get("origin") == "fallback":
        actions.append("This slot currently reuses art from another role; replace it if that reuse looks wrong in context.")
    if not actions:
        actions.append("No immediate action needed beyond normal visual review.")

    suggested_preset = SAFE_PRESET_LABEL if (max_target >= 96 and max_native < 96) else None
    return {
        "label": label,
        "confidence": confidence,
        "reason": reason,
        "warnings": warnings,
        "actions": list(dict.fromkeys(actions)),
        "decision": decision,
        "suggested_preset": suggested_preset,
        "native_sizes": native_sizes,
        "coverage_ratio": coverage_ratio,
        "exact_hits": exact_hits,
        "near_hits": near_hits,
        "scaled_sizes": scaled_sizes,
    }


def score_quality(summary: dict, target_sizes: list[int]) -> tuple[str, str]:
    forecast = evaluate_quality_forecast("default_pointer", summary, target_sizes)
    return forecast["label"], forecast["reason"]


def inspect_animation_behavior(frames: list[dict]) -> dict:
    if not frames:
        return {
            "stats": "No frame timing available",
            "timeline": "",
            "warnings": [],
            "frame_rows": [],
        }

    delays = [max(1, int(frame.get("delay_ms", 50))) for frame in frames]
    total_ms = sum(delays)
    min_delay = min(delays)
    max_delay = max(delays)
    avg_delay = total_ms / len(delays)
    size_pairs = {(int(frame.get("width", 0)), int(frame.get("height", 0))) for frame in frames}
    hotspot_pairs = {(int(frame.get("hotspot_x", 0)), int(frame.get("hotspot_y", 0))) for frame in frames}
    if len(frames) <= 1:
        pacing = "static"
    elif avg_delay <= 45:
        pacing = "fast"
    elif avg_delay <= 90:
        pacing = "balanced"
    else:
        pacing = "slow"

    warnings = []
    if len(frames) > 1 and min_delay < 25:
        warnings.append("contains very fast frames and may feel flickery")
    if len(frames) > 1 and total_ms < 350:
        warnings.append("animation loop is very short and may feel busy")
    if len(frames) > 1 and max_delay >= max(4 * min_delay, min_delay + 120):
        warnings.append("frame delays vary sharply across the loop")
    if len(frames) >= 24:
        warnings.append("long animation sequence; inspect pacing before export")
    if any(int(frame.get("width", 0)) != int(frame.get("height", 0)) for frame in frames):
        warnings.append("contains non-square frames; verify the Linux output preview")
    if len(size_pairs) > 1:
        warnings.append("frame dimensions change across the loop and may cause visible jitter")
    if len(hotspot_pairs) > 1:
        warnings.append("hotspots move between frames; confirm the motion is intentional")

    timeline = " | ".join(f"{index + 1}:{delay}ms" for index, delay in enumerate(delays[:8]))
    if len(delays) > 8:
        timeline += " | ..."

    elapsed = 0
    frame_rows = []
    for index, frame in enumerate(frames, start=1):
        delay = max(1, int(frame.get("delay_ms", 50)))
        width = int(frame.get("width", 0))
        height = int(frame.get("height", 0))
        note_parts = []
        if delay == min_delay and len(frames) > 1:
            note_parts.append("fastest")
        if delay == max_delay and len(frames) > 1 and max_delay != min_delay:
            note_parts.append("longest hold")
        if width != height:
            note_parts.append("non-square")
        frame_rows.append(
            {
                "index": index,
                "delay_ms": delay,
                "start_ms": elapsed,
                "size": f"{width}x{height}",
                "hotspot": f"{int(frame.get('hotspot_x', 0))},{int(frame.get('hotspot_y', 0))}",
                "note": ", ".join(note_parts),
            }
        )
        elapsed += delay

    return {
        "stats": (
            f"{len(frames)} frame(s) | loop {format_duration_ms(total_ms)} | pacing {pacing} | "
            f"delay range {min_delay}-{max_delay}ms"
        ),
        "timeline": f"Frame strip: {timeline}",
        "warnings": warnings,
        "frame_rows": frame_rows,
    }


@dataclass(slots=True)
class SlotRenderState:
    path: str = ""
    loading: bool = False
    summary: dict | None = None
    quality: dict | None = None
    thumbnail_path: str | None = None
    error: str | None = None


def merge_pack_asset_summary(summary: dict, asset_summary: dict | None) -> dict:
    if asset_summary is None:
        return summary
    merged = dict(summary)
    merged["warnings"] = list(dict.fromkeys(asset_summary.get("warnings", [])))
    merged["relative_path"] = asset_summary.get("relative_path", merged["relative_path"])
    merged["source_type"] = asset_summary.get("source_type", merged["source_type"])
    merged["largest_native_size"] = asset_summary.get("largest_native_size", merged["largest_native_size"])
    merged["largest_native_area"] = asset_summary.get("largest_native_area", merged.get("largest_native_area", 0))
    merged["native_sizes"] = list(asset_summary.get("native_sizes", merged.get("native_sizes", [])))
    merged["size_summary"] = asset_summary.get("size_summary", merged["size_summary"])
    merged["contains_non_square"] = asset_summary.get("contains_non_square", merged["contains_non_square"])
    merged["low_priority_hits"] = list(asset_summary.get("low_priority_hits", merged.get("low_priority_hits", [])))
    merged["duplicate_basename_count"] = asset_summary.get(
        "duplicate_basename_count",
        merged.get("duplicate_basename_count", 0),
    )
    return merged


def source_metadata_cache_key_for(source_path: Path, preview_root: Path) -> tuple:
    resolved_text, dependency_token = source_cache_identity(source_path)
    return ("source-metadata", resolved_text, dependency_token, str(preview_root))


def summary_cache_key_for(source_path: Path, preview_root: Path) -> tuple:
    resolved_text, dependency_token = source_cache_identity(source_path)
    return ("source-summary", resolved_text, dependency_token, str(preview_root))


def output_preview_cache_key_for(
    source_path: Path,
    preview_root: Path,
    sizes: list[int],
    filter_name: str,
    preview_nominal_size: int,
) -> tuple:
    resolved_text, dependency_token = source_cache_identity(source_path)
    return (
        "output-preview",
        resolved_text,
        dependency_token,
        tuple(int(size) for size in sizes),
        filter_name,
        int(preview_nominal_size),
        str(preview_root),
    )


def touch_source_preview_artifacts(preview_root: Path, source_path: Path) -> None:
    if source_path.suffix.lower() in {".json", ".png"}:
        return
    source_cache_root = preview_root / "_source"
    touch_cache_path(cache_artifact_dir(source_cache_root, source_path))
    prune_cache_dir(source_cache_root, MAX_SOURCE_CACHE_DIRS)


def touch_output_preview_artifacts(preview_root: Path, preview: dict) -> None:
    output_cache_root = preview_root / "_output"
    frame_dirs = {
        str(Path(frame["png"]).expanduser().resolve().parent)
        for frame in preview.get("frames", [])
        if frame.get("png")
    }
    for frame_dir in frame_dirs:
        touch_cache_path(Path(frame_dir))
    prune_cache_dir(output_cache_root, MAX_OUTPUT_PREVIEW_DIRS)


def load_cached_source_metadata(
    source_path: Path,
    preview_root: Path,
    metadata_cache: BoundedCache[tuple, dict],
) -> dict:
    resolved = normalize_path(source_path)
    cache_key = source_metadata_cache_key_for(resolved, preview_root)
    cached = metadata_cache.get(cache_key)
    if cached is not None:
        touch_source_preview_artifacts(preview_root, resolved)
        return cached
    metadata = load_source_metadata(resolved, preview_root / "_source")
    touch_source_preview_artifacts(preview_root, resolved)
    return metadata_cache.set(cache_key, metadata)


def load_cached_summary(
    source_path: Path,
    preview_root: Path,
    metadata_cache: BoundedCache[tuple, dict],
    summary_cache: BoundedCache[tuple, dict],
    *,
    asset_summary: dict | None = None,
) -> dict:
    resolved = normalize_path(source_path)
    cache_key = summary_cache_key_for(resolved, preview_root)
    cached = summary_cache.get(cache_key)
    if cached is not None:
        return merge_pack_asset_summary(cached, asset_summary)
    metadata = load_cached_source_metadata(resolved, preview_root, metadata_cache)
    summary = summarize_metadata(resolved, metadata)
    stored_summary = summary_cache.set(cache_key, summary)
    return merge_pack_asset_summary(stored_summary, asset_summary)


def load_cached_output_preview(
    source_path: Path,
    preview_root: Path,
    sizes: list[int],
    filter_name: str,
    preview_nominal_size: int,
    metadata_cache: BoundedCache[tuple, dict],
    output_cache: BoundedCache[tuple, dict],
) -> dict:
    resolved = normalize_path(source_path)
    cache_key = output_preview_cache_key_for(resolved, preview_root, sizes, filter_name, preview_nominal_size)
    cached = output_cache.get(cache_key)
    if cached is not None:
        touch_output_preview_artifacts(preview_root, cached)
        return cached
    preview = prepare_output_preview_metadata(
        resolved,
        preview_root / "_output",
        sizes,
        scale_filter=filter_name,
        preview_nominal_size=preview_nominal_size,
        source_metadata=load_cached_source_metadata(resolved, preview_root, metadata_cache),
        source_cache_root=preview_root / "_source",
    )
    touch_output_preview_artifacts(preview_root, preview)
    return output_cache.set(cache_key, preview)


def build_slot_quality(
    slot_key: str,
    summary: dict,
    target_sizes: list[int],
    *,
    pack_analysis: dict | None = None,
    ambiguous_candidates: list[dict] | None = None,
    selection_context: dict | None = None,
) -> dict:
    quality_pack_analysis = pack_analysis
    if quality_pack_analysis is None and ambiguous_candidates is not None:
        quality_pack_analysis = {"ambiguous_candidates": {slot_key: ambiguous_candidates}}
    return evaluate_quality_forecast(
        slot_key,
        summary,
        target_sizes,
        pack_analysis=quality_pack_analysis,
        selection_context=selection_context,
    )


def build_animation_preview_payload(
    frames: list[dict],
    preview_root: Path,
    box_size: int,
    *,
    summary: str,
    frame_info: str,
) -> dict:
    if not frames:
        return {
            "frames": [],
            "thumbnail_paths": [],
            "summary": "No preview available",
            "frame_info": frame_info,
            "inspection_text": "",
            "warning_text": "",
        }
    thumbnail_paths = [
        str(render_preview_thumbnail(Path(frame["png"]), preview_root / "_thumbs", box_size))
        for frame in frames
    ]
    inspection = inspect_animation_behavior(frames)
    inspection_parts = [inspection["stats"]]
    if inspection["timeline"]:
        inspection_parts.append(inspection["timeline"])
    return {
        "frames": frames,
        "thumbnail_paths": thumbnail_paths,
        "summary": summary,
        "frame_info": frame_info,
        "inspection_text": " | ".join(part for part in inspection_parts if part),
        "warning_text": "; ".join(inspection["warnings"][:2]),
    }


def prepare_slot_card_payload(
    source_path: Path,
    preview_root: Path,
    preview_nominal_size: int,
    target_sizes: list[int],
    slot_key: str,
    metadata_cache: BoundedCache[tuple, dict],
    summary_cache: BoundedCache[tuple, dict],
    *,
    pack_analysis: dict | None = None,
    asset_summary: dict | None = None,
    ambiguous_candidates: list[dict] | None = None,
) -> dict:
    summary = load_cached_summary(
        source_path,
        preview_root,
        metadata_cache,
        summary_cache,
        asset_summary=asset_summary,
    )
    quality = build_slot_quality(
        slot_key,
        summary,
        target_sizes,
        pack_analysis=pack_analysis,
        ambiguous_candidates=ambiguous_candidates,
    )
    metadata = load_cached_source_metadata(source_path, preview_root, metadata_cache)
    frames = frames_from_source_metadata(metadata, preview_nominal_size)
    thumbnail_path = None
    if frames:
        thumbnail_path = str(render_preview_thumbnail(Path(frames[0]["png"]), preview_root / "_thumbs", CARD_PREVIEW_SIZE))
    return {
        "summary": summary,
        "quality": quality,
        "thumbnail_path": thumbnail_path,
    }


def prepare_source_preview_payload(
    source_path: Path,
    preview_root: Path,
    preview_nominal_size: int,
    metadata_cache: BoundedCache[tuple, dict],
) -> dict:
    metadata = load_cached_source_metadata(source_path, preview_root, metadata_cache)
    frames = frames_from_source_metadata(metadata, preview_nominal_size)
    if not frames:
        return {"reason": "No extracted frames available", "preview": None}
    summary = (
        f"{len(frames)} frame(s) | {format_duration_ms(sum(frame['delay_ms'] for frame in frames))} total | "
        f"using build-consistent native entries for {preview_nominal_size}px"
    )
    frame_info = f"Actual source timing preserved. First frame nominal size: {frames[0]['nominal_size']}px."
    return {
        "reason": None,
        "preview": build_animation_preview_payload(
            frames,
            preview_root,
            PLAYER_PREVIEW_SIZE,
            summary=summary,
            frame_info=frame_info,
        ),
    }


def prepare_output_preview_payload(
    source_path: Path,
    preview_root: Path,
    preview_nominal_size: int,
    target_sizes: list[int],
    scale_filter: str,
    metadata_cache: BoundedCache[tuple, dict],
    output_cache: BoundedCache[tuple, dict],
) -> dict:
    preview = load_cached_output_preview(
        source_path,
        preview_root,
        target_sizes,
        scale_filter,
        preview_nominal_size,
        metadata_cache,
        output_cache,
    )
    frames = preview["frames"]
    if not frames:
        return {"reason": "No predicted frames available", "preview": None}
    total_ms = sum(int(frame.get("delay_ms", 50)) for frame in frames)
    first = frames[0]
    frame_info = (
        f"Nominal size {preview['preview_nominal_size']}px | emitted PNG {first['width']}x{first['height']} | "
        f"filter {preview['scale_filter']}"
    )
    summary = f"{len(frames)} frame(s) | {format_duration_ms(total_ms)} total | built path preview"
    return {
        "reason": None,
        "preview": build_animation_preview_payload(
            frames,
            preview_root,
            PLAYER_PREVIEW_SIZE,
            summary=summary,
            frame_info=frame_info,
        ),
    }


def prepare_candidate_preview_payload(
    source_path: Path,
    preview_root: Path,
    preview_nominal_size: int,
    target_sizes: list[int],
    slot_key: str,
    metadata_cache: BoundedCache[tuple, dict],
    summary_cache: BoundedCache[tuple, dict],
    *,
    pack_analysis: dict | None = None,
    asset_summary: dict | None = None,
    ambiguous_candidates: list[dict] | None = None,
) -> dict:
    summary = load_cached_summary(
        source_path,
        preview_root,
        metadata_cache,
        summary_cache,
        asset_summary=asset_summary,
    )
    quality = build_slot_quality(
        slot_key,
        summary,
        target_sizes,
        pack_analysis=pack_analysis,
        ambiguous_candidates=ambiguous_candidates,
    )
    metadata = load_cached_source_metadata(source_path, preview_root, metadata_cache)
    frames = frames_from_source_metadata(metadata, preview_nominal_size)
    preview_payload = build_animation_preview_payload(
        frames,
        preview_root,
        CANDIDATE_PREVIEW_SIZE,
        summary=f"{len(frames)} frame(s) | {format_duration_ms(sum(frame['delay_ms'] for frame in frames))}",
        frame_info=f"Candidate path: {compact_path(str(normalize_path(source_path)), max_len=82)}",
    )
    return {
        "summary": summary,
        "quality": quality,
        "preview": preview_payload,
    }


class AnimationPreviewPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Widget, title: str, canvas_size: int, palette: dict[str, str]):
        super().__init__(master, text=title, padding=8)
        self.palette = palette
        self.canvas_size = canvas_size
        self.frames: list[dict] = []
        self.frame_images: list[tk.PhotoImage] = []
        self.current_index = 0
        self.running = False
        self.after_id: str | None = None
        self._strip_sync = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(7, weight=1)

        self.canvas = tk.Canvas(
            self,
            width=canvas_size,
            height=canvas_size,
            bg=self.palette["preview_bg"],
            highlightthickness=1,
            highlightbackground=self.palette["preview_border"],
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")

        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)

        self.play_button = ttk.Button(controls, text="Play", command=self.play)
        self.play_button.grid(row=0, column=0, padx=(0, 6))
        self.pause_button = ttk.Button(controls, text="Pause", command=self.pause)
        self.pause_button.grid(row=0, column=1, sticky="w")
        ttk.Button(controls, text="Replay", command=self.replay).grid(row=0, column=2, padx=(6, 6))
        ttk.Button(controls, text="Prev", command=self.step_prev).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(controls, text="Next", command=self.step_next).grid(row=0, column=4, padx=(0, 6))

        self.speed_var = tk.StringVar(value="1.0x")
        self.speed_combo = ttk.Combobox(
            controls,
            textvariable=self.speed_var,
            values=("0.5x", "1.0x", "1.5x", "2.0x"),
            state="readonly",
            width=6,
        )
        self.speed_combo.grid(row=0, column=5, sticky="e")
        self.speed_combo.bind("<<ComboboxSelected>>", lambda _event: self._restart_if_running())

        self.summary_var = tk.StringVar(value="No preview loaded")
        self.summary_label = ttk.Label(self, textvariable=self.summary_var, justify="left", wraplength=canvas_size + 40)
        self.summary_label.grid(row=2, column=0, sticky="w", pady=(6, 0))

        self.frame_info_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.frame_info_var, justify="left", wraplength=canvas_size + 40).grid(
            row=3,
            column=0,
            sticky="w",
            pady=(2, 0),
        )

        self.inspection_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.inspection_var, justify="left", wraplength=canvas_size + 40, style="Muted.TLabel").grid(
            row=4,
            column=0,
            sticky="w",
            pady=(2, 0),
        )

        self.warning_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.warning_var, justify="left", wraplength=canvas_size + 40, style="Warning.TLabel").grid(
            row=5,
            column=0,
            sticky="w",
            pady=(2, 0),
        )

        self.playhead_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.playhead_var, justify="left", wraplength=canvas_size + 40, style="Muted.TLabel").grid(
            row=6,
            column=0,
            sticky="w",
            pady=(2, 0),
        )

        strip_frame = ttk.Frame(self)
        strip_frame.grid(row=7, column=0, sticky="nsew", pady=(8, 0))
        strip_frame.columnconfigure(0, weight=1)
        strip_frame.rowconfigure(0, weight=1)
        self.frame_strip = ttk.Treeview(
            strip_frame,
            columns=("delay", "start", "size", "hotspot", "note"),
            show="tree headings",
            selectmode="browse",
            height=4,
        )
        self.frame_strip.heading("#0", text="#")
        self.frame_strip.heading("delay", text="Delay")
        self.frame_strip.heading("start", text="Start")
        self.frame_strip.heading("size", text="Size")
        self.frame_strip.heading("hotspot", text="Hotspot")
        self.frame_strip.heading("note", text="Timing Note")
        self.frame_strip.column("#0", width=42, anchor="center")
        self.frame_strip.column("delay", width=56, anchor="center")
        self.frame_strip.column("start", width=58, anchor="center")
        self.frame_strip.column("size", width=70, anchor="center")
        self.frame_strip.column("hotspot", width=70, anchor="center")
        self.frame_strip.column("note", width=max(140, canvas_size - 70), anchor="w")
        self.frame_strip.grid(row=0, column=0, sticky="nsew")
        strip_scroll = ttk.Scrollbar(strip_frame, orient="vertical", command=self.frame_strip.yview)
        strip_scroll.grid(row=0, column=1, sticky="ns")
        self.frame_strip.configure(yscrollcommand=strip_scroll.set)
        self.frame_strip.bind("<<TreeviewSelect>>", self._on_frame_strip_selected)

        self.clear("No preview loaded")

    def destroy(self) -> None:
        self._cancel_after()
        super().destroy()

    def set_title(self, title: str) -> None:
        self.configure(text=title)

    def _cancel_after(self) -> None:
        if self.after_id is not None:
            try:
                self.after_cancel(self.after_id)
            except Exception:  # noqa: BLE001
                pass
            self.after_id = None

    def clear(self, reason: str) -> None:
        self._cancel_after()
        self.frames = []
        self.frame_images = []
        self.current_index = 0
        self.running = False
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas_size // 2,
            self.canvas_size // 2,
            text="--",
            fill=self.palette["preview_placeholder"],
            font=("TkDefaultFont", 14, "bold"),
        )
        self.summary_var.set(reason)
        self.frame_info_var.set("")
        self.inspection_var.set("")
        self.warning_var.set("")
        self.playhead_var.set("")
        for item in self.frame_strip.get_children():
            self.frame_strip.delete(item)

    def set_loading(self, reason: str) -> None:
        self.clear(reason)
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas_size // 2,
            self.canvas_size // 2,
            text="...",
            fill=self.palette["preview_placeholder"],
            font=("TkDefaultFont", 14, "bold"),
        )

    def set_frames(
        self,
        frames: list[dict],
        images: list[tk.PhotoImage],
        summary: str,
        frame_info: str,
        inspection_text: str = "",
        warning_text: str = "",
    ) -> None:
        self._cancel_after()
        if not frames or not images:
            self.clear("No preview available")
            return
        self.frames = frames
        self.frame_images = images
        self.current_index = 0
        self.running = len(frames) > 1
        self.summary_var.set(summary)
        self.frame_info_var.set(frame_info)
        self.inspection_var.set(inspection_text)
        self.warning_var.set(warning_text)
        self._populate_frame_strip(frames)
        self._draw_current_frame()
        if self.running:
            self._schedule_next_frame()

    def _populate_frame_strip(self, frames: list[dict]) -> None:
        for item in self.frame_strip.get_children():
            self.frame_strip.delete(item)
        inspection = inspect_animation_behavior(frames)
        for row in inspection["frame_rows"]:
            iid = f"frame-{row['index'] - 1}"
            self.frame_strip.insert(
                "",
                "end",
                iid=iid,
                text=str(row["index"]),
                values=(
                    f"{row['delay_ms']}ms",
                    f"{row['start_ms']}ms",
                    row["size"],
                    row["hotspot"],
                    row["note"],
                ),
            )

    def _on_frame_strip_selected(self, _event=None) -> None:
        if self._strip_sync:
            return
        selection = self.frame_strip.selection()
        if not selection:
            return
        try:
            index = int(selection[0].split("-")[-1])
        except ValueError:
            return
        self.pause()
        self._select_frame(index)

    def _select_frame(self, index: int) -> None:
        if not self.frame_images:
            return
        self.current_index = max(0, min(index, len(self.frame_images) - 1))
        self._draw_current_frame()

    def _draw_current_frame(self) -> None:
        self.canvas.delete("all")
        if not self.frame_images:
            return
        self.canvas.create_image(
            self.canvas_size // 2,
            self.canvas_size // 2,
            image=self.frame_images[self.current_index],
        )
        self.canvas.create_text(
            8,
            self.canvas_size - 8,
            anchor="sw",
            fill=self.palette["preview_counter_text"],
            text=f"{self.current_index + 1}/{len(self.frame_images)}",
        )
        current = self.frames[self.current_index]
        self.playhead_var.set(
            f"Frame {self.current_index + 1}: {int(current.get('delay_ms', 50))}ms | "
            f"{int(current.get('width', 0))}x{int(current.get('height', 0))} | "
            f"hotspot {int(current.get('hotspot_x', 0))},{int(current.get('hotspot_y', 0))}"
        )
        current_iid = f"frame-{self.current_index}"
        if current_iid in self.frame_strip.get_children():
            self._strip_sync = True
            try:
                self.frame_strip.selection_set(current_iid)
                self.frame_strip.focus(current_iid)
                self.frame_strip.see(current_iid)
            finally:
                self._strip_sync = False

    def _speed_multiplier(self) -> float:
        raw = self.speed_var.get().rstrip("x")
        try:
            value = float(raw)
        except ValueError:
            return 1.0
        return max(0.1, value)

    def _schedule_next_frame(self) -> None:
        self._cancel_after()
        if not self.running or len(self.frames) <= 1:
            return
        delay_ms = max(1, int(self.frames[self.current_index].get("delay_ms", 50)))
        scaled_delay = max(1, int(delay_ms / self._speed_multiplier()))
        self.after_id = self.after(scaled_delay, self._advance_frame)

    def _advance_frame(self) -> None:
        if not self.running or len(self.frame_images) <= 1:
            return
        self.current_index = (self.current_index + 1) % len(self.frame_images)
        self._draw_current_frame()
        self._schedule_next_frame()

    def _restart_if_running(self) -> None:
        if self.running:
            self._schedule_next_frame()

    def play(self) -> None:
        if len(self.frame_images) <= 1:
            return
        self.running = True
        self._schedule_next_frame()

    def pause(self) -> None:
        self.running = False
        self._cancel_after()

    def replay(self) -> None:
        if not self.frame_images:
            return
        self.current_index = 0
        self._draw_current_frame()
        if len(self.frame_images) > 1:
            self.running = True
            self._schedule_next_frame()

    def step_prev(self) -> None:
        if not self.frame_images:
            return
        self.pause()
        self._select_frame((self.current_index - 1) % len(self.frame_images))

    def step_next(self) -> None:
        if not self.frame_images:
            return
        self.pause()
        self._select_frame((self.current_index + 1) % len(self.frame_images))


class ThemedTooltip:
    def __init__(
        self,
        widget: tk.Widget,
        text: str,
        palette: dict[str, str],
        *,
        delay_ms: int = 450,
        wraplength: int = 320,
    ):
        self.widget = widget
        self.text = text
        self.palette = palette
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self.after_id: str | None = None
        self.tipwindow: tk.Toplevel | None = None

        self.widget.bind("<Enter>", self._schedule_show, add="+")
        self.widget.bind("<Leave>", self._hide, add="+")
        self.widget.bind("<ButtonPress>", self._hide, add="+")
        self.widget.bind("<Destroy>", self._hide, add="+")

    def _schedule_show(self, _event=None) -> None:
        self._cancel_show()
        self.after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel_show(self) -> None:
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:  # noqa: BLE001
                pass
            self.after_id = None

    def _show(self) -> None:
        self.after_id = None
        if self.tipwindow is not None or not self.widget.winfo_exists():
            return

        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 10

        tipwindow = tk.Toplevel(self.widget)
        tipwindow.wm_overrideredirect(True)
        try:
            tipwindow.wm_attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        tipwindow.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            tipwindow,
            text=self.text,
            justify="left",
            wraplength=self.wraplength,
            bg=self.palette["panel_bg"],
            fg=self.palette["text"],
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            padx=8,
            pady=6,
        )
        label.pack()
        self.tipwindow = tipwindow

    def _hide(self, _event=None) -> None:
        self._cancel_show()
        if self.tipwindow is not None:
            try:
                self.tipwindow.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.tipwindow = None


class SlotCard(tk.Frame):
    def __init__(self, master: tk.Widget, slot: dict, app: "MappingApp"):
        super().__init__(master, bd=1, relief="solid", bg=app.palette["card_bg"], padx=8, pady=8)
        self.slot = slot
        self.app = app
        self.palette = app.palette
        self.preview_image: tk.PhotoImage | None = None

        self.grid_columnconfigure(1, weight=1)

        self.glyph = tk.Canvas(
            self,
            width=CANVAS_SLOT_GLYPH_SIZE,
            height=CANVAS_SLOT_GLYPH_SIZE,
            highlightthickness=0,
            bg=self.palette["card_bg"],
        )
        self.glyph.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 8))
        draw_slot_glyph(self.glyph, slot["key"], self.palette, bg=self.palette["card_bg"])

        self.title_label = tk.Label(
            self,
            text=slot["label"],
            bg=self.palette["card_bg"],
            fg=self.palette["card_text"],
            font=("TkDefaultFont", 10, "bold"),
        )
        self.title_label.grid(row=0, column=1, sticky="w")

        self.preview_canvas = tk.Canvas(
            self,
            width=CARD_PREVIEW_SIZE,
            height=CARD_PREVIEW_SIZE,
            bg=self.palette["preview_bg"],
            highlightthickness=1,
            highlightbackground=self.palette["preview_border"],
        )
        self.preview_canvas.grid(row=0, column=2, rowspan=3, padx=(10, 0), sticky="ne")

        self.file_label = tk.Label(self, text="Unassigned", bg=self.palette["card_bg"], fg=self.palette["card_text"], anchor="w")
        self.file_label.grid(row=1, column=1, sticky="w")

        self.badge_label = tk.Label(
            self,
            text="Assign a source to preview it",
            bg=self.palette["card_bg"],
            fg=self.palette["card_muted_text"],
            anchor="w",
        )
        self.badge_label.grid(row=2, column=1, sticky="w")

        self.path_label = tk.Label(
            self,
            text="",
            bg=self.palette["card_bg"],
            fg=self.palette["card_muted_text"],
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.path_label.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(4, 0))

        self.warning_label = tk.Label(
            self,
            text="",
            bg=self.palette["card_bg"],
            fg=self.palette["card_warning_text"],
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self.warning_label.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        button_row = ttk.Frame(self)
        button_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(button_row, text="Review", command=lambda: app.select_slot(slot["key"])).grid(row=0, column=0)
        ttk.Button(button_row, text="Browse", command=lambda: app.browse_slot(slot["key"])).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(button_row, text="Clear", command=lambda: app.clear_slot(slot["key"])).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(button_row, text="Candidates", command=lambda: app.focus_slot_candidates(slot["key"])).grid(row=0, column=3, padx=(6, 0))

        self.bind("<Button-1>", lambda _event: app.select_slot(slot["key"]))
        for child in self.winfo_children():
            try:
                child.bind("<Button-1>", lambda _event: app.select_slot(slot["key"]))
            except Exception:  # noqa: BLE001
                pass
        self.update_card(None, None, None, False)

    def update_card(
        self,
        path: str | None,
        summary: dict | None,
        quality: dict | None,
        selected: bool,
        *,
        loading_message: str | None = None,
    ) -> None:
        bg = self.palette["card_selected_bg"] if selected else self.palette["card_bg"]
        fg = self.palette["selection_text"] if selected else self.palette["card_text"]
        muted_fg = self.palette["selection_text"] if selected else self.palette["card_muted_text"]
        warning_fg = self.palette["selection_text"] if selected else self.palette["card_warning_text"]
        for widget in (self, self.glyph, self.title_label, self.file_label, self.badge_label, self.path_label, self.warning_label):
            widget.configure(bg=bg)
        self.title_label.configure(fg=fg)
        self.file_label.configure(fg=fg)
        self.badge_label.configure(fg=muted_fg)
        self.path_label.configure(fg=muted_fg)
        self.warning_label.configure(fg=warning_fg)
        draw_slot_glyph(self.glyph, self.slot["key"], self.palette, bg=bg)
        self.configure(highlightbackground=self.palette["selection_bg"] if selected else self.palette["border"], highlightthickness=1, bd=0)

        self.preview_canvas.delete("all")
        if self.preview_image is not None:
            self.preview_canvas.create_image(CARD_PREVIEW_SIZE // 2, CARD_PREVIEW_SIZE // 2, image=self.preview_image)
        else:
            self.preview_canvas.create_text(
                CARD_PREVIEW_SIZE // 2,
                CARD_PREVIEW_SIZE // 2,
                text="--",
                fill=self.palette["preview_placeholder"],
            )

        if not path:
            self.file_label.configure(text="Unassigned")
            self.badge_label.configure(text="Choose a source or use Auto-Fill")
            self.path_label.configure(text="")
            self.warning_label.configure(text="")
            return

        if loading_message:
            self.file_label.configure(text=f"{Path(path).name}   [loading]")
            self.badge_label.configure(text=loading_message)
            self.path_label.configure(text=compact_path(path))
            self.warning_label.configure(text="")
            return

        if summary is None:
            self.file_label.configure(text=Path(path).name)
            self.badge_label.configure(text="No summary available")
            self.path_label.configure(text=compact_path(path))
            self.warning_label.configure(text="")
            return

        quality_label = quality["label"] if quality else "--"
        self.file_label.configure(text=f"{Path(path).name}   [{quality_label}]")
        self.badge_label.configure(text=build_slot_card_subtitle(summary))
        self.path_label.configure(text=compact_path(path))
        warnings = quality["warnings"] if quality else []
        quality_reason = quality["reason"] if quality else ""
        self.warning_label.configure(text=warnings[0] if warnings else quality_reason)


def draw_slot_glyph(canvas: tk.Canvas, slot_key: str, palette: dict[str, str], bg: str = "white") -> None:
    canvas.configure(bg=bg)
    canvas.delete("all")
    color = palette["glyph_fg"]
    accent = palette["glyph_accent"]

    if slot_key == "default_pointer":
        canvas.create_polygon(6, 5, 6, 22, 11, 17, 14, 25, 17, 24, 14, 16, 21, 16, fill=accent, outline=color)
    elif slot_key == "help":
        canvas.create_text(14, 14, text="?", fill=accent, font=("TkDefaultFont", 14, "bold"))
    elif slot_key in {"progress", "wait"}:
        canvas.create_oval(5, 5, 23, 23, outline=color, width=2)
        canvas.create_arc(5, 5, 23, 23, start=35, extent=110, style="arc", outline=accent, width=3)
    elif slot_key == "text":
        canvas.create_line(10, 5, 18, 5, fill=color, width=2)
        canvas.create_line(14, 5, 14, 23, fill=accent, width=3)
        canvas.create_line(10, 23, 18, 23, fill=color, width=2)
    elif slot_key == "link_alias":
        canvas.create_line(6, 14, 18, 14, fill=accent, width=2)
        canvas.create_line(13, 9, 18, 14, fill=accent, width=2)
        canvas.create_line(13, 19, 18, 14, fill=accent, width=2)
        canvas.create_line(22, 8, 22, 20, fill=color, width=2)
        canvas.create_line(17, 14, 27, 14, fill=color, width=2)
    elif slot_key == "hand":
        canvas.create_text(14, 14, text="HAND", fill=accent, font=("TkDefaultFont", 7, "bold"))
    elif slot_key == "move":
        canvas.create_line(14, 4, 14, 24, fill=accent, width=2)
        canvas.create_line(4, 14, 24, 14, fill=accent, width=2)
        canvas.create_line(14, 4, 11, 7, fill=accent, width=2)
        canvas.create_line(14, 4, 17, 7, fill=accent, width=2)
        canvas.create_line(14, 24, 11, 21, fill=accent, width=2)
        canvas.create_line(14, 24, 17, 21, fill=accent, width=2)
        canvas.create_line(4, 14, 7, 11, fill=accent, width=2)
        canvas.create_line(4, 14, 7, 17, fill=accent, width=2)
        canvas.create_line(24, 14, 21, 11, fill=accent, width=2)
        canvas.create_line(24, 14, 21, 17, fill=accent, width=2)
    elif slot_key == "forbidden":
        canvas.create_oval(5, 5, 23, 23, outline=color, width=2)
        canvas.create_line(8, 20, 20, 8, fill=accent, width=3)
    elif slot_key == "resize_horizontal":
        canvas.create_line(4, 14, 24, 14, fill=accent, width=2)
        canvas.create_line(4, 14, 8, 10, fill=accent, width=2)
        canvas.create_line(4, 14, 8, 18, fill=accent, width=2)
        canvas.create_line(24, 14, 20, 10, fill=accent, width=2)
        canvas.create_line(24, 14, 20, 18, fill=accent, width=2)
    elif slot_key == "resize_vertical":
        canvas.create_line(14, 4, 14, 24, fill=accent, width=2)
        canvas.create_line(14, 4, 10, 8, fill=accent, width=2)
        canvas.create_line(14, 4, 18, 8, fill=accent, width=2)
        canvas.create_line(14, 24, 10, 20, fill=accent, width=2)
        canvas.create_line(14, 24, 18, 20, fill=accent, width=2)
    elif slot_key == "resize_diag_back":
        canvas.create_line(6, 22, 22, 6, fill=accent, width=2)
        canvas.create_line(6, 22, 6, 16, fill=accent, width=2)
        canvas.create_line(6, 22, 12, 22, fill=accent, width=2)
        canvas.create_line(22, 6, 16, 6, fill=accent, width=2)
        canvas.create_line(22, 6, 22, 12, fill=accent, width=2)
    elif slot_key == "resize_diag_forward":
        canvas.create_line(6, 6, 22, 22, fill=accent, width=2)
        canvas.create_line(6, 6, 12, 6, fill=accent, width=2)
        canvas.create_line(6, 6, 6, 12, fill=accent, width=2)
        canvas.create_line(22, 22, 16, 22, fill=accent, width=2)
        canvas.create_line(22, 22, 22, 16, fill=accent, width=2)
    elif slot_key == "crosshair":
        canvas.create_line(14, 4, 14, 24, fill=accent, width=2)
        canvas.create_line(4, 14, 24, 14, fill=accent, width=2)
        canvas.create_oval(10, 10, 18, 18, outline=color, width=1)
    elif slot_key == "pen":
        canvas.create_line(7, 21, 20, 8, fill=accent, width=3)
        canvas.create_polygon(20, 8, 23, 5, 24, 9, fill=color, outline=color)
    else:
        canvas.create_text(14, 14, text="*", fill=accent, font=("TkDefaultFont", 14, "bold"))


class MappingApp:
    def __init__(self, root: tk.Tk, palette_path: Path | None = None):
        self.root = root
        self.root.title("CursorForge")
        self.root.geometry("1560x1040")
        self.palette, self.palette_path, self.palette_name = load_gui_palette(palette_path)
        self.style = ttk.Style(root)

        self.current_mapping_path: Path | None = None
        self.last_tar_path: Path | None = None
        self.last_theme_dir: Path | None = None
        self.pack_analysis: dict | None = None
        self.pack_asset_lookup: dict[str, dict] = {}
        self.selected_slot_key = SLOT_DEFS[0]["key"]
        self.selected_candidate_path: str | None = None
        self.slot_selection_context: dict[str, dict] = {}
        self.analysis_action_items: dict[str, dict] = {}

        self.preview_photo_cache: BoundedCache[tuple, tk.PhotoImage] = BoundedCache(max_entries=384)
        self.source_metadata_cache: BoundedCache[tuple, dict] = BoundedCache(max_entries=128)
        self.output_preview_cache: BoundedCache[tuple, dict] = BoundedCache(max_entries=96)
        self.summary_cache: BoundedCache[tuple, dict] = BoundedCache(max_entries=192)
        self.slot_states = {slot["key"]: SlotRenderState() for slot in SLOT_DEFS}
        self.request_tracker = RequestTracker()
        self.task_runner = GuiTaskRunner(self.root)
        self.refresh_coalescer = TkAfterCoalescer(self.root)
        self._suspend_refresh_traces = False
        self.analysis_busy = False
        self.auto_prepare_busy = False
        self.build_busy = False
        self.tooltips: list[ThemedTooltip] = []
        self.slot_paths = {slot["key"]: "" for slot in SLOT_DEFS}
        self.profile_base_preset_label = resolve_build_preset("hidpi-kde")["label"]

        self.source_dir_var = tk.StringVar()
        self.work_root_var = tk.StringVar(value=str(DEFAULT_WORK_ROOT))
        self.theme_name_var = tk.StringVar(value="Custom-cursor")
        self.scale_filter_var = tk.StringVar(value=DEFAULT_SCALE_FILTER)
        self.summary_var = tk.StringVar(value="No slots assigned yet")
        self.status_var = tk.StringVar(value="Ready")
        self.target_sizes = list(DEFAULT_CURSOR_SIZES)
        self.target_sizes_var = tk.StringVar(value=format_cursor_sizes(self.target_sizes))
        self.build_preset_var = tk.StringVar(value=resolve_build_preset("hidpi-kde")["label"])
        self.preview_nominal_size_var = tk.StringVar(value=str(self._default_preview_size(self.target_sizes)))
        self.preset_description_var = tk.StringVar(value=describe_build_preset(self.build_preset_var.get()))
        self.current_build_profile: BuildProfileState = resolve_build_profile_state(
            self.target_sizes,
            self.scale_filter_var.get(),
            base_preset_label=self.profile_base_preset_label,
        )
        self.profile_state_var = tk.StringVar(value=self.current_build_profile.headline)
        self.profile_match_var = tk.StringVar(value=self.current_build_profile.detail)
        self.overall_quality_var = tk.StringVar(value="Overall quality forecast: --")
        self.readiness_var = tk.StringVar(value="Pack readiness: --")
        self.readiness_detail_var = tk.StringVar(value="Build and review guidance will appear here.")
        self.review_queue_var = tk.StringVar(value="Review queue: --")
        self.review_queue_hint_var = tk.StringVar(
            value="Auto-fill or assign slots, then use Compare to clear ambiguity and export risk."
        )
        self.last_output_var = tk.StringVar(value="No build output yet")
        self.compare_mode_var = tk.StringVar(value=COMPARE_MODE_CURRENT_VS_CANDIDATE)
        self.compare_preset_var = tk.StringVar(value=SAFE_PRESET_LABEL)
        self.compare_summary_var = tk.StringVar(value="Select a slot and candidate to compare.")
        self.compare_hint_var = tk.StringVar(value="")
        self.analysis_action_detail_var = tk.StringVar(value="Analyze a pack to build an action queue.")
        self.palette_name_var = tk.StringVar(
            value=f"GUI palette: {self.palette_name} ({self.palette_path.name})" if self.palette_path else "GUI palette: built-in"
        )

        self._apply_palette()
        self._build_ui()
        self._refresh_all_views()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.target_sizes_var.trace_add("write", lambda *_: self._on_target_sizes_changed())
        self.scale_filter_var.trace_add("write", lambda *_: self._on_scale_filter_changed())
        self.preview_nominal_size_var.trace_add("write", lambda *_: self._on_preview_size_changed())
        self.build_preset_var.trace_add("write", lambda *_: self._update_preset_description())
        self.compare_preset_var.trace_add("write", lambda *_: self._schedule_compare_view_refresh())

    def _refresh_build_profile_state(self) -> None:
        sizes, _size_error = self.try_target_sizes()
        scale_filter = self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER
        self.current_build_profile = resolve_build_profile_state(
            sizes,
            scale_filter,
            base_preset_label=self.profile_base_preset_label,
        )
        self.profile_state_var.set(self.current_build_profile.headline)
        self.profile_match_var.set(self.current_build_profile.detail)

    def _cancel_scheduled_refreshes(self) -> None:
        self.refresh_coalescer.cancel_many(
            "build-settings-refresh",
            "preview-size-refresh",
            "selected-detail-refresh",
            "candidate-detail-refresh",
            "compare-view-refresh",
        )

    def _schedule_build_settings_refresh(self) -> None:
        self.refresh_coalescer.schedule("build-settings-refresh", BUILD_SETTINGS_REFRESH_MS, self._run_build_settings_refresh)

    def _run_build_settings_refresh(self) -> None:
        self._update_preview_size_choices()
        self.output_preview_cache.clear()
        self._refresh_all_views()

    def _schedule_preview_size_refresh(self) -> None:
        self.refresh_coalescer.schedule("preview-size-refresh", PREVIEW_SIZE_REFRESH_MS, self._run_preview_size_refresh)

    def _run_preview_size_refresh(self) -> None:
        self._refresh_slot_cards()
        self._refresh_selected_slot_detail()

    def _schedule_selected_slot_detail_refresh(self) -> None:
        self.refresh_coalescer.schedule(
            "selected-detail-refresh",
            SELECTED_DETAIL_REFRESH_MS,
            self._refresh_selected_slot_detail,
        )

    def _schedule_candidate_detail_refresh(self) -> None:
        self.refresh_coalescer.schedule(
            "candidate-detail-refresh",
            CANDIDATE_DETAIL_REFRESH_MS,
            self._refresh_candidate_detail,
        )

    def _schedule_compare_view_refresh(self) -> None:
        self.refresh_coalescer.schedule("compare-view-refresh", COMPARE_REFRESH_MS, self._refresh_compare_view)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="CursorForge",
            font=("", 13, "bold"),
            style="Heading.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=(
                "Analyze the source pack, review or correct visual slot assignments, preview real animation behavior, "
                "then build and export the final Linux cursor theme."
            ),
            wraplength=1450,
            justify="left",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 4))
        ttk.Label(header, textvariable=self.palette_name_var, style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 10))

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        self.analysis_tab = ttk.Frame(self.notebook, padding=10)
        self.review_tab = ttk.Frame(self.notebook, padding=10)
        self.build_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.analysis_tab, text="1. Source Pack Analysis")
        self.notebook.add(self.review_tab, text="2. Slot Review / Correction")
        self.notebook.add(self.build_tab, text="3. Build / Export")

        self._build_analysis_tab()
        self._build_review_tab()
        self._build_build_tab()

        status_bar = ttk.Frame(outer)
        status_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        status_bar.columnconfigure(1, weight=1)
        ttk.Label(status_bar, textvariable=self.summary_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))

    def _apply_palette(self) -> None:
        palette = self.palette
        self.root.configure(bg=palette["root_bg"])
        self.root.tk_setPalette(
            background=palette["root_bg"],
            foreground=palette["text"],
            activeBackground=palette["selection_bg"],
            activeForeground=palette["selection_text"],
            highlightColor=palette["selection_bg"],
            selectBackground=palette["selection_bg"],
            selectForeground=palette["selection_text"],
        )
        option_pairs = {
            "*Menu.background": palette["panel_bg"],
            "*Menu.foreground": palette["text"],
            "*Menu.activeBackground": palette["selection_bg"],
            "*Menu.activeForeground": palette["selection_text"],
            "*Menu.selectColor": palette["accent"],
            "*Listbox.background": palette["entry_bg"],
            "*Listbox.foreground": palette["entry_fg"],
            "*Listbox.selectBackground": palette["selection_bg"],
            "*Listbox.selectForeground": palette["selection_text"],
            "*Listbox.highlightBackground": palette["border"],
            "*Listbox.highlightColor": palette["selection_bg"],
            "*Entry.background": palette["entry_bg"],
            "*Entry.foreground": palette["entry_fg"],
            "*Entry.insertBackground": palette["text"],
            "*Text.background": palette["content_bg"],
            "*Text.foreground": palette["text"],
            "*Text.insertBackground": palette["text"],
            "*Text.selectBackground": palette["selection_bg"],
            "*Text.selectForeground": palette["selection_text"],
            "*TCombobox*Listbox.background": palette["entry_bg"],
            "*TCombobox*Listbox.foreground": palette["entry_fg"],
            "*TCombobox*Listbox.selectBackground": palette["selection_bg"],
            "*TCombobox*Listbox.selectForeground": palette["selection_text"],
            "*TCombobox*Listbox.highlightBackground": palette["border"],
            "*TCombobox*Listbox.highlightColor": palette["selection_bg"],
            "*TCombobox*Listbox.font": "TkDefaultFont",
            "*TCombobox*Listbox.relief": "flat",
            "*TCombobox*Listbox.borderWidth": 0,
            "*tearOff": 0,
        }
        for pattern, value in option_pairs.items():
            self.root.option_add(pattern, value)

        self.style.configure(".", background=palette["root_bg"], foreground=palette["text"])
        self.style.configure("TFrame", background=palette["root_bg"])
        self.style.configure("TLabel", background=palette["root_bg"], foreground=palette["text"])
        self.style.configure("Heading.TLabel", background=palette["root_bg"], foreground=palette["heading_text"])
        self.style.configure("Muted.TLabel", background=palette["root_bg"], foreground=palette["muted_text"])
        self.style.configure("Warning.TLabel", background=palette["root_bg"], foreground=palette["warning"])
        self.style.configure("Status.TLabel", background=palette["root_bg"], foreground=palette["status_text"])
        self.style.configure("TLabelframe", background=palette["root_bg"], bordercolor=palette["border"])
        self.style.configure("TLabelframe.Label", background=palette["root_bg"], foreground=palette["heading_text"])
        self.style.configure("TButton", background=palette["button_bg"], foreground=palette["button_fg"], bordercolor=palette["border"])
        self.style.map(
            "TButton",
            background=[("active", palette["selection_bg"]), ("pressed", palette["selection_bg"])],
            foreground=[("active", palette["selection_text"]), ("pressed", palette["selection_text"])],
        )
        self.style.configure(
            "TEntry",
            fieldbackground=palette["entry_bg"],
            foreground=palette["entry_fg"],
            bordercolor=palette["border"],
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=palette["entry_bg"],
            foreground=palette["entry_fg"],
            background=palette["panel_bg"],
            bordercolor=palette["border"],
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["entry_bg"])],
            foreground=[("readonly", palette["entry_fg"])],
            selectbackground=[("readonly", palette["selection_bg"])],
            selectforeground=[("readonly", palette["selection_text"])],
        )
        self.style.configure(
            "Treeview",
            background=palette["tree_bg"],
            fieldbackground=palette["tree_bg"],
            foreground=palette["tree_fg"],
            bordercolor=palette["border"],
        )
        self.style.map(
            "Treeview",
            background=[("selected", palette["tree_selected_bg"])],
            foreground=[("selected", palette["tree_selected_fg"])],
        )
        self.style.configure(
            "Treeview.Heading",
            background=palette["panel_bg"],
            foreground=palette["heading_text"],
            bordercolor=palette["border"],
        )
        self.style.map("Treeview.Heading", background=[("active", palette["card_selected_bg"])])
        self.style.configure("TNotebook", background=palette["root_bg"], bordercolor=palette["border"])
        self.style.configure(
            "TNotebook.Tab",
            background=palette["panel_bg"],
            foreground=palette["text"],
            bordercolor=palette["border"],
        )
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", palette["card_selected_bg"])],
            foreground=[("selected", palette["selection_text"])],
        )
        self._install_file_dialog_theme_hook()

    def _install_file_dialog_theme_hook(self) -> None:
        palette = self.palette
        script = """
namespace eval ::cursorforge::filedialogtheme {
    variable bg {%s}
    variable fg {%s}
    variable border {%s}
    variable history_paths
    variable history_index
    variable in_navigation
}

proc ::cursorforge::filedialogtheme::ensure_tk_dialog_commands {} {
    global tk_library

    if {![llength [info commands ::tk::dialog::file::Create]]} {
        catch {source [file join $tk_library tkfbox.tcl]}
    }
    if {![llength [info commands ::tk::dialog::file::chooseDir::]]} {
        catch {source [file join $tk_library choosedir.tcl]}
    }
}

proc ::cursorforge::filedialogtheme::current_path {w} {
    upvar ::tk::dialog::file::[winfo name $w] data
    if {![info exists data(selectPath)]} {
        return [pwd]
    }
    if {[catch {file normalize $data(selectPath)} normalized]} {
        return $data(selectPath)
    }
    return $normalized
}

proc ::cursorforge::filedialogtheme::update_button_states {w} {
    variable history_paths
    variable history_index

    if {![winfo exists $w.contents.nav]} {
        return
    }

    set back_state disabled
    set forward_state disabled
    if {[info exists history_index($w)]} {
        if {$history_index($w) > 0} {
            set back_state normal
        }
        if {[info exists history_paths($w)] && $history_index($w) < ([llength $history_paths($w)] - 1)} {
            set forward_state normal
        }
    }

    set current [current_path $w]
    set up_state normal
    if {$current eq [file dirname $current]} {
        set up_state disabled
    }

    set home_state normal
    if {[catch {file normalize ~} home] || $home eq $current} {
        set home_state disabled
    }

    $w.contents.nav.back configure -state $back_state
    $w.contents.nav.forward configure -state $forward_state
    $w.contents.nav.up configure -state $up_state
    $w.contents.nav.home configure -state $home_state
}

proc ::cursorforge::filedialogtheme::remember_current_path {w} {
    variable history_paths
    variable history_index

    if {![winfo exists $w]} {
        return
    }

    set path [current_path $w]
    if {[info exists history_paths($w)] && [info exists history_index($w)]} {
        if {[lindex $history_paths($w) $history_index($w)] eq $path} {
            update_button_states $w
            return
        }
    }

    set history_paths($w) [list $path]
    set history_index($w) 0
    update_button_states $w
}

proc ::cursorforge::filedialogtheme::record_path {w path} {
    variable history_paths
    variable history_index
    variable in_navigation

    if {![winfo exists $w]} {
        return
    }

    if {[catch {file normalize $path} normalized]} {
        set normalized $path
    }

    if {[info exists in_navigation($w)] && $in_navigation($w)} {
        update_button_states $w
        return
    }

    if {![info exists history_paths($w)] || ![info exists history_index($w)]} {
        set history_paths($w) [list $normalized]
        set history_index($w) 0
        update_button_states $w
        return
    }

    if {[lindex $history_paths($w) $history_index($w)] eq $normalized} {
        update_button_states $w
        return
    }

    set trimmed [lrange $history_paths($w) 0 $history_index($w)]
    lappend trimmed $normalized
    set history_paths($w) $trimmed
    set history_index($w) [expr {[llength $trimmed] - 1}]
    update_button_states $w
}

proc ::cursorforge::filedialogtheme::navigate_to_index {w idx} {
    variable history_paths
    variable history_index
    variable in_navigation

    if {![info exists history_paths($w)] || ![info exists history_index($w)]} {
        return
    }
    if {$idx < 0 || $idx >= [llength $history_paths($w)]} {
        return
    }

    upvar ::tk::dialog::file::[winfo name $w] data
    set history_index($w) $idx
    set in_navigation($w) 1
    set data(selectPath) [lindex $history_paths($w) $idx]
    set in_navigation($w) 0
    update_button_states $w
}

proc ::cursorforge::filedialogtheme::go_back {w} {
    variable history_index
    if {![info exists history_index($w)] || $history_index($w) <= 0} {
        return
    }
    navigate_to_index $w [expr {$history_index($w) - 1}]
}

proc ::cursorforge::filedialogtheme::go_forward {w} {
    variable history_index
    variable history_paths
    if {![info exists history_index($w)] || ![info exists history_paths($w)]} {
        return
    }
    if {$history_index($w) >= [llength $history_paths($w)] - 1} {
        return
    }
    navigate_to_index $w [expr {$history_index($w) + 1}]
}

proc ::cursorforge::filedialogtheme::go_up {w} {
    upvar ::tk::dialog::file::[winfo name $w] data
    set current [current_path $w]
    set parent [file dirname $current]
    if {$parent ne $current} {
        set data(selectPath) $parent
    }
}

proc ::cursorforge::filedialogtheme::go_home {w} {
    if {[catch {file normalize ~} home]} {
        return
    }
    upvar ::tk::dialog::file::[winfo name $w] data
    set data(selectPath) $home
}

proc ::cursorforge::filedialogtheme::ensure_nav {w} {
    if {![winfo exists $w.contents.nav]} {
        set nav [ttk::frame $w.contents.nav]
        ttk::button $nav.home -text "Home" -command [list ::cursorforge::filedialogtheme::go_home $w]
        ttk::button $nav.back -text "Back" -command [list ::cursorforge::filedialogtheme::go_back $w]
        ttk::button $nav.forward -text "Forward" -command [list ::cursorforge::filedialogtheme::go_forward $w]
        ttk::button $nav.up -text "Up" -command [list ::cursorforge::filedialogtheme::go_up $w]
        pack $nav.home $nav.back $nav.forward $nav.up -side left -padx {0 6}
        pack $nav -side top -fill x -before $w.contents.f1 -padx 4 -pady {4 0}
    }

    if {[winfo exists $w.contents.f1.up]} {
        catch {pack forget $w.contents.f1.up}
    }
}

proc ::cursorforge::filedialogtheme::apply {w} {
    variable bg
    variable fg
    variable border

    ensure_nav $w

    set icons $w.contents.icons
    set canvas $icons.canvas
    if {![winfo exists $canvas]} {
        return
    }

    catch {$canvas configure -background $bg}
    catch {$canvas configure -highlightbackground $border -highlightcolor $border}
    catch {$canvas itemconfigure text -fill $fg}

    if {![catch {info object namespace $icons} icon_ns] && $icon_ns ne ""} {
        catch {namespace eval $icon_ns [list set fill $fg]}
    }

    remember_current_path $w
}

if {![llength [info commands ::tk::dialog::file::Create__cursorforge_orig]]} {
    ::cursorforge::filedialogtheme::ensure_tk_dialog_commands
    if {[llength [info commands ::tk::dialog::file::Create]]} {
        rename ::tk::dialog::file::Create ::tk::dialog::file::Create__cursorforge_orig
        proc ::tk::dialog::file::Create {w class} {
            ::tk::dialog::file::Create__cursorforge_orig $w $class
            catch {::cursorforge::filedialogtheme::apply $w}
        }
    }
}

if {![llength [info commands ::tk::dialog::file::SetPath__cursorforge_orig]]} {
    ::cursorforge::filedialogtheme::ensure_tk_dialog_commands
    if {[llength [info commands ::tk::dialog::file::SetPath]]} {
        rename ::tk::dialog::file::SetPath ::tk::dialog::file::SetPath__cursorforge_orig
        proc ::tk::dialog::file::SetPath {w name1 name2 op} {
            ::tk::dialog::file::SetPath__cursorforge_orig $w $name1 $name2 $op
            if {[winfo exists $w]} {
                upvar ::tk::dialog::file::[winfo name $w] data
                if {[info exists data(selectPath)]} {
                    catch {::cursorforge::filedialogtheme::record_path $w $data(selectPath)}
                }
            }
        }
    }
}
""" % (palette["entry_bg"], palette["entry_fg"], palette["border"])
        self.root.tk.eval(script)

    def _theme_text_widget(self, widget: tk.Text, *, bg_key: str = "content_bg", fg_key: str = "text") -> None:
        widget.configure(
            bg=self.palette[bg_key],
            fg=self.palette[fg_key],
            insertbackground=self.palette["text"],
            selectbackground=self.palette["selection_bg"],
            selectforeground=self.palette["selection_text"],
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["selection_bg"],
            relief="flat",
            bd=0,
        )

    def _attach_tooltip(self, widget: tk.Widget, text: str) -> None:
        self.tooltips.append(ThemedTooltip(widget, text, self.palette))

    def _build_analysis_tab(self) -> None:
        self.analysis_tab.columnconfigure(0, weight=1)
        self.analysis_tab.rowconfigure(3, weight=1)

        controls = ttk.LabelFrame(self.analysis_tab, text="Stage 1: Analyze The Source Pack", padding=10)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Windows cursor folder").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(controls, textvariable=self.source_dir_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(controls, text="Browse", command=self.choose_source_dir).grid(row=0, column=2, padx=(8, 0), pady=3)
        self.analyze_button = ttk.Button(controls, text="Analyze Pack", command=self.analyze_pack)
        self.analyze_button.grid(row=0, column=3, padx=(8, 0), pady=3)
        self._attach_tooltip(
            self.analyze_button,
            "Checks the cursor pack and shows what it contains, how much animation and HiDPI detail it has, and any obvious problems.",
        )
        self.auto_fill_button = ttk.Button(controls, text="Auto-Fill From Pack", command=self.auto_prepare)
        self.auto_fill_button.grid(row=0, column=4, padx=(8, 0), pady=3)
        self._attach_tooltip(
            self.auto_fill_button,
            "Fills in the slot guesses for you using the pack data and filenames. Good starting point before fixing anything by hand.",
        )

        ttk.Label(controls, text="Pack analysis").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=(12, 3))
        ttk.Label(
            controls,
            text=(
                "This stage summarizes pack quality before any slot correction: native sizes, animation coverage, "
                "duplicate artifacts, likely HiDPI potential, and ambiguous filename matches."
            ),
            wraplength=1120,
            justify="left",
        ).grid(row=1, column=1, columnspan=4, sticky="w", pady=(12, 3))

        snapshot = ttk.Frame(self.analysis_tab)
        snapshot.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        for column in range(4):
            snapshot.columnconfigure(column, weight=1)

        self.analysis_total_value_var = tk.StringVar(value="--")
        self.analysis_total_note_var = tk.StringVar(value="Source files")
        self.analysis_animation_value_var = tk.StringVar(value="--")
        self.analysis_animation_note_var = tk.StringVar(value="Animated sources")
        self.analysis_hidpi_value_var = tk.StringVar(value="--")
        self.analysis_hidpi_note_var = tk.StringVar(value="HiDPI potential")
        self.analysis_attention_value_var = tk.StringVar(value="--")
        self.analysis_attention_note_var = tk.StringVar(value="Warnings and ambiguity")

        metric_specs = [
            ("Source Files", self.analysis_total_value_var, self.analysis_total_note_var),
            ("Animated", self.analysis_animation_value_var, self.analysis_animation_note_var),
            ("HiDPI", self.analysis_hidpi_value_var, self.analysis_hidpi_note_var),
            ("Attention", self.analysis_attention_value_var, self.analysis_attention_note_var),
        ]
        for column, (title, value_var, note_var) in enumerate(metric_specs):
            card = ttk.LabelFrame(snapshot, text=title, padding=10)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0))
            ttk.Label(card, textvariable=value_var, font=("", 14, "bold"), style="Heading.TLabel").grid(
                row=0,
                column=0,
                sticky="w",
            )
            ttk.Label(card, textvariable=note_var, style="Muted.TLabel", wraplength=240, justify="left").grid(
                row=1,
                column=0,
                sticky="w",
                pady=(4, 0),
            )

        overview = ttk.Frame(self.analysis_tab)
        overview.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        overview.columnconfigure(0, weight=1)
        overview.columnconfigure(1, weight=1)

        left = ttk.LabelFrame(overview, text="Pack Overview", padding=10)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.columnconfigure(1, weight=1)
        self.analysis_counts_var = tk.StringVar(value="Files: --")
        self.analysis_inf_var = tk.StringVar(value="INF: --")
        self.analysis_hidpi_var = tk.StringVar(value="HiDPI: --")
        self.analysis_sizes_var = tk.StringVar(value="Largest native sizes: --")
        self.analysis_animated_var = tk.StringVar(value="Animated sources: --")
        for row_index, variable in enumerate(
            (
                self.analysis_counts_var,
                self.analysis_inf_var,
                self.analysis_hidpi_var,
                self.analysis_sizes_var,
                self.analysis_animated_var,
            )
        ):
            ttk.Label(left, textvariable=variable, justify="left", wraplength=620).grid(
                row=row_index,
                column=0,
                sticky="w",
                pady=2,
            )

        right = ttk.LabelFrame(overview, text="Warnings And Diagnostics", padding=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self.analysis_detail_text = tk.Text(right, height=8, wrap="word")
        self.analysis_detail_text.grid(row=0, column=0, sticky="nsew")
        self._theme_text_widget(self.analysis_detail_text)
        set_readonly_text(self.analysis_detail_text, "Analyze a source pack to see diagnostics.")

        action_frame = ttk.LabelFrame(right, text="Action Queue", padding=8)
        action_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        action_frame.columnconfigure(0, weight=1)
        action_frame.rowconfigure(0, weight=1)
        self.analysis_action_tree = ttk.Treeview(
            action_frame,
            columns=("severity", "category", "target", "next"),
            show="tree headings",
            selectmode="browse",
            height=6,
        )
        self.analysis_action_tree.heading("#0", text="Issue")
        self.analysis_action_tree.heading("severity", text="Severity")
        self.analysis_action_tree.heading("category", text="Category")
        self.analysis_action_tree.heading("target", text="Target")
        self.analysis_action_tree.heading("next", text="Suggested Next Step")
        self.analysis_action_tree.column("#0", width=260, anchor="w")
        self.analysis_action_tree.column("severity", width=70, anchor="center")
        self.analysis_action_tree.column("category", width=120, anchor="center")
        self.analysis_action_tree.column("target", width=140, anchor="w")
        self.analysis_action_tree.column("next", width=210, anchor="w")
        self.analysis_action_tree.grid(row=0, column=0, sticky="nsew")
        action_scroll = ttk.Scrollbar(action_frame, orient="vertical", command=self.analysis_action_tree.yview)
        action_scroll.grid(row=0, column=1, sticky="ns")
        self.analysis_action_tree.configure(yscrollcommand=action_scroll.set)
        self.analysis_action_tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_analysis_action_detail())
        self.analysis_action_tree.bind("<Double-1>", lambda _event: self._run_selected_analysis_action())

        ttk.Label(
            action_frame,
            textvariable=self.analysis_action_detail_var,
            wraplength=620,
            justify="left",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        action_buttons = ttk.Frame(action_frame)
        action_buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(action_buttons, text="Open Target", command=self._run_selected_analysis_action).grid(row=0, column=0)
        ttk.Button(action_buttons, text="Open Compare", command=self._run_selected_analysis_compare_action).grid(
            row=0,
            column=1,
            padx=(6, 0),
        )
        ttk.Button(action_buttons, text="Apply Suggested Preset", command=self._apply_selected_analysis_preset).grid(
            row=0,
            column=2,
            padx=(6, 0),
        )

        assets_frame = ttk.LabelFrame(self.analysis_tab, text="Detected Source Assets", padding=10)
        assets_frame.grid(row=3, column=0, sticky="nsew")
        assets_frame.columnconfigure(0, weight=1)
        assets_frame.rowconfigure(0, weight=1)

        columns = ("type", "animated", "sizes", "flags", "location")
        self.analysis_asset_tree = ttk.Treeview(
            assets_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self.analysis_asset_tree.heading("#0", text="Source")
        self.analysis_asset_tree.heading("type", text="Type")
        self.analysis_asset_tree.heading("animated", text="Animated")
        self.analysis_asset_tree.heading("sizes", text="Native Sizes")
        self.analysis_asset_tree.heading("flags", text="Warnings")
        self.analysis_asset_tree.heading("location", text="Folder")
        self.analysis_asset_tree.column("#0", width=280, anchor="w")
        self.analysis_asset_tree.column("type", width=80, anchor="center")
        self.analysis_asset_tree.column("animated", width=80, anchor="center")
        self.analysis_asset_tree.column("sizes", width=140, anchor="center")
        self.analysis_asset_tree.column("flags", width=330, anchor="w")
        self.analysis_asset_tree.column("location", width=380, anchor="w")
        self.analysis_asset_tree.grid(row=0, column=0, sticky="nsew")
        self.analysis_asset_tree.bind("<Double-1>", lambda _event: self._review_selected_analysis_asset())

        scroll_y = ttk.Scrollbar(assets_frame, orient="vertical", command=self.analysis_asset_tree.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        self.analysis_asset_tree.configure(yscrollcommand=scroll_y.set)

    def _build_review_tab(self) -> None:
        self.review_tab.columnconfigure(0, weight=1)
        self.review_tab.rowconfigure(0, weight=1)

        paned = ttk.Panedwindow(self.review_tab, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew")

        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned, padding=(8, 0, 0, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        paned.add(left, weight=3)
        paned.add(right, weight=5)

        ttk.Label(
            left,
            text="Stage 2: Review the guessed slot assignments visually. Paths stay available, but the primary signals are previews, animation badges, native sizes, and quality warnings.",
            wraplength=500,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(left, textvariable=self.review_queue_var, style="Heading.TLabel", wraplength=500, justify="left").grid(
            row=1,
            column=0,
            sticky="ew",
        )
        ttk.Label(left, textvariable=self.review_queue_hint_var, style="Muted.TLabel", wraplength=500, justify="left").grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(4, 8),
        )

        slot_frame = ttk.Frame(left)
        slot_frame.grid(row=3, column=0, sticky="nsew")
        slot_frame.columnconfigure(0, weight=1)
        slot_frame.rowconfigure(0, weight=1)

        slot_canvas = tk.Canvas(slot_frame, highlightthickness=0, bg=self.palette["root_bg"])
        slot_canvas.grid(row=0, column=0, sticky="nsew")
        slot_scroll = ttk.Scrollbar(slot_frame, orient="vertical", command=slot_canvas.yview)
        slot_scroll.grid(row=0, column=1, sticky="ns")
        slot_canvas.configure(yscrollcommand=slot_scroll.set)

        inner = ttk.Frame(slot_canvas)
        inner.columnconfigure(0, weight=1)
        self.slot_card_container = inner
        slot_window = slot_canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>", lambda _event: slot_canvas.configure(scrollregion=slot_canvas.bbox("all")))
        slot_canvas.bind("<Configure>", lambda event: slot_canvas.itemconfigure(slot_window, width=event.width))

        self.slot_cards: dict[str, SlotCard] = {}
        for row_index, slot in enumerate(SLOT_DEFS):
            card = SlotCard(inner, slot, self)
            card.grid(row=row_index, column=0, sticky="ew", pady=(0, 8))
            self.slot_cards[slot["key"]] = card

        detail = ttk.LabelFrame(right, text="Selected Slot Detail", padding=10)
        detail.grid(row=0, column=0, sticky="ew")
        detail.columnconfigure(0, weight=1)
        detail.columnconfigure(1, weight=1)
        self.selected_slot_title_var = tk.StringVar(value="Select a slot")
        self.selected_slot_meta_var = tk.StringVar(value="")
        self.selected_slot_path_var = tk.StringVar(value="")
        ttk.Label(detail, textvariable=self.selected_slot_title_var, font=("", 11, "bold"), style="Heading.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(detail, textvariable=self.selected_slot_meta_var, wraplength=800, justify="left").grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(4, 0),
        )
        ttk.Label(detail, textvariable=self.selected_slot_path_var, wraplength=800, justify="left", style="Muted.TLabel").grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(2, 8),
        )

        action_row = ttk.Frame(detail)
        action_row.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(action_row, text="Browse Source", command=lambda: self.browse_slot(self.selected_slot_key)).grid(row=0, column=0)
        ttk.Button(action_row, text="Clear Slot", command=lambda: self.clear_slot(self.selected_slot_key)).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(action_row, text="Use Selected Candidate", command=self.apply_selected_candidate).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(action_row, text="Open Compare", command=self.open_compare_view).grid(row=0, column=3, padx=(6, 0))

        preview_size_row = ttk.Frame(detail)
        preview_size_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(preview_size_row, text="Predicted preview size").grid(row=0, column=0, sticky="w")
        self.preview_nominal_size_combo = ttk.Combobox(
            preview_size_row,
            textvariable=self.preview_nominal_size_var,
            state="readonly",
            width=10,
        )
        self.preview_nominal_size_combo.grid(row=0, column=1, padx=(8, 0))

        warning_frame = ttk.LabelFrame(right, text="Slot Validation", padding=10)
        warning_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        warning_frame.columnconfigure(0, weight=1)
        self.slot_warning_text = tk.Text(warning_frame, height=6, wrap="word")
        self.slot_warning_text.grid(row=0, column=0, sticky="nsew")
        self._theme_text_widget(self.slot_warning_text)
        set_readonly_text(self.slot_warning_text, "Select a slot to inspect its warnings and quality forecast.")

        preview_row = ttk.Frame(right)
        preview_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        preview_row.columnconfigure(0, weight=1)
        preview_row.columnconfigure(1, weight=1)
        self.source_preview_panel = AnimationPreviewPanel(preview_row, "Source Animation Preview", PLAYER_PREVIEW_SIZE, self.palette)
        self.source_preview_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.output_preview_panel = AnimationPreviewPanel(preview_row, "Predicted Linux Output Preview", PLAYER_PREVIEW_SIZE, self.palette)
        self.output_preview_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.review_tool_notebook = ttk.Notebook(right)
        self.review_tool_notebook.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        self.candidate_browser_tab = ttk.Frame(self.review_tool_notebook, padding=8)
        self.compare_tab = ttk.Frame(self.review_tool_notebook, padding=8)
        self.review_tool_notebook.add(self.candidate_browser_tab, text="Candidates")
        self.review_tool_notebook.add(self.compare_tab, text="Compare")

        self.candidate_browser_tab.columnconfigure(0, weight=1)
        self.candidate_browser_tab.rowconfigure(0, weight=1)

        self.candidate_tree = ttk.Treeview(
            self.candidate_browser_tab,
            columns=("type", "sizes", "score", "reason"),
            show="tree headings",
            selectmode="browse",
            height=8,
        )
        self.candidate_tree.heading("#0", text="Candidate")
        self.candidate_tree.heading("type", text="Type")
        self.candidate_tree.heading("sizes", text="Native Sizes")
        self.candidate_tree.heading("score", text="Score")
        self.candidate_tree.heading("reason", text="Ranking Reason")
        self.candidate_tree.column("#0", width=220, anchor="w")
        self.candidate_tree.column("type", width=70, anchor="center")
        self.candidate_tree.column("sizes", width=110, anchor="center")
        self.candidate_tree.column("score", width=70, anchor="center")
        self.candidate_tree.column("reason", width=310, anchor="w")
        self.candidate_tree.grid(row=0, column=0, sticky="nsew")
        candidate_scroll = ttk.Scrollbar(self.candidate_browser_tab, orient="vertical", command=self.candidate_tree.yview)
        candidate_scroll.grid(row=0, column=1, sticky="ns")
        self.candidate_tree.configure(yscrollcommand=candidate_scroll.set)
        self.candidate_tree.bind("<<TreeviewSelect>>", lambda _event: self._schedule_candidate_detail_refresh())

        candidate_detail = ttk.Frame(self.candidate_browser_tab)
        candidate_detail.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        candidate_detail.columnconfigure(1, weight=1)
        candidate_detail.rowconfigure(0, weight=1)
        candidate_detail.rowconfigure(1, weight=1)
        self.candidate_preview_panel = AnimationPreviewPanel(candidate_detail, "Candidate Preview", CANDIDATE_PREVIEW_SIZE, self.palette)
        self.candidate_preview_panel.grid(row=0, column=0, rowspan=2, sticky="nsw")
        explain_frame = ttk.LabelFrame(candidate_detail, text="Why This Candidate Ranks Here", padding=8)
        explain_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        explain_frame.columnconfigure(0, weight=1)
        explain_frame.rowconfigure(0, weight=1)
        self.candidate_reason_text = tk.Text(explain_frame, height=8, wrap="word")
        self.candidate_reason_text.grid(row=0, column=0, sticky="nsew")
        self._theme_text_widget(self.candidate_reason_text)
        set_readonly_text(self.candidate_reason_text, "Select a candidate to inspect its ranking logic.")

        current_choice_frame = ttk.LabelFrame(candidate_detail, text="Why The Current Choice Was Made", padding=8)
        current_choice_frame.grid(row=1, column=1, sticky="nsew", padx=(10, 0), pady=(8, 0))
        current_choice_frame.columnconfigure(0, weight=1)
        current_choice_frame.rowconfigure(0, weight=1)
        self.current_choice_text = tk.Text(current_choice_frame, height=7, wrap="word")
        self.current_choice_text.grid(row=0, column=0, sticky="nsew")
        self._theme_text_widget(self.current_choice_text)
        set_readonly_text(self.current_choice_text, "Auto-fill or select a slot to explain the active choice.")

        self.compare_tab.columnconfigure(0, weight=1)
        self.compare_tab.rowconfigure(3, weight=1)

        compare_controls = ttk.LabelFrame(self.compare_tab, text="Compare Mode", padding=8)
        compare_controls.grid(row=0, column=0, sticky="ew")
        compare_controls.columnconfigure(3, weight=1)
        ttk.Label(compare_controls, text="Mode").grid(row=0, column=0, sticky="w")
        self.compare_mode_combo = ttk.Combobox(
            compare_controls,
            textvariable=self.compare_mode_var,
            values=COMPARE_MODE_CHOICES,
            state="readonly",
            width=28,
        )
        self.compare_mode_combo.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self.compare_mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_compare_view_refresh())
        ttk.Label(compare_controls, text="Compare preset").grid(row=0, column=2, sticky="e")
        self.compare_preset_combo = ttk.Combobox(
            compare_controls,
            textvariable=self.compare_preset_var,
            values=BUILD_PRESET_LABELS,
            state="readonly",
            width=18,
        )
        self.compare_preset_combo.grid(row=0, column=3, sticky="w", padx=(8, 0))
        ttk.Button(compare_controls, text="Replay Both", command=self._replay_compare_panels).grid(row=0, column=4, padx=(12, 0))

        ttk.Label(self.compare_tab, textvariable=self.compare_summary_var, wraplength=860, justify="left").grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(8, 0),
        )
        ttk.Label(self.compare_tab, textvariable=self.compare_hint_var, wraplength=860, justify="left", style="Muted.TLabel").grid(
            row=2,
            column=0,
            sticky="nw",
            pady=(4, 8),
        )

        compare_panels = ttk.Frame(self.compare_tab)
        compare_panels.grid(row=3, column=0, sticky="nsew")
        compare_panels.columnconfigure(0, weight=1)
        compare_panels.columnconfigure(1, weight=1)
        self.compare_left_panel = AnimationPreviewPanel(compare_panels, "Left Compare", PLAYER_PREVIEW_SIZE, self.palette)
        self.compare_left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.compare_right_panel = AnimationPreviewPanel(compare_panels, "Right Compare", PLAYER_PREVIEW_SIZE, self.palette)
        self.compare_right_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

    def _build_build_tab(self) -> None:
        self.build_tab.columnconfigure(0, weight=1)
        self.build_tab.columnconfigure(1, weight=1)
        self.build_tab.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(self.build_tab, text="Stage 3: Build Settings And Export", padding=10)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)
        top.columnconfigure(4, weight=1)

        ttk.Label(top, text="Preset to apply").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.preset_combo = ttk.Combobox(
            top,
            textvariable=self.build_preset_var,
            values=BUILD_PRESET_LABELS,
            state="readonly",
            width=18,
        )
        self.preset_combo.grid(row=0, column=1, sticky="w", pady=3)
        apply_preset_button = ttk.Button(top, text="Apply Preset", command=self.apply_selected_preset)
        apply_preset_button.grid(row=0, column=2, padx=(8, 0), pady=3)
        self._attach_tooltip(
            apply_preset_button,
            "Quickly switches the build sizes and scaling style to one of the preset options.",
        )
        ttk.Label(top, textvariable=self.preset_description_var, wraplength=760, justify="left", style="Muted.TLabel").grid(
            row=0,
            column=3,
            columnspan=2,
            sticky="w",
            padx=(12, 0),
            pady=3,
        )

        ttk.Label(top, textvariable=self.profile_state_var, style="Heading.TLabel", wraplength=1120, justify="left").grid(
            row=1,
            column=0,
            columnspan=6,
            sticky="w",
            pady=(6, 0),
        )
        ttk.Label(top, textvariable=self.profile_match_var, style="Muted.TLabel", wraplength=1120, justify="left").grid(
            row=2,
            column=0,
            columnspan=6,
            sticky="w",
            pady=(2, 4),
        )

        ttk.Label(top, text="Theme name").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.theme_name_var).grid(row=3, column=1, sticky="ew", pady=3)

        ttk.Label(top, text="Output sizes").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.target_sizes_var).grid(row=4, column=1, sticky="ew", pady=3)
        ttk.Label(top, text="Scale filter").grid(row=4, column=2, sticky="e", padx=(10, 8), pady=3)
        ttk.Combobox(top, textvariable=self.scale_filter_var, values=SCALE_FILTER_CHOICES, state="readonly", width=12).grid(
            row=4,
            column=3,
            sticky="w",
            pady=3,
        )
        ttk.Label(top, text="Predicted preview size").grid(row=4, column=4, sticky="e", padx=(10, 8), pady=3)
        self.build_preview_size_combo = ttk.Combobox(
            top,
            textvariable=self.preview_nominal_size_var,
            state="readonly",
            width=10,
        )
        self.build_preview_size_combo.grid(row=4, column=5, sticky="w", pady=3)

        ttk.Label(top, text="Output root").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.work_root_var).grid(row=5, column=1, columnspan=4, sticky="ew", pady=3)
        ttk.Button(top, text="Browse", command=self.choose_work_root).grid(row=5, column=5, padx=(8, 0), pady=3)

        quality_frame = ttk.LabelFrame(self.build_tab, text="Quality Forecast And Validation", padding=10)
        quality_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(10, 0))
        quality_frame.columnconfigure(0, weight=1)
        quality_frame.rowconfigure(3, weight=1)
        ttk.Label(quality_frame, textvariable=self.readiness_var, font=("", 11, "bold"), style="Heading.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(quality_frame, textvariable=self.readiness_detail_var, wraplength=720, justify="left", style="Muted.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(4, 0),
        )
        quality_actions = ttk.Frame(quality_frame)
        quality_actions.grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Button(quality_actions, text="Review Weak Slot", command=self.review_most_at_risk_slot).grid(row=0, column=0)
        ttk.Button(quality_actions, text="Open Compare", command=self.open_compare_view).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(quality_actions, text=f"Use {SAFE_PRESET_LABEL}", command=self.apply_safe_preset).grid(row=0, column=2, padx=(6, 0))
        self.build_warning_text = tk.Text(quality_frame, wrap="word")
        self.build_warning_text.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        self._theme_text_widget(self.build_warning_text)
        set_readonly_text(self.build_warning_text, "Warnings and build guidance will appear here.")

        export_frame = ttk.LabelFrame(self.build_tab, text="Mapping, Export, And Final Output", padding=10)
        export_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(10, 0))
        export_frame.columnconfigure(0, weight=1)
        export_frame.rowconfigure(1, weight=1)

        button_row = ttk.Frame(export_frame)
        button_row.grid(row=0, column=0, sticky="ew")
        load_json_button = ttk.Button(button_row, text="Load JSON", command=self.load_json)
        load_json_button.grid(row=0, column=0)
        self._attach_tooltip(load_json_button, "Loads a saved mapping so you can pick up where you left off.")
        save_json_button = ttk.Button(button_row, text="Save JSON", command=self.save_json)
        save_json_button.grid(row=0, column=1, padx=(6, 0))
        self._attach_tooltip(
            save_json_button,
            "Saves your current mapping so you can reuse it later or build from it again.",
        )
        save_markdown_button = ttk.Button(button_row, text="Save Markdown", command=self.save_markdown)
        save_markdown_button.grid(row=0, column=2, padx=(6, 0))
        self._attach_tooltip(
            save_markdown_button,
            "Exports a simple readable report of the current slot mapping and Linux cursor roles.",
        )
        self.build_button = ttk.Button(button_row, text="Build + Package", command=self.build_and_package)
        self.build_button.grid(row=0, column=3, padx=(12, 0))
        self._attach_tooltip(
            self.build_button,
            "Builds the cursor theme and packs it into a tarball you can install or share.",
        )

        self.build_summary_text = tk.Text(export_frame, wrap="word")
        self.build_summary_text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self._theme_text_widget(self.build_summary_text)
        ttk.Label(export_frame, textvariable=self.last_output_var, wraplength=720, justify="left", style="Muted.TLabel").grid(
            row=2,
            column=0,
            sticky="w",
            pady=(10, 0),
        )

        self._update_preview_size_choices()

    def _update_preset_description(self) -> None:
        preset_value = self.build_preset_var.get().strip()
        try:
            self.preset_description_var.set(describe_build_preset(preset_value))
        except KeyError:
            pass

    def on_close(self) -> None:
        self.refresh_coalescer.close()
        self.task_runner.close()
        self.root.destroy()

    def _set_pack_analysis(self, analysis: dict | None) -> None:
        self.pack_analysis = analysis
        self.pack_asset_lookup = {
            asset["path"]: asset
            for asset in (analysis or {}).get("asset_summaries", [])
            if asset.get("path")
        }

    def _ambiguous_candidates_for_slot(self, slot_key: str) -> list[dict]:
        if self.pack_analysis is None:
            return []
        return list(self.pack_analysis.get("ambiguous_candidates", {}).get(slot_key, []))

    def _asset_summary_for_path(self, path: str | Path) -> dict | None:
        normalized = str(normalize_path(Path(path)))
        return self.pack_asset_lookup.get(normalized)

    def _slot_candidates(self, slot_key: str) -> list[dict]:
        if self.pack_analysis is None:
            return []
        return list(self.pack_analysis.get("slot_candidates", {}).get(slot_key, []))

    def _candidate_for_slot_path(self, slot_key: str, path: str | Path) -> dict | None:
        normalized = str(normalize_path(Path(path)))
        for candidate in self._slot_candidates(slot_key):
            if candidate.get("path") == normalized:
                return candidate
        return None

    def _selection_origin_label(self, origin: str) -> str:
        return {
            "inf": "install.inf",
            "heuristic": "filename heuristic",
            "fallback": "fallback reuse",
            "override": "auto-fill override",
            "manual-candidate": "manual candidate choice",
            "manual-browse": "manual browse choice",
            "loaded": "loaded mapping",
            "unknown": "current assignment",
        }.get(origin, origin.replace("-", " "))

    def _infer_selection_context(self, slot_key: str, path: str) -> dict:
        normalized = str(normalize_path(Path(path)))
        analysis = self.pack_analysis or {}
        inf_mapping = analysis.get("install_inf_mapping", {})
        if inf_mapping.get(slot_key) == normalized:
            return {
                "origin": "inf",
                "path": normalized,
                "reason": "Auto-fill kept the path explicitly referenced by install.inf.",
            }
        candidate = self._candidate_for_slot_path(slot_key, normalized)
        if candidate is not None:
            return {
                "origin": "heuristic",
                "path": normalized,
                "reason": candidate.get("reason", "Chosen from the ranked candidate browser."),
                "rank": candidate.get("rank"),
                "score": candidate.get("score"),
            }
        return {
            "origin": "loaded",
            "path": normalized,
            "reason": "This assignment came from a loaded mapping or a manual source path.",
        }

    def _selection_context_for_slot(self, slot_key: str, path: str | None = None) -> dict | None:
        current_path = path or self.slot_paths.get(slot_key, "")
        if not current_path:
            return None
        context = self.slot_selection_context.get(slot_key)
        if context and context.get("path") == current_path:
            return context
        inferred = self._infer_selection_context(slot_key, current_path)
        self.slot_selection_context[slot_key] = inferred
        return inferred

    def _set_selection_context(self, slot_key: str, context: dict | None) -> None:
        if context is None:
            self.slot_selection_context.pop(slot_key, None)
            return
        self.slot_selection_context[slot_key] = context

    def _selection_context_payload(self) -> dict:
        payload = {}
        for slot_key, context in self.slot_selection_context.items():
            path = self.slot_paths.get(slot_key, "").strip()
            if not path:
                continue
            if context.get("path") != path:
                continue
            payload[slot_key] = dict(context)
        return payload

    def _apply_prepare_selection_context(self, summary: dict) -> None:
        diagnostics = summary.get("diagnostics", {})
        context: dict[str, dict] = {}
        for slot_key, path in diagnostics.get("chosen_by_inf", {}).items():
            context[slot_key] = {
                "origin": "inf",
                "path": path,
                "reason": "Auto-fill used install.inf for this slot.",
            }
        for slot_key, item in diagnostics.get("chosen_by_heuristic", {}).items():
            context[slot_key] = {
                "origin": "heuristic",
                "path": item["path"],
                "reason": item.get("reason", "Auto-fill used the top-ranked filename candidate."),
                "rank": 1,
                "score": item.get("score"),
            }
        for item in diagnostics.get("fallbacks", []):
            context[item["target"]] = {
                "origin": "fallback",
                "path": item["path"],
                "reason": f"Auto-fill reused {SLOT_BY_KEY[item['source']]['label']} because this slot had no stronger standalone match.",
                "source_slot": item["source"],
            }
        for item in diagnostics.get("overrides", []):
            context[item["target"]] = {
                "origin": "override",
                "path": item["to"],
                "reason": item.get("reason", "Auto-fill applied a post-selection override."),
                "from_path": item.get("from"),
            }
        self.slot_selection_context = context
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            if path and slot["key"] not in self.slot_selection_context:
                self.slot_selection_context[slot["key"]] = self._infer_selection_context(slot["key"], path)

    def _candidate_reason_for_tree(self, slot_key: str, candidate: dict) -> str:
        if candidate.get("path") == (self.pack_analysis or {}).get("install_inf_mapping", {}).get(slot_key):
            return "INF-backed candidate"
        reason = summarize_match_details(candidate.get("match_details", {}))
        if candidate.get("low_priority_hits"):
            reason += " | generated-folder penalty"
        elif int(candidate.get("depth", 0)) > 1:
            reason += " | deeper path tie-break"
        return reason

    def _candidate_explanation_text(self, slot_key: str, candidate: dict, summary: dict, quality: dict) -> str:
        leader = self._slot_candidates(slot_key)[0] if self._slot_candidates(slot_key) else None
        lines = [
            f"Candidate: {Path(candidate['path']).name}",
            f"Rank: #{candidate.get('rank', '--')} | score {candidate.get('score', '--')}",
            f"Match basis: {summarize_match_details(candidate.get('match_details', {}))}",
            f"Ranking outcome: {candidate_rank_gap_reason(candidate, leader)}",
            f"Source location: {summary.get('relative_path', compact_path(candidate['path'], max_len=96))}",
            f"Quality forecast: {quality['label']} ({quality.get('confidence', 'low')} confidence) | {quality['reason']}",
        ]
        if candidate.get("path") == (self.pack_analysis or {}).get("install_inf_mapping", {}).get(slot_key):
            lines.append("This path is also backed by install.inf for the slot.")
        if candidate.get("low_priority_hits"):
            lines.append("Folder priority penalty: candidate lives under generated/temp-style folders.")
        if int(candidate.get("depth", 0)) > 0:
            lines.append(f"Depth tie-break: nested {candidate['depth']} folder level(s) below the pack root.")
        if candidate.get("warnings"):
            lines.append("Candidate warnings: " + "; ".join(candidate["warnings"][:3]))
        ambiguous_paths = {item["path"] for item in self._ambiguous_candidates_for_slot(slot_key)}
        if candidate.get("path") in ambiguous_paths:
            lines.append("Ambiguity: this slot is effectively near-tied and should be judged visually.")
        return "\n".join(f"- {line}" for line in lines)

    def _current_choice_text(self, slot_key: str, current_path: str, quality: dict, selected_candidate: dict | None) -> str:
        context = self._selection_context_for_slot(slot_key, current_path)
        lines = []
        if context is None:
            return "No current slot assignment."
        lines.append(f"Origin: {self._selection_origin_label(context.get('origin', 'unknown'))}")
        lines.append(f"Why it was chosen: {context.get('reason', 'No extra provenance recorded.')}")
        current_candidate = self._candidate_for_slot_path(slot_key, current_path)
        leader = self._slot_candidates(slot_key)[0] if self._slot_candidates(slot_key) else None
        if current_candidate is not None:
            lines.append(
                f"Current ranking: #{current_candidate.get('rank', '--')} | score {current_candidate.get('score', '--')} | "
                f"{summarize_match_details(current_candidate.get('match_details', {}))}"
            )
            if leader is not None and leader.get("path") != current_path:
                lines.append(
                    f"Why it is not the leader: {candidate_rank_gap_reason(current_candidate, leader)}."
                )
        elif context.get("origin") == "inf":
            lines.append("This choice can stay selected even when another filename scores higher, because install.inf pinned it.")
        elif context.get("origin") == "fallback":
            source_slot = context.get("source_slot")
            if source_slot in SLOT_BY_KEY:
                lines.append(f"It currently reuses {SLOT_BY_KEY[source_slot]['label']} art as a fallback.")
        if selected_candidate is not None and selected_candidate.get("path") != current_path:
            lines.append(
                f"Selected alternate: {Path(selected_candidate['path']).name} | "
                f"{candidate_rank_gap_reason(selected_candidate, leader)}"
            )
        lines.append(
            f"Decision guidance: {quality['decision']} | {quality['label']} ({quality.get('confidence', 'low')} confidence)"
        )
        return "\n".join(f"- {line}" for line in lines)

    def _slot_guidance_text(self, slot_key: str, summary: dict, quality: dict) -> str:
        context = self._selection_context_for_slot(slot_key, summary.get("path"))
        lines = [
            "Forecast:",
            f"- {quality['label']} ({quality.get('confidence', 'low')} confidence): {quality['reason']}",
            "",
            "Choice provenance:",
            f"- {self._selection_origin_label((context or {}).get('origin', 'unknown'))}: {(context or {}).get('reason', 'No provenance recorded.')}",
            "",
            "Warnings:",
        ]
        warning_lines = quality["warnings"] or ["No immediate warnings."]
        for warning in warning_lines:
            lines.append(f"- {warning}")
        if summary.get("hotspot_summary"):
            lines.append(f"- Hotspot summary: {summary['hotspot_summary']}")
        lines.extend(["", "Recommended actions:"])
        for action in quality["actions"]:
            lines.append(f"- {action}")
        return "\n".join(lines)

    def _build_analysis_action_items(self) -> list[dict]:
        analysis = self.pack_analysis
        if analysis is None:
            return []

        items: list[dict] = []
        if analysis.get("install_inf") is None:
            items.append(
                {
                    "title": "No install.inf guidance",
                    "severity": "warn",
                    "category": "Pack",
                    "target": "Candidate review",
                    "next": "Review ranked slots",
                    "detail": "Auto-fill must lean entirely on filename heuristics, so slot review matters more than usual.",
                    "action": {"kind": "open_review"},
                }
            )

        hidpi = analysis.get("hidpi_potential", {})
        if hidpi.get("rating") in {"weak", "limited"}:
            items.append(
                {
                    "title": "Weak HiDPI coverage",
                    "severity": "warn",
                    "category": "Build",
                    "target": "Preset guidance",
                    "next": f"Apply {SAFE_PRESET_LABEL}",
                    "detail": (
                        f"Only {hidpi.get('supports_96_count', 0)} asset(s) reach 96px native detail. "
                        "Large presets will upscale heavily unless you redraw weak slots."
                    ),
                    "action": {"kind": "open_build"},
                    "compare_action": {"kind": "compare_preset", "preset": SAFE_PRESET_LABEL},
                    "suggested_preset": SAFE_PRESET_LABEL,
                }
            )

        for slot_key, candidates in sorted(analysis.get("ambiguous_candidates", {}).items()):
            alternate_path = candidates[1]["path"] if len(candidates) > 1 else candidates[0]["path"]
            items.append(
                {
                    "title": f"{SLOT_BY_KEY[slot_key]['label']} is ambiguous",
                    "severity": "warn",
                    "category": "Slot",
                    "target": SLOT_BY_KEY[slot_key]["label"],
                    "next": "Compare top candidates",
                    "detail": (
                        "Top filename candidates are near-tied here. Review the slot and compare the strongest options visually."
                    ),
                    "action": {"kind": "review_slot", "slot_key": slot_key, "candidate_path": candidates[0]["path"]},
                    "compare_action": {
                        "kind": "compare_slot",
                        "slot_key": slot_key,
                        "candidate_path": alternate_path,
                    },
                }
            )

        for slot in SLOT_DEFS:
            candidates = self._slot_candidates(slot["key"])
            if not candidates:
                continue
            top_candidate = candidates[0]
            if int(top_candidate.get("low_priority_hits", 0)) > 0:
                items.append(
                    {
                        "title": f"{slot['label']} leans on generated folders",
                        "severity": "warn",
                        "category": "Candidate",
                        "target": slot["label"],
                        "next": "Review candidate origin",
                        "detail": (
                            f"The top-ranked candidate for {slot['label']} comes from a tmp/build/cache-style path. "
                            "Compare it against a cleaner source before locking the slot."
                        ),
                        "action": {
                            "kind": "review_slot",
                            "slot_key": slot["key"],
                            "candidate_path": top_candidate["path"],
                        },
                        "compare_action": {
                            "kind": "compare_slot",
                            "slot_key": slot["key"],
                            "candidate_path": top_candidate["path"],
                        },
                    }
                )

        error_assets = [asset for asset in analysis.get("asset_summaries", []) if asset.get("error")]
        if error_assets:
            items.append(
                {
                    "title": "Some assets failed inspection",
                    "severity": "warn",
                    "category": "Assets",
                    "target": f"{len(error_assets)} file(s)",
                    "next": "Open asset list",
                    "detail": "At least one source file could not be inspected cleanly. Double-click it in the asset list and replace it if needed.",
                    "action": {"kind": "open_analysis"},
                }
            )

        return items

    def _populate_analysis_action_tree(self) -> None:
        for item in self.analysis_action_tree.get_children():
            self.analysis_action_tree.delete(item)
        self.analysis_action_items = {}
        for index, item in enumerate(self._build_analysis_action_items(), start=1):
            iid = f"analysis-action-{index}"
            self.analysis_action_items[iid] = item
            self.analysis_action_tree.insert(
                "",
                "end",
                iid=iid,
                text=item["title"],
                values=(item["severity"], item["category"], item["target"], item["next"]),
            )
        children = self.analysis_action_tree.get_children()
        if children:
            self.analysis_action_tree.selection_set(children[0])
        else:
            self.analysis_action_detail_var.set("No action items. The pack analysis looks straightforward so far.")

    def _selected_analysis_action_item(self) -> dict | None:
        selection = self.analysis_action_tree.selection()
        if not selection:
            return None
        return self.analysis_action_items.get(selection[0])

    def _refresh_analysis_action_detail(self) -> None:
        item = self._selected_analysis_action_item()
        if item is None:
            self.analysis_action_detail_var.set("Select an action item to see why it matters.")
            return
        self.analysis_action_detail_var.set(item["detail"])

    def _select_candidate_in_tree(self, candidate_path: str | None) -> None:
        if not candidate_path:
            return
        if candidate_path in self.candidate_tree.get_children():
            self.candidate_tree.selection_set(candidate_path)
            self.candidate_tree.focus(candidate_path)
            self.candidate_tree.see(candidate_path)
            self._refresh_candidate_detail()

    def _perform_analysis_action(self, action: dict | None) -> None:
        if action is None:
            return
        kind = action.get("kind")
        if kind == "open_analysis":
            self.notebook.select(self.analysis_tab)
            return
        if kind == "open_build":
            self.notebook.select(self.build_tab)
            return
        if kind == "open_review":
            self.notebook.select(self.review_tab)
            return
        if kind == "review_slot":
            slot_key = action["slot_key"]
            self.focus_slot_candidates(slot_key)
            self.review_tool_notebook.select(self.candidate_browser_tab)
            self._select_candidate_in_tree(action.get("candidate_path"))
            return
        if kind == "compare_slot":
            self.open_compare_view(
                mode=COMPARE_MODE_CURRENT_VS_CANDIDATE,
                slot_key=action["slot_key"],
                candidate_path=action.get("candidate_path"),
            )
            return
        if kind == "compare_preset":
            self.open_compare_view(
                mode=COMPARE_MODE_PRESET,
                preset_label=action.get("preset", SAFE_PRESET_LABEL),
            )

    def _run_selected_analysis_action(self) -> None:
        item = self._selected_analysis_action_item()
        if item is None:
            return
        self._perform_analysis_action(item.get("action"))

    def _run_selected_analysis_compare_action(self) -> None:
        item = self._selected_analysis_action_item()
        if item is None:
            return
        action = item.get("compare_action")
        if action is None:
            self.analysis_action_detail_var.set(item["detail"] + " No compare action is available for this item yet.")
            return
        self._perform_analysis_action(action)

    def _apply_selected_analysis_preset(self) -> None:
        item = self._selected_analysis_action_item()
        if item is None:
            return
        preset_label = item.get("suggested_preset")
        if not preset_label:
            self.analysis_action_detail_var.set(item["detail"] + " No preset change is suggested for this item.")
            return
        self.build_preset_var.set(preset_label)
        self.apply_selected_preset()
        self.notebook.select(self.build_tab)

    def _review_selected_analysis_asset(self) -> None:
        selection = self.analysis_asset_tree.selection()
        if not selection:
            return
        asset_path = selection[0]
        for slot in SLOT_DEFS:
            candidate = self._candidate_for_slot_path(slot["key"], asset_path)
            if candidate is not None:
                self.focus_slot_candidates(slot["key"])
                self.review_tool_notebook.select(self.candidate_browser_tab)
                self._select_candidate_in_tree(asset_path)
                return
        for slot in SLOT_DEFS:
            if self.slot_paths.get(slot["key"], "").strip() == asset_path:
                self.focus_slot_candidates(slot["key"])
                return
        self.set_status(f"No slot context found yet for {Path(asset_path).name}")

    def _render_preview_payload_into_panel(self, panel: AnimationPreviewPanel, payload: dict, box_size: int) -> None:
        self._apply_preview_panel_payload(panel, payload)

    def _prepare_custom_output_preview_payload(self, source_path: Path, sizes: list[int], scale_filter: str) -> dict:
        preview = load_cached_output_preview(
            source_path,
            self.current_preview_root(),
            sizes,
            scale_filter,
            self.current_preview_nominal_size(),
            self.source_metadata_cache,
            self.output_preview_cache,
        )
        frames = preview["frames"]
        if not frames:
            return {"reason": "No predicted frames available", "preview": None}
        total_ms = sum(int(frame.get("delay_ms", 50)) for frame in frames)
        first = frames[0]
        frame_info = (
            f"Nominal size {preview['preview_nominal_size']}px | emitted PNG {first['width']}x{first['height']} | "
            f"filter {preview['scale_filter']}"
        )
        return {
            "reason": None,
            "preview": build_animation_preview_payload(
                frames,
                self.current_preview_root(),
                PLAYER_PREVIEW_SIZE,
                summary=f"{len(frames)} frame(s) | {format_duration_ms(total_ms)} total | built path preview",
                frame_info=frame_info,
            ),
        }

    def _default_compare_candidate_path(self) -> str | None:
        current_path = self._selected_slot_path()
        candidates = self._slot_candidates(self.selected_slot_key)
        if not candidates:
            return None
        if not current_path:
            top_path = candidates[0]["path"]
            if self.selected_candidate_path and self.selected_candidate_path != top_path:
                return self.selected_candidate_path
            if len(candidates) > 1:
                return candidates[1]["path"]
            return top_path
        if self.selected_candidate_path and self.selected_candidate_path != current_path:
            return self.selected_candidate_path
        for candidate in candidates:
            if candidate.get("path") != current_path:
                return candidate["path"]
        return None

    def _replay_compare_panels(self) -> None:
        self.compare_left_panel.replay()
        self.compare_right_panel.replay()

    def open_compare_view(
        self,
        *,
        mode: str | None = None,
        slot_key: str | None = None,
        candidate_path: str | None = None,
        preset_label: str | None = None,
    ) -> None:
        if slot_key is not None and slot_key != self.selected_slot_key:
            self.select_slot(slot_key)
        if candidate_path:
            self._select_candidate_in_tree(candidate_path)
        if preset_label:
            self.compare_preset_var.set(preset_label)
        if mode:
            self.compare_mode_var.set(mode)
        elif self.compare_mode_var.get() == COMPARE_MODE_CURRENT_VS_CANDIDATE and not self._default_compare_candidate_path():
            self.compare_mode_var.set(COMPARE_MODE_SOURCE_VS_OUTPUT)
        self.notebook.select(self.review_tab)
        self.review_tool_notebook.select(self.compare_tab)
        self._refresh_compare_view()

    def _refresh_compare_view(self) -> None:
        self.refresh_coalescer.cancel("compare-view-refresh")
        if not hasattr(self, "compare_left_panel"):
            return
        mode = self.compare_mode_var.get().strip() or COMPARE_MODE_CURRENT_VS_CANDIDATE
        if mode == COMPARE_MODE_PRESET:
            self.compare_preset_combo.configure(state="readonly")
        else:
            self.compare_preset_combo.configure(state="disabled")

        slot_label = SLOT_BY_KEY[self.selected_slot_key]["label"]
        weak_hidpi = ((self.pack_analysis or {}).get("hidpi_potential", {}).get("rating") in {"weak", "limited"})
        is_ambiguous = bool(self._ambiguous_candidates_for_slot(self.selected_slot_key))
        try:
            if mode == COMPARE_MODE_CURRENT_VS_CANDIDATE:
                current_path = self._selected_slot_path()
                left_title = "Current Selection"
                if not current_path:
                    candidates = self._slot_candidates(self.selected_slot_key)
                    if not candidates:
                        self.compare_summary_var.set("Select a slot first.")
                        self.compare_hint_var.set("Compare mode uses the currently selected slot as its baseline.")
                        self.compare_left_panel.clear("No slot selected")
                        self.compare_right_panel.clear("No slot selected")
                        return
                    current_path = candidates[0]["path"]
                    left_title = "Top Candidate"
                source_path = Path(current_path)
                alternate_path = self._default_compare_candidate_path()
                self.compare_left_panel.set_title(left_title)
                self.compare_right_panel.set_title("Alternate Candidate")
                left_payload = prepare_source_preview_payload(
                    source_path,
                    self.current_preview_root(),
                    self.current_preview_nominal_size(),
                    self.source_metadata_cache,
                )
                self._render_preview_payload_into_panel(self.compare_left_panel, left_payload, PLAYER_PREVIEW_SIZE)
                if not alternate_path:
                    self.compare_right_panel.clear("No alternate candidate available")
                    self.compare_summary_var.set(f"Compare current vs candidate for {slot_label}.")
                    self.compare_hint_var.set("This slot currently has no alternate ranked candidate to compare against.")
                    return
                alternate_source = Path(alternate_path)
                right_payload = prepare_source_preview_payload(
                    alternate_source,
                    self.current_preview_root(),
                    self.current_preview_nominal_size(),
                    self.source_metadata_cache,
                )
                self._render_preview_payload_into_panel(self.compare_right_panel, right_payload, PLAYER_PREVIEW_SIZE)
                current_quality = self._slot_quality(self.selected_slot_key, current_path) or {}
                alternate_quality = self._slot_quality(self.selected_slot_key, alternate_path) or {}
                alternate_candidate = self._candidate_for_slot_path(self.selected_slot_key, alternate_path) or {}
                summary, hint = build_compare_guidance(
                    COMPARE_MODE_CURRENT_VS_CANDIDATE,
                    slot_label=slot_label,
                    current_profile_label=self.current_build_profile.compare_label,
                    current_quality=current_quality,
                    selection_context=self._selection_context_for_slot(self.selected_slot_key, current_path),
                    weak_hidpi=weak_hidpi,
                    is_ambiguous=is_ambiguous,
                    alternate_path=Path(alternate_path).name,
                    alternate_rank=alternate_candidate.get("rank"),
                    alternate_quality=alternate_quality,
                )
                self.compare_summary_var.set(summary)
                self.compare_hint_var.set(
                    f"{hint} Current: {Path(current_path).name} [{current_quality.get('label', '--')}]. "
                    f"Alternate: {Path(alternate_path).name} [rank #{alternate_candidate.get('rank', '--')}, "
                    f"{alternate_quality.get('label', '--')}]."
                )
                return

            current_path = self._selected_slot_path()
            if not current_path:
                self.compare_summary_var.set("Select a slot first.")
                self.compare_hint_var.set("Compare mode uses the currently selected slot as its baseline.")
                self.compare_left_panel.clear("No slot selected")
                self.compare_right_panel.clear("No slot selected")
                return
            source_path = Path(current_path)

            if mode == COMPARE_MODE_SOURCE_VS_OUTPUT:
                self.compare_left_panel.set_title("Source Preview")
                self.compare_right_panel.set_title("Predicted Linux Output")
                left_payload = prepare_source_preview_payload(
                    source_path,
                    self.current_preview_root(),
                    self.current_preview_nominal_size(),
                    self.source_metadata_cache,
                )
                right_payload = prepare_output_preview_payload(
                    source_path,
                    self.current_preview_root(),
                    self.current_preview_nominal_size(),
                    self.try_target_sizes()[0],
                    self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER,
                    self.source_metadata_cache,
                    self.output_preview_cache,
                )
                self._render_preview_payload_into_panel(self.compare_left_panel, left_payload, PLAYER_PREVIEW_SIZE)
                self._render_preview_payload_into_panel(self.compare_right_panel, right_payload, PLAYER_PREVIEW_SIZE)
                current_quality = self._slot_quality(self.selected_slot_key, current_path)
                summary, hint = build_compare_guidance(
                    COMPARE_MODE_SOURCE_VS_OUTPUT,
                    slot_label=slot_label,
                    current_profile_label=self.current_build_profile.compare_label,
                    current_quality=current_quality,
                    selection_context=self._selection_context_for_slot(self.selected_slot_key, current_path),
                    weak_hidpi=weak_hidpi,
                    is_ambiguous=is_ambiguous,
                )
                self.compare_summary_var.set(summary)
                self.compare_hint_var.set(hint)
                return

            compare_preset = resolve_build_preset(self.compare_preset_var.get().strip() or SAFE_PRESET_LABEL)
            current_sizes = self.try_target_sizes()[0]
            current_filter = self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER
            self.compare_left_panel.set_title(f"Current Build ({self.current_build_profile.compare_label})")
            self.compare_right_panel.set_title(f"Compare Preset ({compare_preset['label']})")
            left_payload = self._prepare_custom_output_preview_payload(source_path, current_sizes, current_filter)
            right_payload = self._prepare_custom_output_preview_payload(
                source_path,
                compare_preset["target_sizes"],
                compare_preset["scale_filter"],
            )
            self._render_preview_payload_into_panel(self.compare_left_panel, left_payload, PLAYER_PREVIEW_SIZE)
            self._render_preview_payload_into_panel(self.compare_right_panel, right_payload, PLAYER_PREVIEW_SIZE)
            summary = self.ensure_summary(source_path)
            current_quality = evaluate_quality_forecast(
                self.selected_slot_key,
                summary,
                current_sizes,
                self.pack_analysis,
                selection_context=self._selection_context_for_slot(self.selected_slot_key, current_path),
            )
            preset_quality = evaluate_quality_forecast(
                self.selected_slot_key,
                summary,
                compare_preset["target_sizes"],
                self.pack_analysis,
                selection_context=self._selection_context_for_slot(self.selected_slot_key, current_path),
            )
            summary, hint = build_compare_guidance(
                COMPARE_MODE_PRESET,
                slot_label=slot_label,
                current_profile_label=self.current_build_profile.compare_label,
                current_quality=current_quality,
                selection_context=self._selection_context_for_slot(self.selected_slot_key, current_path),
                weak_hidpi=weak_hidpi,
                is_ambiguous=is_ambiguous,
                compare_preset_label=compare_preset["label"],
                compare_preset_quality=preset_quality,
            )
            self.compare_summary_var.set(summary)
            self.compare_hint_var.set(hint)
        except Exception as exc:  # noqa: BLE001
            self.compare_summary_var.set("Unable to build the requested compare preview.")
            self.compare_hint_var.set(str(exc))
            self.compare_left_panel.clear(str(exc))
            self.compare_right_panel.clear(str(exc))

    def review_most_at_risk_slot(self) -> None:
        ranked_slots = []
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            if not path:
                continue
            quality = self._slot_quality(slot["key"], path)
            if quality is None:
                continue
            ranked_slots.append((quality_to_score(quality["label"]), slot["key"], quality))
        if not ranked_slots:
            self.set_status("No assigned slots to review yet.")
            return
        ranked_slots.sort(key=lambda item: (item[0], item[1]))
        weakest_slot = ranked_slots[0][1]
        self.focus_slot_candidates(weakest_slot)
        self.open_compare_view()

    def apply_safe_preset(self) -> None:
        self.build_preset_var.set(SAFE_PRESET_LABEL)
        self.apply_selected_preset()

    def _update_busy_buttons(self) -> None:
        analysis_state = "disabled" if (self.analysis_busy or self.auto_prepare_busy) else "normal"
        build_state = "disabled" if self.build_busy else "normal"
        for button_name in ("analyze_button", "auto_fill_button"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.configure(state=analysis_state)
        if getattr(self, "build_button", None) is not None:
            self.build_button.configure(state=build_state)

    def _set_analysis_busy(self, is_busy: bool, status_message: str | None = None) -> None:
        self.analysis_busy = is_busy
        self._update_busy_buttons()
        if status_message is not None:
            self.set_status(status_message)

    def _set_auto_prepare_busy(self, is_busy: bool, status_message: str | None = None) -> None:
        self.auto_prepare_busy = is_busy
        self._update_busy_buttons()
        if status_message is not None:
            self.set_status(status_message)

    def _set_build_busy(self, is_busy: bool, status_message: str | None = None) -> None:
        self.build_busy = is_busy
        self._update_busy_buttons()
        if status_message is not None:
            self.set_status(status_message)

    def set_status(self, message: str) -> None:
        self.status_var.set(message)
        self.root.update_idletasks()

    def current_preview_root(self) -> Path:
        work_root = normalize_path(Path(self.work_root_var.get().strip() or DEFAULT_WORK_ROOT))
        return work_root / DEFAULT_PREVIEW_ROOT_NAME

    def preview_photo(self, png_path: Path, box_size: int) -> tk.PhotoImage:
        preview_root = self.current_preview_root()
        preview_png = render_preview_thumbnail(normalize_path(png_path), preview_root / "_thumbs", box_size)
        return self.preview_photo_from_path(preview_png, box_size)

    def preview_photo_from_path(self, preview_png: Path, box_size: int) -> tk.PhotoImage:
        resolved_text, cache_token = file_identity(preview_png)
        key = ("preview-photo", resolved_text, cache_token, int(box_size))
        cached = self.preview_photo_cache.get(key)
        if cached is not None:
            return cached
        image = tk.PhotoImage(file=str(preview_png))
        return self.preview_photo_cache.set(key, image)

    def _source_metadata_cache_key(self, source_path: Path) -> tuple:
        return source_metadata_cache_key_for(normalize_path(source_path), self.current_preview_root())

    def _summary_cache_key(self, source_path: Path) -> tuple:
        return summary_cache_key_for(normalize_path(source_path), self.current_preview_root())

    def _output_preview_cache_key(self, source_path: Path, sizes: list[int], filter_name: str, preview_nominal_size: int) -> tuple:
        return output_preview_cache_key_for(
            normalize_path(source_path),
            self.current_preview_root(),
            sizes,
            filter_name,
            preview_nominal_size,
        )

    def _touch_source_preview_artifacts(self, source_path: Path) -> None:
        touch_source_preview_artifacts(self.current_preview_root(), normalize_path(source_path))

    def _touch_output_preview_artifacts(self, preview: dict) -> None:
        touch_output_preview_artifacts(self.current_preview_root(), preview)

    def invalidate_source_caches(self, *paths: str | Path) -> None:
        normalized_paths = {
            str(normalize_path(Path(path)))
            for path in paths
            if str(path).strip()
        }
        if not normalized_paths:
            return

        self.source_metadata_cache.discard_where(lambda key, _value: len(key) > 1 and key[1] in normalized_paths)
        self.summary_cache.discard_where(lambda key, _value: len(key) > 1 and key[1] in normalized_paths)
        self.output_preview_cache.discard_where(lambda key, _value: len(key) > 1 and key[1] in normalized_paths)

    def ensure_source_metadata(self, source_path: Path) -> dict:
        return load_cached_source_metadata(source_path, self.current_preview_root(), self.source_metadata_cache)

    def ensure_summary(self, source_path: Path) -> dict:
        normalized = normalize_path(source_path)
        return load_cached_summary(
            normalized,
            self.current_preview_root(),
            self.source_metadata_cache,
            self.summary_cache,
            asset_summary=self.pack_asset_lookup.get(str(normalized)),
        )

    def ensure_output_preview(self, source_path: Path, preview_nominal_size: int | None = None) -> dict:
        sizes = self.try_target_sizes()[0]
        filter_name = self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER
        selected_preview_size = int(preview_nominal_size or self.current_preview_nominal_size())
        return load_cached_output_preview(
            source_path,
            self.current_preview_root(),
            sizes,
            filter_name,
            selected_preview_size,
            self.source_metadata_cache,
            self.output_preview_cache,
        )

    def try_target_sizes(self) -> tuple[list[int], str | None]:
        try:
            sizes = normalize_cursor_sizes(self.target_sizes_var.get(), fallback=self.target_sizes)
            self.target_sizes = sizes
            return sizes, None
        except Exception as exc:  # noqa: BLE001
            return list(self.target_sizes), str(exc)

    def current_target_sizes(self, normalize_display: bool = False) -> list[int]:
        sizes = normalize_cursor_sizes(self.target_sizes_var.get(), fallback=self.target_sizes)
        self.target_sizes = sizes
        if normalize_display:
            normalized_text = format_cursor_sizes(sizes)
            if self.target_sizes_var.get().strip() != normalized_text:
                self.target_sizes_var.set(normalized_text)
        return sizes

    def current_preview_nominal_size(self) -> int:
        sizes = self.try_target_sizes()[0]
        raw_value = self.preview_nominal_size_var.get().strip()
        if raw_value:
            try:
                return max(1, int(raw_value))
            except ValueError:
                pass
        return self._default_preview_size(sizes)

    def _default_preview_size(self, sizes: list[int]) -> int:
        return min(sizes, key=lambda size: (abs(size - 32), size))

    def _update_preview_size_choices(self) -> None:
        sizes, _error = self.try_target_sizes()
        values = [str(size) for size in sizes]
        self.preview_nominal_size_combo.configure(values=values)
        self.build_preview_size_combo.configure(values=values)
        current = self.preview_nominal_size_var.get().strip()
        if current not in values:
            self._suspend_refresh_traces = True
            try:
                self.preview_nominal_size_var.set(str(self._default_preview_size(sizes)))
            finally:
                self._suspend_refresh_traces = False

    def choose_source_dir(self) -> None:
        selected = filedialog.askdirectory(title="Choose Windows cursor folder")
        if not selected:
            return
        previous_value = self.source_dir_var.get().strip()
        normalized_selected = str(normalize_path(Path(selected)))
        self.source_dir_var.set(normalized_selected)
        if not previous_value or normalize_path(Path(previous_value)) != Path(normalized_selected):
            self._set_pack_analysis(None)
            self.slot_selection_context.clear()
            self.summary_cache.clear()
            self._refresh_all_views()
        if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
            self.theme_name_var.set(slugify_name(Path(normalized_selected).name))
        self.set_status(f"Selected source pack: {normalized_selected}")

    def choose_work_root(self) -> None:
        selected = filedialog.askdirectory(title="Choose output root")
        if not selected:
            return
        self.work_root_var.set(str(normalize_path(Path(selected))))
        self.clear_preview_caches()
        self.set_status(f"Selected output root: {self.work_root_var.get()}")
        self._refresh_all_views()

    def clear_preview_caches(self) -> None:
        self.preview_photo_cache.clear()
        self.source_metadata_cache.clear()
        self.output_preview_cache.clear()
        self.summary_cache.clear()

    def _set_analysis_loading(self, message: str) -> None:
        set_readonly_text(self.analysis_detail_text, message)
        if hasattr(self, "analysis_action_detail_var"):
            self.analysis_action_detail_var.set(message)
        if hasattr(self, "analysis_action_tree"):
            for item in self.analysis_action_tree.get_children():
                self.analysis_action_tree.delete(item)

    def analyze_pack(self) -> None:
        source_dir_text = self.source_dir_var.get().strip()
        if not source_dir_text:
            messagebox.showerror("Missing source folder", "Choose a Windows cursor folder first.")
            return
        source_dir = Path(source_dir_text).expanduser()
        if not source_dir.exists():
            messagebox.showerror("Missing source folder", f"Folder does not exist:\n{source_dir}")
            return
        self._set_analysis_busy(True, f"Analyzing {source_dir}")
        self._set_analysis_loading("Analyzing pack...")
        token = self.request_tracker.next("pack-analysis")

        def work() -> dict:
            return analyze_cursor_pack(source_dir)

        def on_success(result_token: TaskToken, analysis: dict) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            self._set_analysis_busy(False)
            self._set_pack_analysis(analysis)
            self.slot_selection_context = {
                slot["key"]: self._infer_selection_context(slot["key"], path)
                for slot in SLOT_DEFS
                for path in [self.slot_paths.get(slot["key"], "").strip()]
                if path
            }
            self.summary_cache.clear()
            self.set_status(f"Analyzed {analysis['counts']['total']} source assets")
            self._render_pack_analysis()
            self._refresh_all_views()

        def on_error(result_token: TaskToken, exc: BaseException) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            self._set_analysis_busy(False, "Pack analysis failed")
            self._render_pack_analysis()
            messagebox.showerror("Pack analysis failed", str(exc))

        self.task_runner.submit(token, work, on_success=on_success, on_error=on_error)

    def _render_pack_analysis(self) -> None:
        analysis = self.pack_analysis
        if analysis is None:
            self.analysis_counts_var.set("Files: --")
            self.analysis_inf_var.set("INF: --")
            self.analysis_hidpi_var.set("HiDPI: --")
            self.analysis_sizes_var.set("Largest native sizes: --")
            self.analysis_animated_var.set("Animated sources: --")
            self.analysis_total_value_var.set("--")
            self.analysis_total_note_var.set("Source files")
            self.analysis_animation_value_var.set("--")
            self.analysis_animation_note_var.set("Animated sources")
            self.analysis_hidpi_value_var.set("--")
            self.analysis_hidpi_note_var.set("HiDPI potential")
            self.analysis_attention_value_var.set("--")
            self.analysis_attention_note_var.set("Warnings and ambiguity")
            set_readonly_text(self.analysis_detail_text, "Analyze a source pack to see diagnostics.")
            self.analysis_action_detail_var.set("Analyze a pack to build an action queue.")
            self.analysis_action_items = {}
            for item in self.analysis_action_tree.get_children():
                self.analysis_action_tree.delete(item)
            for item in self.analysis_asset_tree.get_children():
                self.analysis_asset_tree.delete(item)
            return

        counts = analysis["counts"]
        warning_count = len(analysis.get("warnings", []))
        ambiguous_count = len(analysis.get("ambiguous_candidates", {}))
        duplicate_count = len(analysis.get("duplicate_artifacts", []))
        action_count = len(self._build_analysis_action_items())
        self.analysis_counts_var.set(
            f"Files: {counts['total']} total | {counts['cur']} .cur | {counts['ani']} .ani | {counts['png']} .png"
        )
        inf_details = analysis.get("install_inf")
        if inf_details is None:
            self.analysis_inf_var.set("INF: none found at pack root")
        else:
            self.analysis_inf_var.set(
                f"INF: {Path(inf_details['path']).name} | {inf_details['reason']} | "
                f"{analysis['install_inf_slots_resolved']} slot names resolved"
            )
        hidpi = analysis["hidpi_potential"]
        self.analysis_hidpi_var.set(
            f"HiDPI potential: {hidpi['rating']} | >=96px: {hidpi['supports_96_count']} | "
            f">=128px: {hidpi['supports_128_count']} | >=192px: {hidpi['supports_192_count']}"
        )
        self.analysis_sizes_var.set(
            "Largest native sizes: "
            + (", ".join(str(size) for size in analysis["largest_native_sizes_found"]) or "--")
        )
        self.analysis_animated_var.set(f"Animated sources detected: {len(analysis['animated_sources'])}")
        self.analysis_total_value_var.set(str(counts["total"]))
        self.analysis_total_note_var.set(f"{counts['cur']} CUR | {counts['ani']} ANI | {counts['png']} PNG")
        self.analysis_animation_value_var.set(str(len(analysis["animated_sources"])))
        self.analysis_animation_note_var.set(
            "Real animation preview available" if analysis["animated_sources"] else "Static-only or undetected"
        )
        self.analysis_hidpi_value_var.set(str(analysis["hidpi_potential"]["rating"]).upper())
        largest_sizes = ", ".join(str(size) for size in analysis["largest_native_sizes_found"][:4]) or "--"
        self.analysis_hidpi_note_var.set(f"Top native sizes: {largest_sizes}")
        self.analysis_attention_value_var.set(str(action_count))
        self.analysis_attention_note_var.set(
            f"{warning_count} warning(s), {ambiguous_count} ambiguous slot(s), {duplicate_count} duplicate/temp artifact(s), {action_count} action item(s)"
        )

        warning_lines = ["Pack outlook:"]
        if analysis.get("warnings"):
            warning_lines.extend(f"- {warning}" for warning in analysis["warnings"])
        else:
            warning_lines.append("- No immediate pack-level warnings.")
        ambiguous_items = analysis.get("ambiguous_candidates", {})
        if ambiguous_items:
            warning_lines.append("")
            warning_lines.append("Immediate review targets:")
            for slot_key, candidates in sorted(ambiguous_items.items()):
                labels = ", ".join(Path(candidate["path"]).name for candidate in candidates[:3])
                warning_lines.append(f"- {SLOT_BY_KEY[slot_key]['label']}: compare {labels}")
        duplicate_artifacts = analysis.get("duplicate_artifacts", [])
        if duplicate_artifacts:
            warning_lines.append("")
            warning_lines.append("Generated/duplicate risk:")
            warning_lines.extend(
                f"- {artifact['relative_path']} ({artifact['reason']})"
                for artifact in duplicate_artifacts[:6]
            )
        set_readonly_text(self.analysis_detail_text, "\n".join(warning_lines))

        for item in self.analysis_asset_tree.get_children():
            self.analysis_asset_tree.delete(item)
        for asset in analysis.get("asset_summaries", []):
            location = str(Path(asset["relative_path"]).parent)
            if location == ".":
                location = "pack root"
            flags = "; ".join(asset.get("warnings", [])[:2]) or "--"
            self.analysis_asset_tree.insert(
                "",
                "end",
                iid=asset["path"],
                text=asset["filename"],
                values=(
                    asset.get("source_type", "--"),
                    "yes" if asset.get("is_animated") else "no",
                    asset.get("size_summary", "--"),
                    flags,
                    location,
                ),
            )
        self._populate_analysis_action_tree()
        self._refresh_analysis_action_detail()

    def auto_prepare(self) -> None:
        source_dir = Path(self.source_dir_var.get().strip()).expanduser()
        if not str(source_dir):
            messagebox.showerror("Missing source folder", "Choose a Windows cursor folder first.")
            return
        if not source_dir.exists():
            messagebox.showerror("Missing source folder", f"Folder does not exist:\n{source_dir}")
            return
        work_root = Path(self.work_root_var.get().strip()).expanduser()
        if not str(work_root):
            messagebox.showerror("Missing output root", "Choose an output root first.")
            return

        prep_dir = work_root / "_prepared" / slugify_name(source_dir.name)
        self._set_auto_prepare_busy(True, f"Preparing {source_dir.name}")
        self._set_analysis_loading("Analyzing pack and preparing slot mapping...")
        token = self.request_tracker.next("auto-prepare")

        def work() -> dict:
            return prepare_windows_cursor_set(source_dir, prep_dir)

        def on_success(result_token: TaskToken, summary: dict) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            self._set_auto_prepare_busy(False)
            self._set_pack_analysis(summary.get("analysis"))
            self._render_pack_analysis()
            mapping_path = Path(summary["mapping_json"]).resolve()
            payload = load_mapping_payload(mapping_path)
            self.apply_payload(payload)
            self._apply_prepare_selection_context(summary)
            self._render_selected_slot_text()
            self._refresh_candidate_detail()
            self._refresh_build_summary()
            self.current_mapping_path = mapping_path
            if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
                self.theme_name_var.set(slugify_name(source_dir.name))
            self.set_status(
                f"Auto-filled {summary['selected_slot_count']} slots; review them in the next tab when ready"
            )
            messagebox.showinfo(
                "Auto-Fill Complete",
                f"Prepared {summary['selected_slot_count']} slots.\n\nMapping JSON:\n{mapping_path}",
            )

        def on_error(result_token: TaskToken, exc: BaseException) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            self._set_auto_prepare_busy(False, "Auto-fill failed")
            self._render_pack_analysis()
            messagebox.showerror("Auto-fill failed", str(exc))

        self.task_runner.submit(token, work, on_success=on_success, on_error=on_error)

    def apply_payload(self, payload: dict) -> None:
        build_options = payload.get("build_options", {})
        scale_filter = build_options.get("scale_filter")
        target_sizes = build_options.get("target_sizes")
        self.target_sizes = normalize_cursor_sizes(target_sizes, fallback=DEFAULT_CURSOR_SIZES)
        self._suspend_refresh_traces = True
        try:
            if scale_filter in SCALE_FILTER_CHOICES:
                self.scale_filter_var.set(scale_filter)
            self.target_sizes_var.set(format_cursor_sizes(self.target_sizes))
        finally:
            self._suspend_refresh_traces = False
        self._update_preview_size_choices()
        self.profile_base_preset_label = restore_profile_base_preset(
            payload.get("build_profile"),
            self.target_sizes,
            scale_filter,
        )
        if self.profile_base_preset_label:
            self.build_preset_var.set(self.profile_base_preset_label)
        self._refresh_build_profile_state()

        selected = payload.get("selected_slots", {})
        role_map = payload.get("resolved_role_map", {})
        self.slot_paths = {slot["key"]: "" for slot in SLOT_DEFS}
        self.slot_selection_context = {}

        for slot_key, item in selected.items():
            if slot_key in self.slot_paths:
                self.slot_paths[slot_key] = item.get("path", "")

        if not selected and role_map:
            for slot in SLOT_DEFS:
                for role in slot["roles"]:
                    if role in role_map:
                        self.slot_paths[slot["key"]] = role_map[role]
                        break

        stored_context = payload.get("selection_context", {})
        if isinstance(stored_context, dict):
            for slot_key, context in stored_context.items():
                if slot_key in self.slot_paths and self.slot_paths[slot_key]:
                    self.slot_selection_context[slot_key] = dict(context)
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            if path and slot["key"] not in self.slot_selection_context:
                self.slot_selection_context[slot["key"]] = self._infer_selection_context(slot["key"], path)

        self.clear_preview_caches()
        self._refresh_all_views()

    def gather_mapping(self) -> tuple[dict, dict]:
        selected_slots = {}
        duplicates = []
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            if not path:
                continue
            source_path = Path(path).expanduser()
            if not source_path.exists():
                raise ValueError(f"{slot['label']}: file does not exist: {source_path}")
            ext = source_path.suffix.lower()
            if ext not in slot["allowed_extensions"]:
                allowed = ", ".join(slot["allowed_extensions"])
                raise ValueError(f"{slot['label']}: expected one of {allowed}, got {ext or 'no extension'}")
            if slot["key"] in selected_slots:
                duplicates.append(slot["label"])
                continue
            selected_slots[slot["key"]] = {"slot": slot, "path": str(source_path.resolve())}

        if duplicates:
            raise ValueError(f"Duplicate slot assignments: {', '.join(sorted(set(duplicates)))}")

        resolved = {}
        for item in selected_slots.values():
            for role in item["slot"]["roles"]:
                resolved[role] = item["path"]
        return selected_slots, resolved

    def render_markdown(self, selected_slots: dict, resolved: dict, target_sizes: list[int]) -> str:
        lines = [
            "# Cursor Source Slot Mapping",
            "",
            "## Build Options",
            "",
            f"- Sizes: `{format_cursor_sizes(target_sizes)}`",
            f"- Scale filter: `{self.scale_filter_var.get()}`",
            "",
            "## Selected Source Slots",
            "",
        ]
        for item in sorted(selected_slots.values(), key=lambda item: item["slot"]["label"]):
            lines.append(f"- `{item['slot']['label']}` -> `{item['path']}`")
        if not selected_slots:
            lines.append("- None selected yet")
        lines.extend(
            [
                "",
                "## Expanded Linux Role Map",
                "",
                "| Linux role | Source path |",
                "|---|---|",
            ]
        )
        for role, path in sorted(resolved.items()):
            lines.append(f"| `{role}` | `{path}` |")
        if not resolved:
            lines.append("| _none_ | _none_ |")
        return "\n".join(lines) + "\n"

    def save_json(self) -> None:
        try:
            selected_slots, resolved = self.gather_mapping()
            target_sizes = self.current_target_sizes(normalize_display=True)
        except ValueError as exc:
            messagebox.showerror("Invalid mapping", str(exc))
            return
        payload = build_payload(
            selected_slots,
            resolved,
            target_sizes,
            self.scale_filter_var.get(),
            selection_context=self._selection_context_payload(),
            build_profile=build_profile_payload(self.current_build_profile),
        )
        target = filedialog.asksaveasfilename(
            title="Save role mapping as JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if not target:
            return
        target_path = Path(target)
        target_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self.current_mapping_path = target_path.resolve()
        self.set_status(f"Saved mapping: {target}")
        messagebox.showinfo("Saved", f"Saved JSON mapping to:\n{target}")

    def save_markdown(self) -> None:
        try:
            selected_slots, resolved = self.gather_mapping()
            target_sizes = self.current_target_sizes(normalize_display=True)
        except ValueError as exc:
            messagebox.showerror("Invalid mapping", str(exc))
            return
        target = filedialog.asksaveasfilename(
            title="Save role mapping as Markdown",
            defaultextension=".md",
            filetypes=[("Markdown files", "*.md")],
        )
        if not target:
            return
        Path(target).write_text(self.render_markdown(selected_slots, resolved, target_sizes), encoding="utf-8")
        self.set_status(f"Saved mapping markdown: {target}")
        messagebox.showinfo("Saved", f"Saved Markdown mapping to:\n{target}")

    def load_json(self) -> None:
        target = filedialog.askopenfilename(title="Load role mapping JSON", filetypes=[("JSON files", "*.json")])
        if not target:
            return
        try:
            payload = load_mapping_payload(Path(target))
            self.apply_payload(payload)
            self.current_mapping_path = Path(target).resolve()
            if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
                self.theme_name_var.set(slugify_name(Path(target).stem))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Unable to load mapping", str(exc))
            return
        self.set_status(f"Loaded mapping: {target}")
        self.notebook.select(self.review_tab)

    def select_slot(self, slot_key: str) -> None:
        previous_slot = self.selected_slot_key
        self.selected_slot_key = slot_key
        self.selected_candidate_path = None
        if previous_slot in self.slot_cards:
            self._render_slot_card(previous_slot)
        if slot_key in self.slot_cards:
            self._render_slot_card(slot_key)
        self._schedule_selected_slot_detail_refresh()

    def focus_slot_candidates(self, slot_key: str) -> None:
        self.select_slot(slot_key)
        self.notebook.select(self.review_tab)
        if hasattr(self, "review_tool_notebook"):
            self.review_tool_notebook.select(self.candidate_browser_tab)

    def browse_slot(self, slot_key: str) -> None:
        slot = SLOT_BY_KEY[slot_key]
        patterns = " ".join(f"*{ext}" for ext in slot["allowed_extensions"])
        label = f"{slot['label']} files"
        allowed = [(label, patterns), ("All supported", "*.ani *.cur *.png *.json")]
        file_path = filedialog.askopenfilename(title=f"Select source for {slot['label']}", filetypes=allowed)
        if not file_path:
            return
        previous_path = self.slot_paths.get(slot_key, "")
        normalized_path = str(normalize_path(Path(file_path)))
        self.slot_paths[slot_key] = normalized_path
        self._set_selection_context(
            slot_key,
            {
                "origin": "manual-browse",
                "path": normalized_path,
                "reason": "Assigned manually through Browse Source.",
            },
        )
        self.invalidate_source_caches(previous_path, self.slot_paths[slot_key])
        self.select_slot(slot_key)
        self._refresh_slot_cards()
        self.set_status(f"Assigned {slot['label']} -> {file_path}")

    def clear_slot(self, slot_key: str) -> None:
        previous_path = self.slot_paths.get(slot_key, "")
        self.slot_paths[slot_key] = ""
        self._set_selection_context(slot_key, None)
        self.invalidate_source_caches(previous_path)
        self.select_slot(slot_key)
        self._refresh_slot_cards()
        self._refresh_build_summary()
        self.set_status(f"Cleared {SLOT_BY_KEY[slot_key]['label']}")

    def apply_selected_candidate(self) -> None:
        if not self.selected_candidate_path:
            messagebox.showerror("No candidate selected", "Select a candidate first.")
            return
        previous_path = self.slot_paths.get(self.selected_slot_key, "")
        self.slot_paths[self.selected_slot_key] = self.selected_candidate_path
        candidate = self._candidate_for_slot_path(self.selected_slot_key, self.selected_candidate_path)
        self._set_selection_context(
            self.selected_slot_key,
            {
                "origin": "manual-candidate",
                "path": self.selected_candidate_path,
                "reason": "Assigned manually from the ranked candidate browser.",
                "rank": None if candidate is None else candidate.get("rank"),
                "score": None if candidate is None else candidate.get("score"),
            },
        )
        self.invalidate_source_caches(previous_path, self.selected_candidate_path)
        self.select_slot(self.selected_slot_key)
        self._refresh_slot_cards()
        self.set_status(
            f"Assigned {SLOT_BY_KEY[self.selected_slot_key]['label']} -> {Path(self.selected_candidate_path).name}"
        )

    def _slot_quality(self, slot_key: str, path: str) -> dict | None:
        if not path:
            return None
        state = self.slot_states.get(slot_key)
        if state is not None and state.path == path and state.quality is not None and not state.loading:
            return state.quality
        summary = self.ensure_summary(Path(path))
        target_sizes = self.try_target_sizes()[0]
        return build_slot_quality(
            slot_key,
            summary,
            target_sizes,
            pack_analysis=self.pack_analysis,
            ambiguous_candidates=self._ambiguous_candidates_for_slot(slot_key),
            selection_context=self._selection_context_for_slot(slot_key, path),
        )

    def _slot_card_family(self, slot_key: str) -> str:
        return f"slot-card:{slot_key}"

    def _selected_slot_path(self) -> str:
        return self.slot_paths.get(self.selected_slot_key, "").strip()

    def _render_slot_card(self, slot_key: str) -> None:
        state = self.slot_states[slot_key]
        card = self.slot_cards[slot_key]
        card.preview_image = None
        if state.thumbnail_path and not state.loading:
            try:
                card.preview_image = self.preview_photo_from_path(Path(state.thumbnail_path), CARD_PREVIEW_SIZE)
            except Exception as exc:  # noqa: BLE001
                state.error = str(exc)
                state.thumbnail_path = None

        selected = slot_key == self.selected_slot_key
        if not state.path:
            card.update_card(None, None, None, selected)
            return

        if state.loading:
            card.update_card(
                state.path,
                state.summary,
                state.quality,
                selected,
                loading_message="Preparing source preview...",
            )
            return

        if state.error:
            summary = state.summary or {
                "filename": Path(state.path).name,
                "warnings": [state.error],
                "path": state.path,
                "hotspot_summary": "--",
                "size_summary": "--",
                "source_type": Path(state.path).suffix.lower().lstrip(".") or "unknown",
                "is_animated": False,
                "frame_count": 0,
            }
            quality = state.quality or {
                "label": "redraw recommended",
                "reason": state.error,
                "warnings": [state.error],
            }
            card.update_card(state.path, summary, quality, selected)
            return

        card.update_card(state.path, state.summary, state.quality, selected)

    def _refresh_slot_cards(self) -> None:
        for slot in SLOT_DEFS:
            slot_key = slot["key"]
            path = self.slot_paths.get(slot_key, "").strip()
            state = self.slot_states[slot_key]
            if not path:
                self.request_tracker.invalidate(self._slot_card_family(slot_key))
                self.slot_states[slot_key] = SlotRenderState()
                self._render_slot_card(slot_key)
                continue

            token = self.request_tracker.next(self._slot_card_family(slot_key))
            state.path = path
            state.loading = True
            state.summary = None
            state.quality = None
            state.thumbnail_path = None
            state.error = None
            self._render_slot_card(slot_key)

            preview_root = self.current_preview_root()
            preview_nominal_size = self.current_preview_nominal_size()
            target_sizes = self.try_target_sizes()[0]
            asset_summary = self.pack_asset_lookup.get(path)
            ambiguous_candidates = self._ambiguous_candidates_for_slot(slot_key)
            pack_analysis = self.pack_analysis

            def work(
                source_path: str = path,
                preview_root: Path = preview_root,
                preview_nominal_size: int = preview_nominal_size,
                target_sizes: list[int] = list(target_sizes),
                slot_key: str = slot_key,
                asset_summary: dict | None = asset_summary,
                ambiguous_candidates: list[dict] = list(ambiguous_candidates),
                pack_analysis: dict | None = pack_analysis,
            ) -> dict:
                return prepare_slot_card_payload(
                    Path(source_path),
                    preview_root,
                    preview_nominal_size,
                    target_sizes,
                    slot_key,
                    self.source_metadata_cache,
                    self.summary_cache,
                    pack_analysis=pack_analysis,
                    asset_summary=asset_summary,
                    ambiguous_candidates=ambiguous_candidates,
                )

            def on_success(
                result_token: TaskToken,
                payload: dict,
                *,
                slot_key: str = slot_key,
                source_path: str = path,
            ) -> None:
                if not self.request_tracker.is_current(result_token):
                    return
                if self.slot_paths.get(slot_key, "").strip() != source_path:
                    return
                state = self.slot_states[slot_key]
                state.path = source_path
                state.loading = False
                state.summary = payload["summary"]
                state.quality = payload["quality"]
                state.thumbnail_path = payload["thumbnail_path"]
                state.error = None
                self._render_slot_card(slot_key)
                if slot_key == self.selected_slot_key:
                    self._render_selected_slot_text()
                self._refresh_build_summary()

            def on_error(
                result_token: TaskToken,
                exc: BaseException,
                *,
                slot_key: str = slot_key,
                source_path: str = path,
            ) -> None:
                if not self.request_tracker.is_current(result_token):
                    return
                if self.slot_paths.get(slot_key, "").strip() != source_path:
                    return
                state = self.slot_states[slot_key]
                state.path = source_path
                state.loading = False
                state.summary = None
                state.quality = None
                state.thumbnail_path = None
                state.error = str(exc)
                self._render_slot_card(slot_key)
                if slot_key == self.selected_slot_key:
                    self._render_selected_slot_text()
                self._refresh_build_summary()

            self.task_runner.submit(
                token,
                work,
                on_success=on_success,
                on_error=on_error,
                should_run=self.request_tracker.is_current,
            )

    def _refresh_selected_slot_detail(self) -> None:
        self.refresh_coalescer.cancel("selected-detail-refresh")
        slot = SLOT_BY_KEY[self.selected_slot_key]
        path = self._selected_slot_path()
        self.selected_slot_title_var.set(slot["label"])
        self.selected_candidate_path = None

        if not path:
            self.request_tracker.invalidate("selected-detail")
            self.selected_slot_meta_var.set("No source selected yet.")
            self.selected_slot_path_var.set("Use Auto-Fill, Browse Source, or choose a ranked candidate.")
            set_readonly_text(self.slot_warning_text, "This slot is currently unassigned.")
            self.source_preview_panel.clear("No source selected")
            self.output_preview_panel.clear("No source selected")
            self._populate_candidate_tree()
            self._refresh_candidate_detail()
            self._refresh_compare_view()
            return

        self._render_selected_slot_text()
        self._populate_candidate_tree()
        self._refresh_candidate_detail()
        self._refresh_compare_view()

        token = self.request_tracker.next("selected-detail")
        preview_root = self.current_preview_root()
        preview_nominal_size = self.current_preview_nominal_size()
        target_sizes = self.try_target_sizes()[0]
        scale_filter = self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER
        source_path = Path(path)

        self.source_preview_panel.set_loading("Preparing source preview...")
        self.output_preview_panel.set_loading("Preparing Linux output preview...")

        def source_work(
            source_path: Path = source_path,
            preview_root: Path = preview_root,
            preview_nominal_size: int = preview_nominal_size,
        ) -> dict:
            return prepare_source_preview_payload(
                source_path,
                preview_root,
                preview_nominal_size,
                self.source_metadata_cache,
            )

        def source_success(result_token: TaskToken, payload: dict, *, source_path: str = path) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            if self._selected_slot_path() != source_path:
                return
            self._apply_preview_panel_payload(self.source_preview_panel, payload)

        def source_error(result_token: TaskToken, exc: BaseException, *, source_path: str = path) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            if self._selected_slot_path() != source_path:
                return
            self.source_preview_panel.clear(str(exc))

        self.task_runner.submit(
            token,
            source_work,
            on_success=source_success,
            on_error=source_error,
            should_run=self.request_tracker.is_current,
        )

        def output_work(
            source_path: Path = source_path,
            preview_root: Path = preview_root,
            preview_nominal_size: int = preview_nominal_size,
            target_sizes: list[int] = list(target_sizes),
            scale_filter: str = scale_filter,
        ) -> dict:
            return prepare_output_preview_payload(
                source_path,
                preview_root,
                preview_nominal_size,
                target_sizes,
                scale_filter,
                self.source_metadata_cache,
                self.output_preview_cache,
            )

        def output_success(result_token: TaskToken, payload: dict, *, source_path: str = path) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            if self._selected_slot_path() != source_path:
                return
            self._apply_preview_panel_payload(self.output_preview_panel, payload)

        def output_error(result_token: TaskToken, exc: BaseException, *, source_path: str = path) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            if self._selected_slot_path() != source_path:
                return
            self.output_preview_panel.clear(str(exc))

        self.task_runner.submit(
            token,
            output_work,
            on_success=output_success,
            on_error=output_error,
            should_run=self.request_tracker.is_current,
        )

    def _render_selected_slot_text(self) -> None:
        slot = SLOT_BY_KEY[self.selected_slot_key]
        path = self._selected_slot_path()
        self.selected_slot_title_var.set(slot["label"])
        if not path:
            self.selected_slot_meta_var.set("No source selected yet.")
            self.selected_slot_path_var.set("Use Auto-Fill, Browse Source, or choose a ranked candidate.")
            set_readonly_text(self.slot_warning_text, "This slot is currently unassigned.")
            return

        self.selected_slot_path_var.set(compact_path(path, max_len=130))
        state = self.slot_states[self.selected_slot_key]
        if state.path != path or state.loading:
            self.selected_slot_meta_var.set("Loading source metadata and quality forecast...")
            set_readonly_text(self.slot_warning_text, "- Preparing source metadata and quality forecast...")
            return
        if state.error:
            self.selected_slot_meta_var.set("Unable to inspect the selected source.")
            set_readonly_text(self.slot_warning_text, f"- {state.error}")
            return
        if state.summary is None or state.quality is None:
            self.selected_slot_meta_var.set("Loading source metadata and quality forecast...")
            set_readonly_text(self.slot_warning_text, "- Preparing source metadata and quality forecast...")
            return

        quality = state.quality
        summary = state.summary
        self.selected_slot_meta_var.set(
            f"{badges_for_summary(summary)} | Quality: {quality['label']} ({quality.get('confidence', 'low')} confidence) | "
            f"{quality['decision']}"
        )
        set_readonly_text(self.slot_warning_text, self._slot_guidance_text(self.selected_slot_key, summary, quality))

    def _apply_preview_panel_payload(self, panel: AnimationPreviewPanel, payload: dict) -> None:
        preview = payload.get("preview")
        if preview is None:
            panel.clear(payload.get("reason", "No preview available"))
            return
        if not preview.get("frames"):
            panel.clear(payload.get("reason", "No preview available"))
            return
        images = [
            self.preview_photo_from_path(Path(preview_path), panel.canvas_size)
            for preview_path in preview["thumbnail_paths"]
        ]
        panel.set_frames(
            preview["frames"],
            images,
            preview["summary"],
            preview["frame_info"],
            inspection_text=preview["inspection_text"],
            warning_text=preview["warning_text"],
        )

    def _populate_candidate_tree(self) -> None:
        for item in self.candidate_tree.get_children():
            self.candidate_tree.delete(item)
        slot_candidates = {}
        if self.pack_analysis is not None:
            slot_candidates = self.pack_analysis.get("slot_candidates", {})
        candidates = slot_candidates.get(self.selected_slot_key, [])
        if not candidates:
            self.candidate_tree.insert("", "end", iid="__none__", text="No ranked pack candidates available", values=("", "", "", ""))
            return
        for candidate in candidates[:12]:
            candidate_id = candidate["path"]
            self.candidate_tree.insert(
                "",
                "end",
                iid=candidate_id,
                text=candidate["filename"],
                values=(
                    ("ANI" if candidate["is_animated"] else "Static"),
                    candidate["size_summary"],
                    candidate["score"],
                    self._candidate_reason_for_tree(self.selected_slot_key, candidate),
                ),
            )
        current_path = self._selected_slot_path()
        if current_path and current_path in self.candidate_tree.get_children():
            self.candidate_tree.selection_set(current_path)
        else:
            first = self.candidate_tree.get_children()[0]
            if first != "__none__":
                self.candidate_tree.selection_set(first)

    def _candidate_lookup(self) -> dict[str, dict]:
        if self.pack_analysis is None:
            return {}
        candidates = self.pack_analysis.get("slot_candidates", {}).get(self.selected_slot_key, [])
        return {candidate["path"]: candidate for candidate in candidates}

    def _refresh_candidate_detail(self) -> None:
        self.refresh_coalescer.cancel("candidate-detail-refresh")
        selection = self.candidate_tree.selection()
        if not selection or selection[0] == "__none__":
            self.request_tracker.invalidate("candidate-preview")
            self.selected_candidate_path = None
            set_readonly_text(self.candidate_reason_text, "Select a candidate to inspect its ranking logic.")
            current_path = self._selected_slot_path()
            if current_path:
                quality = self._slot_quality(self.selected_slot_key, current_path) or {
                    "label": "--",
                    "confidence": "low",
                    "decision": "review",
                    "reason": "No quality data yet.",
                }
                set_readonly_text(
                    self.current_choice_text,
                    self._current_choice_text(self.selected_slot_key, current_path, quality, None),
                )
            else:
                set_readonly_text(self.current_choice_text, "Auto-fill or assign a slot to explain the active choice.")
            self.candidate_preview_panel.clear("No candidate selected")
            self._schedule_compare_view_refresh()
            return
        candidate_path = selection[0]
        self.selected_candidate_path = candidate_path
        set_readonly_text(self.candidate_reason_text, "Preparing candidate metadata and ranking explanation...")
        set_readonly_text(self.current_choice_text, "Preparing current-choice provenance...")
        self.candidate_preview_panel.set_loading("Loading candidate preview...")
        token = self.request_tracker.next("candidate-preview")
        candidate = self._candidate_lookup().get(candidate_path, {})
        preview_root = self.current_preview_root()
        preview_nominal_size = self.current_preview_nominal_size()
        target_sizes = self.try_target_sizes()[0]
        slot_key = self.selected_slot_key
        asset_summary = self.pack_asset_lookup.get(candidate_path)
        ambiguous_candidates = self._ambiguous_candidates_for_slot(slot_key)
        pack_analysis = self.pack_analysis

        def work(
            candidate_path: str = candidate_path,
            preview_root: Path = preview_root,
            preview_nominal_size: int = preview_nominal_size,
            target_sizes: list[int] = list(target_sizes),
            slot_key: str = slot_key,
            asset_summary: dict | None = asset_summary,
            ambiguous_candidates: list[dict] = list(ambiguous_candidates),
            pack_analysis: dict | None = pack_analysis,
        ) -> dict:
            return prepare_candidate_preview_payload(
                Path(candidate_path),
                preview_root,
                preview_nominal_size,
                target_sizes,
                slot_key,
                self.source_metadata_cache,
                self.summary_cache,
                pack_analysis=pack_analysis,
                asset_summary=asset_summary,
                ambiguous_candidates=ambiguous_candidates,
            )

        def on_success(
            result_token: TaskToken,
            payload: dict,
            *,
            candidate_path: str = candidate_path,
            slot_key: str = slot_key,
            candidate: dict = dict(candidate),
        ) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            if self.selected_slot_key != slot_key:
                return
            selection = self.candidate_tree.selection()
            if not selection or selection[0] != candidate_path:
                return
            summary = payload["summary"]
            quality = payload["quality"]
            current_path = self._selected_slot_path()
            current_quality = self._slot_quality(slot_key, current_path) if current_path else None
            if current_quality is None and current_path:
                current_quality = {
                    "label": "--",
                    "confidence": "low",
                    "decision": "review",
                    "reason": "Quality data is still loading.",
                }
            set_readonly_text(
                self.candidate_reason_text,
                self._candidate_explanation_text(slot_key, candidate or {"path": candidate_path}, summary, quality),
            )
            if current_path and current_quality is not None:
                set_readonly_text(
                    self.current_choice_text,
                    self._current_choice_text(slot_key, current_path, current_quality, candidate),
                )
            else:
                set_readonly_text(self.current_choice_text, "Assign the slot first to compare the active choice against this candidate.")
            self._apply_preview_panel_payload(self.candidate_preview_panel, payload)
            self._schedule_compare_view_refresh()

        def on_error(
            result_token: TaskToken,
            exc: BaseException,
            *,
            candidate_path: str = candidate_path,
            slot_key: str = slot_key,
        ) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            if self.selected_slot_key != slot_key:
                return
            selection = self.candidate_tree.selection()
            if not selection or selection[0] != candidate_path:
                return
            set_readonly_text(self.candidate_reason_text, f"- Unable to inspect {Path(candidate_path).name}\n- {exc}")
            current_path = self._selected_slot_path()
            current_quality = self._slot_quality(slot_key, current_path) if current_path else None
            if current_path and current_quality is not None:
                set_readonly_text(
                    self.current_choice_text,
                    self._current_choice_text(slot_key, current_path, current_quality, candidate),
                )
            else:
                set_readonly_text(self.current_choice_text, "Current-choice provenance is unavailable right now.")
            self.candidate_preview_panel.clear(str(exc))
            self._schedule_compare_view_refresh()

        self.task_runner.submit(
            token,
            work,
            on_success=on_success,
            on_error=on_error,
            should_run=self.request_tracker.is_current,
        )

    def _refresh_build_summary(self) -> None:
        sizes, size_error = self.try_target_sizes()
        try:
            selected_slots, resolved = self.gather_mapping()
        except ValueError as exc:
            selected_slots = {}
            resolved = {}
            mapping_error = str(exc)
        else:
            mapping_error = None

        pending_slots = []
        quality_entries: list[tuple[dict, dict, dict | None]] = []
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            if not path:
                continue
            state = self.slot_states[slot["key"]]
            if state.path != path or state.loading:
                pending_slots.append(slot["label"])
                continue
            if state.error:
                quality_entries.append(
                    (
                        slot,
                        {
                            "label": "redraw recommended",
                            "confidence": "low",
                            "decision": "reduce preset or replace art",
                            "warnings": [state.error],
                            "actions": [state.error],
                        },
                        self._selection_context_for_slot(slot["key"], path),
                    )
                )
                continue
            quality = state.quality
            if quality is None:
                pending_slots.append(slot["label"])
                continue
            quality_entries.append((slot, quality, self._selection_context_for_slot(slot["key"], path)))

        snapshot = build_readiness_snapshot(
            quality_entries=quality_entries,
            pending_slots=pending_slots,
            selected_slot_count=len(selected_slots),
            resolved_role_count=len(resolved),
            target_sizes=sizes,
            size_error=size_error,
            mapping_error=mapping_error,
            pack_analysis=self.pack_analysis,
            safe_preset_label=SAFE_PRESET_LABEL,
        )
        self.overall_quality_var.set(snapshot.overall_quality_text)
        self.readiness_var.set(snapshot.readiness_headline)
        self.readiness_detail_var.set(
            f"{snapshot.readiness_detail} | Forecast: {snapshot.overall_quality_text.removeprefix('Overall quality forecast: ')}"
        )
        self.review_queue_var.set(snapshot.review_queue_headline)
        self.review_queue_hint_var.set(snapshot.review_queue_hint)
        set_readonly_text(self.build_warning_text, snapshot.guidance_text)

        build_summary_lines = [
            f"Theme name: {self.theme_name_var.get().strip() or 'Custom-cursor'}",
            f"Build profile: {self.current_build_profile.label}",
            f"Profile detail: {self.current_build_profile.detail}",
            f"Output root: {self.work_root_var.get().strip() or DEFAULT_WORK_ROOT}",
            f"Target sizes: {format_cursor_sizes(sizes)}",
            f"Scale filter: {self.scale_filter_var.get()}",
            f"Readiness: {snapshot.readiness_headline.removeprefix('Pack readiness: ')}",
            f"Workflow summary: {snapshot.readiness_detail}",
        ]
        if snapshot.suggested_preset:
            build_summary_lines.append(f"Suggested safer preset: {snapshot.suggested_preset}")
        if mapping_error:
            build_summary_lines.append(f"Mapping error: {mapping_error}")
        if size_error:
            build_summary_lines.append(f"Output size warning: {size_error}")
        build_summary_lines.extend(["", "Selected slots:"])
        if selected_slots:
            for item in sorted(selected_slots.values(), key=lambda item: item["slot"]["label"]):
                build_summary_lines.append(f"- {item['slot']['label']}: {item['path']}")
        else:
            build_summary_lines.append("- none")
        build_summary_lines.extend(["", "Expanded Linux role map:"])
        if resolved:
            for role, path in sorted(resolved.items()):
                build_summary_lines.append(f"- {role}: {path}")
        else:
            build_summary_lines.append("- none")
        set_readonly_text(self.build_summary_text, "\n".join(build_summary_lines))

    def _refresh_all_views(self) -> None:
        self._cancel_scheduled_refreshes()
        self._refresh_build_profile_state()
        self._update_preview_size_choices()
        self._render_pack_analysis()
        self._refresh_slot_cards()
        self._refresh_selected_slot_detail()
        self._refresh_build_summary()
        try:
            selected_slots, resolved = self.gather_mapping()
            self.summary_var.set(
                f"{len(selected_slots)} source slots selected, {len(resolved)} Linux cursor roles resolved"
            )
        except Exception as exc:  # noqa: BLE001
            self.summary_var.set(str(exc))

    def _on_target_sizes_changed(self) -> None:
        if self._suspend_refresh_traces:
            return
        self.on_build_settings_changed()

    def _on_scale_filter_changed(self) -> None:
        if self._suspend_refresh_traces:
            return
        self.on_build_settings_changed()

    def _on_preview_size_changed(self) -> None:
        if self._suspend_refresh_traces:
            return
        self._schedule_preview_size_refresh()

    def on_build_settings_changed(self) -> None:
        self._refresh_build_profile_state()
        self._schedule_build_settings_refresh()

    def apply_selected_preset(self) -> None:
        preset = resolve_build_preset(self.build_preset_var.get())
        self._suspend_refresh_traces = True
        try:
            self.target_sizes_var.set(format_cursor_sizes(preset["target_sizes"]))
            self.scale_filter_var.set(preset["scale_filter"])
            self.build_preset_var.set(preset["label"])
            self.preset_description_var.set(describe_build_preset(preset["label"]))
            self.preview_nominal_size_var.set(str(self._default_preview_size(preset["target_sizes"])))
        finally:
            self._suspend_refresh_traces = False
        self.profile_base_preset_label = preset["label"]
        self._refresh_build_profile_state()
        self.set_status(f"Applied preset: {preset['label']}")
        self._refresh_all_views()

    def build_and_package(self) -> None:
        try:
            selected_slots, resolved = self.gather_mapping()
            target_sizes = self.current_target_sizes(normalize_display=True)
        except ValueError as exc:
            messagebox.showerror("Invalid mapping", str(exc))
            return
        if not selected_slots:
            messagebox.showerror("No source slots", "Assign at least one slot before building.")
            return

        theme_name_default = self.theme_name_var.get().strip() or "Custom-cursor"
        theme_name = simpledialog.askstring(
            "Theme Name",
            "Enter the new cursor theme name:",
            initialvalue=theme_name_default,
            parent=self.root,
        )
        if not theme_name:
            return
        safe_theme_name = slugify_name(theme_name.strip())
        self.theme_name_var.set(theme_name.strip())

        work_root = Path(self.work_root_var.get().strip()).expanduser()
        if not str(work_root):
            messagebox.showerror("Missing output root", "Choose an output root first.")
            return
        work_root.mkdir(parents=True, exist_ok=True)

        payload = build_payload(
            selected_slots,
            resolved,
            target_sizes,
            self.scale_filter_var.get(),
            selection_context=self._selection_context_payload(),
        )
        scale_filter = self.scale_filter_var.get()
        build_root = work_root / "_builds" / safe_theme_name
        mapping_store_dir = work_root / "_mappings"
        mapping_path = mapping_store_dir / f"{safe_theme_name}.json"
        final_theme_dir = work_root / safe_theme_name
        tar_path = work_root / f"{safe_theme_name}.tar.gz"

        existing_paths = [path for path in (build_root, final_theme_dir, tar_path) if path.exists()]
        if existing_paths:
            names = "\n".join(str(path) for path in existing_paths)
            if not messagebox.askyesno(
                "Overwrite Existing Output",
                f"The following output paths already exist and will be replaced:\n\n{names}",
            ):
                return

        self._set_build_busy(True, f"Building Linux cursor theme: {safe_theme_name}")
        token = self.request_tracker.next("build-and-package")

        def work() -> dict:
            if build_root.exists():
                shutil.rmtree(build_root)
            build_root.mkdir(parents=True, exist_ok=True)
            mapping_store_dir.mkdir(parents=True, exist_ok=True)
            mapping_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            manifest = build_theme_from_mapping(
                mapping_path,
                build_root,
                safe_theme_name,
                target_sizes,
                scale_filter=scale_filter,
            )
            built_theme_dir = Path(manifest["theme_dir"]).resolve()

            if final_theme_dir.exists():
                shutil.rmtree(final_theme_dir)
            shutil.copytree(built_theme_dir, final_theme_dir)

            if tar_path.exists():
                tar_path.unlink()
            package_theme(final_theme_dir, tar_path)

            return {
                "mapping_path": str(mapping_path.resolve()),
                "final_theme_dir": str(final_theme_dir.resolve()),
                "tar_path": str(tar_path.resolve()),
            }

        def on_success(result_token: TaskToken, result: dict) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            self._set_build_busy(False)
            final_theme_dir = Path(result["final_theme_dir"])
            tar_path = Path(result["tar_path"])
            mapping_path = Path(result["mapping_path"])
            self.current_mapping_path = mapping_path
            self.last_theme_dir = final_theme_dir
            self.last_tar_path = tar_path
            self.last_output_var.set(
                f"Theme directory: {final_theme_dir}\nTarball: {tar_path}\nMapping JSON: {mapping_path}"
            )
            self.notebook.select(self.build_tab)
            self.set_status(f"Built theme and tarball: {tar_path}")
            messagebox.showinfo(
                "Build Complete",
                f"Theme directory:\n{final_theme_dir}\n\nTarball:\n{tar_path}\n\nMapping JSON:\n{mapping_path}",
            )

        def on_error(result_token: TaskToken, exc: BaseException) -> None:
            if not self.request_tracker.is_current(result_token):
                return
            self._set_build_busy(False, "Build failed")
            messagebox.showerror("Build failed", str(exc))

        self.task_runner.submit(token, work, on_success=on_success, on_error=on_error)


def main(argv: list[str] | None = None) -> None:
    configure_project_tmp()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load", type=Path, help="preload a mapping JSON into the GUI")
    parser.add_argument("--palette", type=Path, help="load a GUI palette JSON; defaults to gui-palette.json when present")
    parser.add_argument("--auto-close-ms", type=int, default=0, help="close automatically after N milliseconds")
    args = parser.parse_args(argv)

    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    app = MappingApp(root, palette_path=args.palette)
    app.preset_description_var.set(describe_build_preset(app.build_preset_var.get()))

    if args.load:
        payload = load_mapping_payload(args.load)
        app.apply_payload(payload)
        app.current_mapping_path = args.load.resolve()

    if args.auto_close_ms > 0:
        root.after(args.auto_close_ms, root.destroy)

    root.mainloop()


if __name__ == "__main__":
    main()
