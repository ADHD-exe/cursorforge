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
import tkinter as tk
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
from prepare_windows_cursor_set import analyze_cursor_pack, prepare_windows_cursor_set
from slot_definitions import (
    BUILD_PRESETS,
    BUILD_PRESET_BY_KEY,
    DEFAULT_CURSOR_SIZES,
    DEFAULT_SCALE_FILTER,
    SCALE_FILTER_CHOICES,
    SLOT_BY_KEY,
    SLOT_DEFS,
    describe_build_preset,
    format_cursor_sizes,
    normalize_cursor_sizes,
    score_slot_match,
)
from windows_cursor_tool import sanitize_path_component


REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_WORK_ROOT = REPO_ROOT / "gui-builds"
DEFAULT_GUI_PALETTE_PATH = REPO_ROOT / "gui-palette.json"
CARD_PREVIEW_SIZE = 48
PLAYER_PREVIEW_SIZE = 132
CANDIDATE_PREVIEW_SIZE = 96
CANVAS_SLOT_GLYPH_SIZE = 28
LOW_CONFIDENCE_LABELS = {"likely blurry", "redraw recommended"}
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
) -> dict:
    sizes = normalize_cursor_sizes(target_sizes, fallback=DEFAULT_CURSOR_SIZES)
    return {
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
    digest = hashlib.sha256(
        f"{source_png.resolve()}::{source_png.stat().st_mtime_ns}::{box_size}".encode("utf-8")
    ).hexdigest()[:12]
    preview_png = preview_root / f"{sanitize_path_component(source_png.stem)}_{digest}_{box_size}.png"
    if preview_png.exists():
        return preview_png

    image_tool = find_image_tool()
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
            str(preview_png),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
        "size_summary": size_summary,
        "contains_non_square": any(width != height for width, height in size_pairs),
        "hotspot_summary": ", ".join(f"{x},{y}" for x, y in sorted(hotspot_pairs)[:3]) or "--",
        "warnings": [],
    }


def preview_entry_sort_key(entry: dict) -> tuple[int, int, int, int]:
    colors = int(entry.get("colors", 0) or 0)
    colors = 1_000_000 if colors == 0 else colors
    return (
        max(int(entry["width"]), int(entry["height"])),
        int(entry["width"]) * int(entry["height"]),
        int(entry.get("image_size", 0) or 0),
        colors,
    )


def frames_from_source_metadata(metadata: dict) -> list[dict]:
    frames = []
    for frame_index, frame in enumerate(metadata.get("frames", [])):
        entries = frame.get("entries", [])
        if not entries and "png" in frame:
            entries = [frame]
        if not entries:
            continue
        entry = max(entries, key=preview_entry_sort_key)
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


def score_quality(summary: dict, target_sizes: list[int]) -> tuple[str, str]:
    if summary.get("error"):
        return "redraw recommended", "The source metadata could not be inspected cleanly."
    if not target_sizes:
        return "acceptable", "No target sizes are configured yet."
    max_target = max(target_sizes)
    max_native = int(summary.get("largest_native_size", 0))
    if max_native >= max_target:
        return "excellent", "The source already reaches the largest requested output size."
    if max_native >= int(max_target * 0.75):
        return "good", "Only moderate upscale is required for the largest output size."
    if max_native >= int(max_target * 0.5):
        return "acceptable", "The source can scale up, but larger sizes may soften."
    if max_native >= int(max_target * 0.33):
        return "likely blurry", "Large output sizes are substantially above the native source detail."
    return "redraw recommended", "The selected source is far below the requested build sizes."


def infer_slot_warnings(slot_key: str, summary: dict, target_sizes: list[int], pack_analysis: dict | None) -> list[str]:
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

    stem = Path(summary["filename"]).stem
    if slot_key == "default_pointer":
        progress_score = score_slot_match(stem, SLOT_BY_KEY["progress"])
        default_score = score_slot_match(stem, SLOT_BY_KEY["default_pointer"])
        if progress_score >= default_score and progress_score > 0:
            warnings.append("this default pointer name also looks like a progress/appstart cursor")

    if pack_analysis is not None:
        ambiguous = pack_analysis.get("ambiguous_candidates", {}).get(slot_key, [])
        if ambiguous:
            selected_path = summary["path"]
            top_paths = {candidate["path"] for candidate in ambiguous}
            if selected_path in top_paths:
                warnings.append("slot choice is ambiguous based on filename heuristics alone")
    return list(dict.fromkeys(warnings))


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

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

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

        self.speed_var = tk.StringVar(value="1.0x")
        self.speed_combo = ttk.Combobox(
            controls,
            textvariable=self.speed_var,
            values=("0.5x", "1.0x", "1.5x", "2.0x"),
            state="readonly",
            width=6,
        )
        self.speed_combo.grid(row=0, column=3, sticky="e")
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

        self.clear("No preview loaded")

    def destroy(self) -> None:
        self._cancel_after()
        super().destroy()

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

    def set_frames(self, frames: list[dict], images: list[tk.PhotoImage], summary: str, frame_info: str) -> None:
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
        self._draw_current_frame()
        if self.running:
            self._schedule_next_frame()

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

    def update_card(self, path: str | None, summary: dict | None, quality: dict | None, selected: bool) -> None:
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

        if not path or summary is None:
            self.file_label.configure(text="Unassigned")
            self.badge_label.configure(text="Choose a source or use Auto-Fill")
            self.path_label.configure(text="")
            self.warning_label.configure(text="")
            return

        quality_label = quality["label"] if quality else "--"
        self.file_label.configure(text=f"{Path(path).name}   [{quality_label}]")
        self.badge_label.configure(text=badges_for_summary(summary))
        self.path_label.configure(text=compact_path(path))
        warnings = quality["warnings"] if quality else []
        self.warning_label.configure(text=warnings[0] if warnings else "")


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
        self.root.title("win2kde Cursor Converter")
        self.root.geometry("1560x1040")
        self.palette, self.palette_path, self.palette_name = load_gui_palette(palette_path)
        self.style = ttk.Style(root)

        self.current_mapping_path: Path | None = None
        self.last_tar_path: Path | None = None
        self.last_theme_dir: Path | None = None
        self.pack_analysis: dict | None = None
        self.selected_slot_key = SLOT_DEFS[0]["key"]
        self.selected_candidate_path: str | None = None

        self.preview_photo_cache: dict[str, tk.PhotoImage] = {}
        self.source_metadata_cache: dict[str, dict] = {}
        self.output_preview_cache: dict[str, dict] = {}
        self.summary_cache: dict[str, dict] = {}
        self.slot_paths = {slot["key"]: "" for slot in SLOT_DEFS}

        self.source_dir_var = tk.StringVar()
        self.work_root_var = tk.StringVar(value=str(DEFAULT_WORK_ROOT))
        self.theme_name_var = tk.StringVar(value="Custom-cursor")
        self.scale_filter_var = tk.StringVar(value=DEFAULT_SCALE_FILTER)
        self.summary_var = tk.StringVar(value="No slots assigned yet")
        self.status_var = tk.StringVar(value="Ready")
        self.target_sizes = list(DEFAULT_CURSOR_SIZES)
        self.target_sizes_var = tk.StringVar(value=format_cursor_sizes(self.target_sizes))
        self.build_preset_var = tk.StringVar(value="hidpi-kde")
        self.preview_nominal_size_var = tk.StringVar(value=str(self._default_preview_size(self.target_sizes)))
        self.preset_description_var = tk.StringVar(value=describe_build_preset(self.build_preset_var.get()))
        self.overall_quality_var = tk.StringVar(value="Overall quality forecast: --")
        self.last_output_var = tk.StringVar(value="No build output yet")
        self.palette_name_var = tk.StringVar(
            value=f"GUI palette: {self.palette_name} ({self.palette_path.name})" if self.palette_path else "GUI palette: built-in"
        )

        self._apply_palette()
        self._build_ui()
        self._refresh_all_views()

        self.target_sizes_var.trace_add("write", lambda *_: self.on_build_settings_changed())
        self.scale_filter_var.trace_add("write", lambda *_: self.on_build_settings_changed())
        self.preview_nominal_size_var.trace_add("write", lambda *_: self._refresh_selected_slot_detail())
        self.build_preset_var.trace_add("write", lambda *_: self._update_preset_description())

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
            text="Windows cursor pack -> Linux Xcursor theme",
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

    def _build_analysis_tab(self) -> None:
        self.analysis_tab.columnconfigure(0, weight=1)
        self.analysis_tab.rowconfigure(2, weight=1)

        controls = ttk.LabelFrame(self.analysis_tab, text="Stage 1: Analyze The Source Pack", padding=10)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Windows cursor folder").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(controls, textvariable=self.source_dir_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(controls, text="Browse", command=self.choose_source_dir).grid(row=0, column=2, padx=(8, 0), pady=3)
        ttk.Button(controls, text="Analyze Pack", command=self.analyze_pack).grid(row=0, column=3, padx=(8, 0), pady=3)
        ttk.Button(controls, text="Auto-Fill From Pack", command=self.auto_prepare).grid(row=0, column=4, padx=(8, 0), pady=3)

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

        overview = ttk.Frame(self.analysis_tab)
        overview.grid(row=1, column=0, sticky="ew", pady=(10, 10))
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
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.analysis_detail_text = tk.Text(right, height=8, wrap="word")
        self.analysis_detail_text.grid(row=0, column=0, sticky="nsew")
        self._theme_text_widget(self.analysis_detail_text)
        set_readonly_text(self.analysis_detail_text, "Analyze a source pack to see diagnostics.")

        assets_frame = ttk.LabelFrame(self.analysis_tab, text="Detected Source Assets", padding=10)
        assets_frame.grid(row=2, column=0, sticky="nsew")
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
        left.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(4, weight=1)
        paned.add(left, weight=3)
        paned.add(right, weight=5)

        ttk.Label(
            left,
            text="Stage 2: Review the guessed slot assignments visually. Paths stay available, but the primary signals are previews, animation badges, native sizes, and quality warnings.",
            wraplength=500,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        slot_frame = ttk.Frame(left)
        slot_frame.grid(row=1, column=0, sticky="nsew")
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

        candidate_frame = ttk.LabelFrame(right, text="Ranked Candidate Browser", padding=10)
        candidate_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        candidate_frame.columnconfigure(0, weight=1)
        candidate_frame.rowconfigure(0, weight=1)

        self.candidate_tree = ttk.Treeview(
            candidate_frame,
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
        self.candidate_tree.column("#0", width=240, anchor="w")
        self.candidate_tree.column("type", width=70, anchor="center")
        self.candidate_tree.column("sizes", width=110, anchor="center")
        self.candidate_tree.column("score", width=70, anchor="center")
        self.candidate_tree.column("reason", width=330, anchor="w")
        self.candidate_tree.grid(row=0, column=0, sticky="nsew")
        candidate_scroll = ttk.Scrollbar(candidate_frame, orient="vertical", command=self.candidate_tree.yview)
        candidate_scroll.grid(row=0, column=1, sticky="ns")
        self.candidate_tree.configure(yscrollcommand=candidate_scroll.set)
        self.candidate_tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_candidate_detail())

        candidate_detail = ttk.Frame(candidate_frame)
        candidate_detail.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        candidate_detail.columnconfigure(1, weight=1)
        self.candidate_preview_panel = AnimationPreviewPanel(candidate_detail, "Candidate Preview", CANDIDATE_PREVIEW_SIZE, self.palette)
        self.candidate_preview_panel.grid(row=0, column=0, rowspan=2, sticky="nw")
        self.candidate_summary_var = tk.StringVar(value="Select a candidate to inspect it.")
        self.candidate_warning_var = tk.StringVar(value="")
        ttk.Label(candidate_detail, textvariable=self.candidate_summary_var, wraplength=620, justify="left").grid(
            row=0,
            column=1,
            sticky="nw",
            padx=(10, 0),
        )
        ttk.Label(
            candidate_detail,
            textvariable=self.candidate_warning_var,
            wraplength=620,
            justify="left",
            style="Warning.TLabel",
        ).grid(row=1, column=1, sticky="nw", padx=(10, 0), pady=(6, 0))

    def _build_build_tab(self) -> None:
        self.build_tab.columnconfigure(0, weight=1)
        self.build_tab.columnconfigure(1, weight=1)
        self.build_tab.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(self.build_tab, text="Stage 3: Build Settings And Export", padding=10)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)
        top.columnconfigure(4, weight=1)

        ttk.Label(top, text="Build preset").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.preset_combo = ttk.Combobox(
            top,
            textvariable=self.build_preset_var,
            values=[preset["key"] for preset in BUILD_PRESETS],
            state="readonly",
            width=18,
        )
        self.preset_combo.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Button(top, text="Apply Preset", command=self.apply_selected_preset).grid(row=0, column=2, padx=(8, 0), pady=3)
        ttk.Label(top, textvariable=self.preset_description_var, wraplength=760, justify="left", style="Muted.TLabel").grid(
            row=0,
            column=3,
            columnspan=2,
            sticky="w",
            padx=(12, 0),
            pady=3,
        )

        ttk.Label(top, text="Theme name").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.theme_name_var).grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(top, text="Output sizes").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.target_sizes_var).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(top, text="Scale filter").grid(row=2, column=2, sticky="e", padx=(10, 8), pady=3)
        ttk.Combobox(top, textvariable=self.scale_filter_var, values=SCALE_FILTER_CHOICES, state="readonly", width=12).grid(
            row=2,
            column=3,
            sticky="w",
            pady=3,
        )
        ttk.Label(top, text="Predicted preview size").grid(row=2, column=4, sticky="e", padx=(10, 8), pady=3)
        self.build_preview_size_combo = ttk.Combobox(
            top,
            textvariable=self.preview_nominal_size_var,
            state="readonly",
            width=10,
        )
        self.build_preview_size_combo.grid(row=2, column=5, sticky="w", pady=3)

        ttk.Label(top, text="Output root").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.work_root_var).grid(row=3, column=1, columnspan=4, sticky="ew", pady=3)
        ttk.Button(top, text="Browse", command=self.choose_work_root).grid(row=3, column=5, padx=(8, 0), pady=3)

        quality_frame = ttk.LabelFrame(self.build_tab, text="Quality Forecast And Validation", padding=10)
        quality_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(10, 0))
        quality_frame.columnconfigure(0, weight=1)
        quality_frame.rowconfigure(1, weight=1)
        ttk.Label(quality_frame, textvariable=self.overall_quality_var, font=("", 11, "bold"), style="Heading.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.build_warning_text = tk.Text(quality_frame, wrap="word")
        self.build_warning_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self._theme_text_widget(self.build_warning_text)
        set_readonly_text(self.build_warning_text, "Warnings and build guidance will appear here.")

        export_frame = ttk.LabelFrame(self.build_tab, text="Mapping, Export, And Final Output", padding=10)
        export_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(10, 0))
        export_frame.columnconfigure(0, weight=1)
        export_frame.rowconfigure(1, weight=1)

        button_row = ttk.Frame(export_frame)
        button_row.grid(row=0, column=0, sticky="ew")
        ttk.Button(button_row, text="Load JSON", command=self.load_json).grid(row=0, column=0)
        ttk.Button(button_row, text="Save JSON", command=self.save_json).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(button_row, text="Save Markdown", command=self.save_markdown).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(button_row, text="Build + Package", command=self.build_and_package).grid(row=0, column=3, padx=(12, 0))

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
        preset_key = self.build_preset_var.get().strip()
        if preset_key in BUILD_PRESET_BY_KEY:
            self.preset_description_var.set(describe_build_preset(preset_key))

    def _sync_preset_from_settings(self) -> None:
        sizes, _error = self.try_target_sizes()
        current_filter = self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER
        for preset in BUILD_PRESETS:
            if preset["target_sizes"] == sizes and preset["scale_filter"] == current_filter:
                if self.build_preset_var.get() != preset["key"]:
                    self.build_preset_var.set(preset["key"])
                return

    def set_status(self, message: str) -> None:
        self.status_var.set(message)
        self.root.update_idletasks()

    def current_preview_root(self) -> Path:
        work_root = Path(self.work_root_var.get().strip() or DEFAULT_WORK_ROOT).expanduser()
        return work_root / "_preview-cache"

    def preview_photo(self, png_path: Path, box_size: int) -> tk.PhotoImage:
        resolved = png_path.expanduser().resolve()
        key = f"{resolved}::{resolved.stat().st_mtime_ns}::{box_size}"
        if key in self.preview_photo_cache:
            return self.preview_photo_cache[key]
        preview_png = render_preview_thumbnail(resolved, self.current_preview_root() / "_thumbs", box_size)
        image = tk.PhotoImage(file=str(preview_png))
        self.preview_photo_cache[key] = image
        return image

    def _source_metadata_cache_key(self, source_path: Path) -> str:
        resolved = source_path.expanduser().resolve()
        preview_root = self.current_preview_root()
        return f"{resolved}::{resolved.stat().st_mtime_ns}::{preview_root}"

    def ensure_source_metadata(self, source_path: Path) -> dict:
        cache_key = self._source_metadata_cache_key(source_path)
        if cache_key in self.source_metadata_cache:
            return self.source_metadata_cache[cache_key]
        metadata = load_source_metadata(source_path.expanduser().resolve(), self.current_preview_root() / "_source")
        self.source_metadata_cache[cache_key] = metadata
        return metadata

    def ensure_summary(self, source_path: Path) -> dict:
        resolved = source_path.expanduser().resolve()
        cache_key = self._source_metadata_cache_key(resolved)
        if cache_key in self.summary_cache:
            return self.summary_cache[cache_key]
        metadata = self.ensure_source_metadata(resolved)
        summary = summarize_metadata(resolved, metadata)
        if self.pack_analysis is not None:
            for asset in self.pack_analysis.get("asset_summaries", []):
                if asset["path"] == str(resolved):
                    summary["warnings"] = list(dict.fromkeys(asset.get("warnings", [])))
                    summary["relative_path"] = asset.get("relative_path", summary["relative_path"])
                    summary["source_type"] = asset.get("source_type", summary["source_type"])
                    summary["largest_native_size"] = asset.get("largest_native_size", summary["largest_native_size"])
                    summary["size_summary"] = asset.get("size_summary", summary["size_summary"])
                    summary["contains_non_square"] = asset.get("contains_non_square", summary["contains_non_square"])
                    break
        self.summary_cache[cache_key] = summary
        return summary

    def ensure_output_preview(self, source_path: Path, preview_nominal_size: int | None = None) -> dict:
        sizes = self.try_target_sizes()[0]
        filter_name = self.scale_filter_var.get().strip() or DEFAULT_SCALE_FILTER
        resolved = source_path.expanduser().resolve()
        cache_key = (
            f"{resolved}::{resolved.stat().st_mtime_ns}::{format_cursor_sizes(sizes)}::"
            f"{filter_name}::{preview_nominal_size or self.preview_nominal_size_var.get()}::{self.current_preview_root()}"
        )
        if cache_key in self.output_preview_cache:
            return self.output_preview_cache[cache_key]
        preview = prepare_output_preview_metadata(
            resolved,
            self.current_preview_root() / "_output",
            sizes,
            scale_filter=filter_name,
            preview_nominal_size=preview_nominal_size,
        )
        self.output_preview_cache[cache_key] = preview
        return preview

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

    def _default_preview_size(self, sizes: list[int]) -> int:
        return min(sizes, key=lambda size: (abs(size - 32), size))

    def _update_preview_size_choices(self) -> None:
        sizes, _error = self.try_target_sizes()
        values = [str(size) for size in sizes]
        self.preview_nominal_size_combo.configure(values=values)
        self.build_preview_size_combo.configure(values=values)
        current = self.preview_nominal_size_var.get().strip()
        if current not in values:
            self.preview_nominal_size_var.set(str(self._default_preview_size(sizes)))

    def choose_source_dir(self) -> None:
        selected = filedialog.askdirectory(title="Choose Windows cursor folder")
        if not selected:
            return
        self.source_dir_var.set(selected)
        if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
            self.theme_name_var.set(slugify_name(Path(selected).name))
        self.set_status(f"Selected source pack: {selected}")

    def choose_work_root(self) -> None:
        selected = filedialog.askdirectory(title="Choose output root")
        if not selected:
            return
        self.work_root_var.set(selected)
        self.clear_preview_caches()
        self.set_status(f"Selected output root: {selected}")
        self._refresh_all_views()

    def clear_preview_caches(self) -> None:
        self.preview_photo_cache.clear()
        self.source_metadata_cache.clear()
        self.output_preview_cache.clear()
        self.summary_cache.clear()

    def analyze_pack(self) -> None:
        source_dir_text = self.source_dir_var.get().strip()
        if not source_dir_text:
            messagebox.showerror("Missing source folder", "Choose a Windows cursor folder first.")
            return
        source_dir = Path(source_dir_text).expanduser()
        if not source_dir.exists():
            messagebox.showerror("Missing source folder", f"Folder does not exist:\n{source_dir}")
            return
        try:
            self.set_status(f"Analyzing {source_dir}")
            self.pack_analysis = analyze_cursor_pack(source_dir)
            self.set_status(f"Analyzed {self.pack_analysis['counts']['total']} source assets")
            self._render_pack_analysis()
            self._refresh_all_views()
        except Exception as exc:  # noqa: BLE001
            self.set_status("Pack analysis failed")
            messagebox.showerror("Pack analysis failed", str(exc))

    def _render_pack_analysis(self) -> None:
        analysis = self.pack_analysis
        if analysis is None:
            self.analysis_counts_var.set("Files: --")
            self.analysis_inf_var.set("INF: --")
            self.analysis_hidpi_var.set("HiDPI: --")
            self.analysis_sizes_var.set("Largest native sizes: --")
            self.analysis_animated_var.set("Animated sources: --")
            set_readonly_text(self.analysis_detail_text, "Analyze a source pack to see diagnostics.")
            for item in self.analysis_asset_tree.get_children():
                self.analysis_asset_tree.delete(item)
            return

        counts = analysis["counts"]
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

        warning_lines = []
        if analysis.get("warnings"):
            warning_lines.append("Warnings:")
            warning_lines.extend(f"- {warning}" for warning in analysis["warnings"])
            warning_lines.append("")
        if analysis.get("ambiguous_candidates"):
            warning_lines.append("Ambiguous slots:")
            for slot_key, candidates in sorted(analysis["ambiguous_candidates"].items()):
                labels = ", ".join(Path(candidate["path"]).name for candidate in candidates[:3])
                warning_lines.append(f"- {SLOT_BY_KEY[slot_key]['label']}: {labels}")
            warning_lines.append("")
        duplicate_artifacts = analysis.get("duplicate_artifacts", [])
        if duplicate_artifacts:
            warning_lines.append("Duplicate / temp-style artifacts:")
            for artifact in duplicate_artifacts[:8]:
                warning_lines.append(f"- {artifact['relative_path']} ({artifact['reason']})")
        set_readonly_text(self.analysis_detail_text, "\n".join(warning_lines) or "No pack-level warnings.")

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
                text=asset["filename"],
                values=(
                    asset.get("source_type", "--"),
                    "yes" if asset.get("is_animated") else "no",
                    asset.get("size_summary", "--"),
                    flags,
                    location,
                ),
            )

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
        try:
            self.set_status(f"Preparing {source_dir.name}")
            summary = prepare_windows_cursor_set(source_dir, prep_dir)
            self.pack_analysis = summary.get("analysis")
            self._render_pack_analysis()
            mapping_path = Path(summary["mapping_json"]).resolve()
            payload = load_mapping_payload(mapping_path)
            self.apply_payload(payload)
            self.current_mapping_path = mapping_path
            if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
                self.theme_name_var.set(slugify_name(source_dir.name))
            self.notebook.select(self.review_tab)
            self.set_status(f"Auto-filled {summary['selected_slot_count']} source slots")
            messagebox.showinfo(
                "Auto-Fill Complete",
                f"Prepared {summary['selected_slot_count']} slots.\n\nMapping JSON:\n{mapping_path}",
            )
        except Exception as exc:  # noqa: BLE001
            self.set_status("Auto-fill failed")
            messagebox.showerror("Auto-fill failed", str(exc))

    def apply_payload(self, payload: dict) -> None:
        build_options = payload.get("build_options", {})
        scale_filter = build_options.get("scale_filter")
        if scale_filter in SCALE_FILTER_CHOICES:
            self.scale_filter_var.set(scale_filter)
        target_sizes = build_options.get("target_sizes")
        self.target_sizes = normalize_cursor_sizes(target_sizes, fallback=DEFAULT_CURSOR_SIZES)
        self.target_sizes_var.set(format_cursor_sizes(self.target_sizes))
        self._update_preview_size_choices()
        self._sync_preset_from_settings()

        selected = payload.get("selected_slots", {})
        role_map = payload.get("resolved_role_map", {})
        self.slot_paths = {slot["key"]: "" for slot in SLOT_DEFS}

        for slot_key, item in selected.items():
            if slot_key in self.slot_paths:
                self.slot_paths[slot_key] = item.get("path", "")

        if not selected and role_map:
            for slot in SLOT_DEFS:
                for role in slot["roles"]:
                    if role in role_map:
                        self.slot_paths[slot["key"]] = role_map[role]
                        break

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
        payload = build_payload(selected_slots, resolved, target_sizes, self.scale_filter_var.get())
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
        self.selected_slot_key = slot_key
        self.selected_candidate_path = None
        self._refresh_slot_cards()
        self._refresh_selected_slot_detail()

    def focus_slot_candidates(self, slot_key: str) -> None:
        self.select_slot(slot_key)
        self.notebook.select(self.review_tab)

    def browse_slot(self, slot_key: str) -> None:
        slot = SLOT_BY_KEY[slot_key]
        patterns = " ".join(f"*{ext}" for ext in slot["allowed_extensions"])
        label = f"{slot['label']} files"
        allowed = [(label, patterns), ("All supported", "*.ani *.cur *.png *.json")]
        file_path = filedialog.askopenfilename(title=f"Select source for {slot['label']}", filetypes=allowed)
        if not file_path:
            return
        self.slot_paths[slot_key] = str(Path(file_path).expanduser().resolve())
        self.clear_preview_caches()
        self.select_slot(slot_key)
        self.set_status(f"Assigned {slot['label']} -> {file_path}")

    def clear_slot(self, slot_key: str) -> None:
        self.slot_paths[slot_key] = ""
        self.clear_preview_caches()
        self.select_slot(slot_key)
        self.set_status(f"Cleared {SLOT_BY_KEY[slot_key]['label']}")

    def apply_selected_candidate(self) -> None:
        if not self.selected_candidate_path:
            messagebox.showerror("No candidate selected", "Select a candidate first.")
            return
        self.slot_paths[self.selected_slot_key] = self.selected_candidate_path
        self.clear_preview_caches()
        self.select_slot(self.selected_slot_key)
        self.set_status(
            f"Assigned {SLOT_BY_KEY[self.selected_slot_key]['label']} -> {Path(self.selected_candidate_path).name}"
        )

    def _slot_quality(self, slot_key: str, path: str) -> dict | None:
        if not path:
            return None
        summary = self.ensure_summary(Path(path))
        target_sizes = self.try_target_sizes()[0]
        label, reason = score_quality(summary, target_sizes)
        warnings = infer_slot_warnings(slot_key, summary, target_sizes, self.pack_analysis)
        return {"label": label, "reason": reason, "warnings": warnings}

    def _refresh_slot_cards(self) -> None:
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            summary = None
            quality = None
            card = self.slot_cards[slot["key"]]
            card.preview_image = None
            if path:
                try:
                    summary = self.ensure_summary(Path(path))
                    source_frames = frames_from_source_metadata(self.ensure_source_metadata(Path(path)))
                    if source_frames:
                        card.preview_image = self.preview_photo(Path(source_frames[0]["png"]), CARD_PREVIEW_SIZE)
                    quality = self._slot_quality(slot["key"], path)
                except Exception as exc:  # noqa: BLE001
                    summary = {"filename": Path(path).name, "warnings": [str(exc)], "path": path}
                    quality = {"label": "redraw recommended", "reason": str(exc), "warnings": [str(exc)]}
            card.update_card(path if path else None, summary, quality, slot["key"] == self.selected_slot_key)

    def _selected_slot_path(self) -> str:
        return self.slot_paths.get(self.selected_slot_key, "").strip()

    def _refresh_selected_slot_detail(self) -> None:
        slot = SLOT_BY_KEY[self.selected_slot_key]
        path = self._selected_slot_path()
        self.selected_slot_title_var.set(slot["label"])
        self.selected_candidate_path = None

        if not path:
            self.selected_slot_meta_var.set("No source selected yet.")
            self.selected_slot_path_var.set("Use Auto-Fill, Browse Source, or choose a ranked candidate.")
            set_readonly_text(self.slot_warning_text, "This slot is currently unassigned.")
            self.source_preview_panel.clear("No source selected")
            self.output_preview_panel.clear("No source selected")
            self._populate_candidate_tree()
            self._refresh_candidate_detail()
            return

        try:
            source_path = Path(path)
            summary = self.ensure_summary(source_path)
            quality = self._slot_quality(self.selected_slot_key, path)
            self.selected_slot_meta_var.set(
                f"{badges_for_summary(summary)} | Quality forecast: {quality['label']} | {quality['reason']}"
            )
            self.selected_slot_path_var.set(compact_path(path, max_len=130))
            warning_lines = quality["warnings"] or ["No immediate warnings."]
            if summary.get("hotspot_summary"):
                warning_lines.append(f"Hotspot summary: {summary['hotspot_summary']}")
            set_readonly_text(self.slot_warning_text, "\n".join(f"- {line}" for line in warning_lines))
            self._load_source_preview(source_path)
            self._load_output_preview(source_path)
        except Exception as exc:  # noqa: BLE001
            self.selected_slot_meta_var.set("Unable to inspect the selected source.")
            self.selected_slot_path_var.set(compact_path(path, max_len=130))
            set_readonly_text(self.slot_warning_text, f"- {exc}")
            self.source_preview_panel.clear(str(exc))
            self.output_preview_panel.clear(str(exc))

        self._populate_candidate_tree()
        self._refresh_candidate_detail()

    def _load_source_preview(self, source_path: Path) -> None:
        metadata = self.ensure_source_metadata(source_path)
        frames = frames_from_source_metadata(metadata)
        if not frames:
            self.source_preview_panel.clear("No extracted frames available")
            return
        images = [self.preview_photo(Path(frame["png"]), PLAYER_PREVIEW_SIZE) for frame in frames]
        summary = (
            f"{len(frames)} frame(s) | {format_duration_ms(sum(frame['delay_ms'] for frame in frames))} total | "
            f"using representative native entries"
        )
        frame_info = f"Actual source timing preserved. First frame nominal size: {frames[0]['nominal_size']}px."
        self.source_preview_panel.set_frames(frames, images, summary, frame_info)

    def _load_output_preview(self, source_path: Path) -> None:
        preview_size = int(self.preview_nominal_size_var.get())
        preview = self.ensure_output_preview(source_path, preview_size)
        frames = preview["frames"]
        images = [self.preview_photo(Path(frame["png"]), PLAYER_PREVIEW_SIZE) for frame in frames]
        total_ms = sum(int(frame.get("delay_ms", 50)) for frame in frames)
        if frames:
            first = frames[0]
            frame_info = (
                f"Nominal size {preview['preview_nominal_size']}px | emitted PNG {first['width']}x{first['height']} | "
                f"filter {preview['scale_filter']}"
            )
        else:
            frame_info = "No predicted frames available"
        summary = f"{len(frames)} frame(s) | {format_duration_ms(total_ms)} total | built path preview"
        self.output_preview_panel.set_frames(frames, images, summary, frame_info)

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
                    candidate["reason"],
                ),
            )
        current_path = self._selected_slot_path()
        if current_path and current_path in self.candidate_tree.get_children():
            self.candidate_tree.selection_set(current_path)
        else:
            first = self.candidate_tree.get_children()[0]
            if first != "__none__":
                self.candidate_tree.selection_set(first)

    def _refresh_candidate_detail(self) -> None:
        selection = self.candidate_tree.selection()
        if not selection or selection[0] == "__none__":
            self.selected_candidate_path = None
            self.candidate_summary_var.set("Select a candidate to inspect it.")
            self.candidate_warning_var.set("")
            self.candidate_preview_panel.clear("No candidate selected")
            return
        candidate_path = selection[0]
        self.selected_candidate_path = candidate_path
        try:
            summary = self.ensure_summary(Path(candidate_path))
            quality = self._slot_quality(self.selected_slot_key, candidate_path)
            self.candidate_summary_var.set(
                f"{Path(candidate_path).name} | {badges_for_summary(summary)} | quality {quality['label']}"
            )
            self.candidate_warning_var.set("; ".join(quality["warnings"][:3]) or "No immediate candidate warnings.")
            metadata = self.ensure_source_metadata(Path(candidate_path))
            frames = frames_from_source_metadata(metadata)
            images = [self.preview_photo(Path(frame["png"]), CANDIDATE_PREVIEW_SIZE) for frame in frames]
            self.candidate_preview_panel.set_frames(
                frames,
                images,
                f"{len(frames)} frame(s) | {format_duration_ms(sum(frame['delay_ms'] for frame in frames))}",
                f"Candidate path: {compact_path(candidate_path, max_len=82)}",
            )
        except Exception as exc:  # noqa: BLE001
            self.candidate_summary_var.set(Path(candidate_path).name)
            self.candidate_warning_var.set(str(exc))
            self.candidate_preview_panel.clear(str(exc))

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

        quality_labels = []
        warning_lines = []
        for slot in SLOT_DEFS:
            path = self.slot_paths.get(slot["key"], "").strip()
            if not path:
                continue
            quality = self._slot_quality(slot["key"], path)
            if quality is None:
                continue
            quality_labels.append(quality["label"])
            for warning in quality["warnings"][:2]:
                warning_lines.append(f"- {slot['label']}: {warning}")

        if mapping_error:
            self.overall_quality_var.set("Overall quality forecast: configuration error")
            warning_lines.insert(0, f"- Mapping error: {mapping_error}")
        elif not quality_labels:
            self.overall_quality_var.set("Overall quality forecast: no source slots assigned")
        else:
            avg_score = sum(quality_to_score(label) for label in quality_labels) / len(quality_labels)
            if avg_score >= 3.5:
                overall = "excellent"
            elif avg_score >= 2.6:
                overall = "good"
            elif avg_score >= 1.8:
                overall = "acceptable"
            elif avg_score >= 1.0:
                overall = "likely blurry"
            else:
                overall = "redraw recommended"
            self.overall_quality_var.set(
                f"Overall quality forecast: {overall} | {len(selected_slots)} slot(s) assigned | {len(resolved)} Linux roles resolved"
            )

        if size_error:
            warning_lines.insert(0, f"- Output sizes: {size_error}")
        if self.pack_analysis is not None:
            for warning in self.pack_analysis.get("warnings", []):
                warning_lines.append(f"- Pack: {warning}")

        set_readonly_text(self.build_warning_text, "\n".join(dict.fromkeys(warning_lines)) or "No major warnings.")

        build_summary_lines = [
            f"Theme name: {self.theme_name_var.get().strip() or 'Custom-cursor'}",
            f"Output root: {self.work_root_var.get().strip() or DEFAULT_WORK_ROOT}",
            f"Target sizes: {format_cursor_sizes(sizes)}",
            f"Scale filter: {self.scale_filter_var.get()}",
            "",
            "Selected slots:",
        ]
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
        self._sync_preset_from_settings()
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

    def on_build_settings_changed(self) -> None:
        self._update_preview_size_choices()
        self.output_preview_cache.clear()
        self._refresh_all_views()

    def apply_selected_preset(self) -> None:
        preset = BUILD_PRESET_BY_KEY[self.build_preset_var.get()]
        self.target_sizes_var.set(format_cursor_sizes(preset["target_sizes"]))
        self.scale_filter_var.set(preset["scale_filter"])
        self.preset_description_var.set(describe_build_preset(preset["key"]))
        self.preview_nominal_size_var.set(str(self._default_preview_size(preset["target_sizes"])))
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

        payload = build_payload(selected_slots, resolved, target_sizes, self.scale_filter_var.get())
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

        try:
            self.set_status(f"Saving mapping for {safe_theme_name}")
            if build_root.exists():
                shutil.rmtree(build_root)
            build_root.mkdir(parents=True, exist_ok=True)
            mapping_store_dir.mkdir(parents=True, exist_ok=True)
            mapping_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            self.current_mapping_path = mapping_path

            self.set_status(f"Building Linux cursor theme: {safe_theme_name}")
            manifest = build_theme_from_mapping(
                mapping_path,
                build_root,
                safe_theme_name,
                target_sizes,
                scale_filter=self.scale_filter_var.get(),
            )
            built_theme_dir = Path(manifest["theme_dir"]).resolve()

            self.set_status(f"Copying final theme to {final_theme_dir}")
            if final_theme_dir.exists():
                shutil.rmtree(final_theme_dir)
            shutil.copytree(built_theme_dir, final_theme_dir)

            self.set_status(f"Packaging tarball: {tar_path.name}")
            if tar_path.exists():
                tar_path.unlink()
            package_theme(final_theme_dir, tar_path)

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
        except Exception as exc:  # noqa: BLE001
            self.set_status("Build failed")
            messagebox.showerror("Build failed", str(exc))


def main(argv: list[str] | None = None) -> None:
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
