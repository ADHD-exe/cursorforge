#!/usr/bin/env python3
"""Simple GUI for mapping a small source set onto a full Linux cursor role map."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tarfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_from_slot_mapping import build_theme_from_mapping
from prepare_windows_cursor_set import prepare_windows_cursor_set
from slot_definitions import (
    DEFAULT_CURSOR_SIZES,
    DEFAULT_SCALE_FILTER,
    SCALE_FILTER_CHOICES,
    SLOT_BY_KEY,
    SLOT_DEFS,
)


REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_WORK_ROOT = REPO_ROOT / "gui-builds"


def build_payload(
    selected_slots: dict,
    resolved: dict,
    target_sizes: list[int] | None = None,
    scale_filter: str = DEFAULT_SCALE_FILTER,
) -> dict:
    return {
        "mapping_format_version": 2,
        "build_options": {
            "target_sizes": list(target_sizes or DEFAULT_CURSOR_SIZES),
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


def draw_slot_glyph(canvas: tk.Canvas, slot_key: str):
    canvas.delete("all")
    color = "#2b2b2b"
    accent = "#0f7b6c"

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


class SlotRow:
    def __init__(self, master: tk.Widget, row_index: int, slot: dict, on_change):
        self.on_change = on_change
        self.slot = slot
        self.path_var = tk.StringVar()

        self.icon = tk.Canvas(master, width=28, height=28, highlightthickness=0, bg="white")
        self.icon.grid(row=row_index, column=0, padx=(0, 6), pady=3, sticky="w")
        draw_slot_glyph(self.icon, slot["key"])

        self.label = ttk.Label(master, text=slot["label"], width=22, anchor="w")
        self.label.grid(row=row_index, column=1, padx=(0, 6), pady=3, sticky="w")

        self.path_entry = ttk.Entry(master, textvariable=self.path_var, width=62)
        self.path_entry.grid(row=row_index, column=2, padx=(0, 6), pady=3, sticky="ew")
        self.path_var.trace_add("write", lambda *_: self.on_change())

        self.browse_button = ttk.Button(master, text="Browse", command=self.browse)
        self.browse_button.grid(row=row_index, column=3, padx=(0, 6), pady=3)

        self.clear_button = ttk.Button(master, text="Clear", command=self.clear)
        self.clear_button.grid(row=row_index, column=4, pady=3)

    def browse(self):
        patterns = " ".join(f"*{ext}" for ext in self.slot["allowed_extensions"])
        label = f"{self.slot['label']} files"
        allowed = [(label, patterns), ("All supported", "*.ani *.cur *.png *.json")]
        file_path = filedialog.askopenfilename(title="Select source cursor file", filetypes=allowed)
        if file_path:
            self.path_var.set(file_path)

    def clear(self):
        self.path_var.set("")

    def set_value(self, path: str):
        self.path_var.set(path)

    def get_selected_slot(self):
        return self.slot

    def get_path(self):
        return self.path_var.get().strip()


class MappingApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Cursor Source Slot Mapper")
        root.geometry("1220x980")

        self.current_mapping_path: Path | None = None
        self.last_tar_path: Path | None = None
        self.last_theme_dir: Path | None = None

        self.source_dir_var = tk.StringVar()
        self.work_root_var = tk.StringVar(value=str(DEFAULT_WORK_ROOT))
        self.theme_name_var = tk.StringVar(value="Custom-cursor")
        self.scale_filter_var = tk.StringVar(value=DEFAULT_SCALE_FILTER)
        self.summary_var = tk.StringVar(value="0 source slots selected")
        self.status_var = tk.StringVar(value="Ready")
        self.size_summary_var = tk.StringVar(value=", ".join(str(size) for size in DEFAULT_CURSOR_SIZES))

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=4)
        outer.rowconfigure(5, weight=1)

        title = ttk.Label(
            outer,
            text="Windows cursor pack -> Linux animated cursor theme",
            font=("", 12, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        info = ttk.Label(
            outer,
            text=(
                "Workflow: choose a Windows cursor folder, click Auto-Fill, adjust any of the 16 slots if needed, "
                "then click Build + Package. The builder keeps the original .cur/.ani sources until build time so it can "
                "choose the best native image per Linux cursor size instead of flattening early."
            ),
            wraplength=1150,
            justify="left",
        )
        info.grid(row=1, column=0, sticky="w", pady=(4, 10))

        workflow = ttk.LabelFrame(outer, text="Workflow", padding=10)
        workflow.grid(row=2, column=0, sticky="ew")
        workflow.columnconfigure(1, weight=1)

        steps = ttk.Label(
            workflow,
            text=(
                "1. Choose the Windows cursor folder.\n"
                "2. Click Auto-Fill From Pack.\n"
                "3. Fix any slot paths that look wrong.\n"
                "4. Confirm the scale filter and output root.\n"
                "5. Click Build + Package.\n"
                "6. Install the generated .tar.gz cursor theme."
            ),
            justify="left",
        )
        steps.grid(row=0, column=4, rowspan=5, sticky="ne", padx=(14, 0))

        ttk.Label(workflow, text="Windows cursor folder").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(workflow, textvariable=self.source_dir_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(workflow, text="Browse", command=self.choose_source_dir).grid(row=0, column=2, padx=(8, 0), pady=3)
        ttk.Button(workflow, text="Auto-Fill From Pack", command=self.auto_prepare).grid(row=0, column=3, padx=(8, 0), pady=3)

        ttk.Label(workflow, text="Output root").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(workflow, textvariable=self.work_root_var).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Button(workflow, text="Browse", command=self.choose_work_root).grid(row=1, column=2, padx=(8, 0), pady=3)

        ttk.Label(workflow, text="Theme name").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(workflow, textvariable=self.theme_name_var).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Button(workflow, text="Build + Package", command=self.build_and_package).grid(
            row=2, column=3, padx=(8, 0), pady=3
        )

        ttk.Label(workflow, text="Scale filter").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Combobox(
            workflow,
            textvariable=self.scale_filter_var,
            values=SCALE_FILTER_CHOICES,
            state="readonly",
            width=18,
        ).grid(row=3, column=1, sticky="w", pady=3)

        ttk.Label(workflow, text="Output sizes").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Label(workflow, textvariable=self.size_summary_var).grid(row=4, column=1, sticky="w", pady=3)

        slot_frame = ttk.LabelFrame(outer, text="Source Slots", padding=10)
        slot_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        slot_frame.columnconfigure(0, weight=1)
        slot_frame.rowconfigure(0, weight=1)

        slot_canvas = tk.Canvas(slot_frame, highlightthickness=0)
        slot_canvas.grid(row=0, column=0, sticky="nsew")
        slot_scroll = ttk.Scrollbar(slot_frame, orient="vertical", command=slot_canvas.yview)
        slot_scroll.grid(row=0, column=1, sticky="ns")
        slot_canvas.configure(yscrollcommand=slot_scroll.set)

        slot_inner = ttk.Frame(slot_canvas)
        self.slot_inner = slot_inner
        slot_inner.columnconfigure(2, weight=1)
        slot_window = slot_canvas.create_window((0, 0), window=slot_inner, anchor="nw")

        def _sync_slot_region(_event=None):
            slot_canvas.configure(scrollregion=slot_canvas.bbox("all"))

        def _resize_slot_inner(event):
            slot_canvas.itemconfigure(slot_window, width=event.width)

        slot_inner.bind("<Configure>", _sync_slot_region)
        slot_canvas.bind("<Configure>", _resize_slot_inner)
        slot_canvas.bind_all(
            "<MouseWheel>",
            lambda event: slot_canvas.yview_scroll(-1 * int(event.delta / 120), "units"),
        )

        self.rows = []
        self.rows_by_key = {}
        for idx, slot in enumerate(SLOT_DEFS):
            row = SlotRow(slot_inner, idx, slot, self.refresh_preview)
            self.rows.append(row)
            self.rows_by_key[slot["key"]] = row

        controls = ttk.Frame(outer)
        controls.grid(row=4, column=0, sticky="ew", pady=(10, 8))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, textvariable=self.summary_var).grid(row=0, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.status_var).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(controls, text="Refresh Preview", command=self.refresh_preview).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(controls, text="Load JSON", command=self.load_json).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(controls, text="Save JSON", command=self.save_json).grid(row=0, column=4, padx=(8, 0))
        ttk.Button(controls, text="Save Markdown", command=self.save_markdown).grid(row=0, column=5, padx=(8, 0))

        preview_frame = ttk.LabelFrame(outer, text="Expanded Linux Role Map", padding=10)
        preview_frame.grid(row=5, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview = tk.Text(preview_frame, wrap="none", height=10)
        self.preview.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.preview.configure(yscrollcommand=yscroll.set)

        self.refresh_preview()

    def set_status(self, message: str):
        self.status_var.set(message)
        self.root.update_idletasks()

    def choose_source_dir(self):
        selected = filedialog.askdirectory(title="Choose Windows cursor folder")
        if selected:
            self.source_dir_var.set(selected)
            if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
                self.theme_name_var.set(slugify_name(Path(selected).name))

    def choose_work_root(self):
        selected = filedialog.askdirectory(title="Choose output root")
        if selected:
            self.work_root_var.set(selected)

    def clear_rows(self):
        for row in self.rows:
            row.clear()

    def apply_payload(self, payload: dict):
        self.clear_rows()
        selected = payload.get("selected_slots", {})
        role_map = payload.get("resolved_role_map", {})
        build_options = payload.get("build_options", {})
        used_keys = set()

        scale_filter = build_options.get("scale_filter")
        if scale_filter in SCALE_FILTER_CHOICES:
            self.scale_filter_var.set(scale_filter)

        target_sizes = build_options.get("target_sizes")
        if isinstance(target_sizes, list) and target_sizes:
            self.size_summary_var.set(", ".join(str(int(size)) for size in target_sizes))
        else:
            self.size_summary_var.set(", ".join(str(size) for size in DEFAULT_CURSOR_SIZES))

        for slot_key, item in sorted(selected.items()):
            row = self.rows_by_key.get(slot_key)
            slot = SLOT_BY_KEY.get(slot_key)
            if not row or not slot:
                continue
            path = item.get("path", "")
            row.set_value(path)
            used_keys.add(slot_key)

        if not selected and role_map:
            for slot in SLOT_DEFS:
                if slot["key"] in used_keys:
                    continue
                candidate_path = ""
                for role in slot["roles"]:
                    if role in role_map:
                        candidate_path = role_map[role]
                        break
                if not candidate_path:
                    continue
                self.rows_by_key[slot["key"]].set_value(candidate_path)

        self.refresh_preview()

    def gather_mapping(self):
        selected_slots = {}
        duplicates = []
        for row in self.rows:
            slot = row.get_selected_slot()
            if not slot:
                continue
            path = row.get_path()
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
            slot = item["slot"]
            path = item["path"]
            for role in slot["roles"]:
                resolved[role] = path

        return selected_slots, resolved

    def render_markdown(self, selected_slots, resolved):
        lines = [
            "# Cursor Source Slot Mapping",
            "",
            "## Build Options",
            "",
            f"- Sizes: `{', '.join(str(size) for size in DEFAULT_CURSOR_SIZES)}`",
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

    def refresh_preview(self):
        try:
            selected_slots, resolved = self.gather_mapping()
            text = self.render_markdown(selected_slots, resolved)
            self.summary_var.set(
                f"{len(selected_slots)} source slots selected, {len(resolved)} Linux cursor roles resolved"
            )
        except ValueError as exc:
            text = f"Configuration error:\n\n{exc}\n"
            self.summary_var.set("Fix duplicate slot assignments")

        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)

    def load_json(self):
        target = filedialog.askopenfilename(
            title="Load role mapping JSON",
            filetypes=[("JSON files", "*.json")],
        )
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
        messagebox.showinfo("Loaded", f"Loaded mapping from:\n{target}")

    def save_json(self):
        try:
            selected_slots, resolved = self.gather_mapping()
        except ValueError as exc:
            messagebox.showerror("Invalid mapping", str(exc))
            return

        payload = build_payload(
            selected_slots,
            resolved,
            DEFAULT_CURSOR_SIZES,
            self.scale_filter_var.get(),
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

    def save_markdown(self):
        try:
            selected_slots, resolved = self.gather_mapping()
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
        Path(target).write_text(self.render_markdown(selected_slots, resolved), encoding="utf-8")
        self.set_status(f"Saved preview markdown: {target}")
        messagebox.showinfo("Saved", f"Saved Markdown mapping to:\n{target}")

    def auto_prepare(self):
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
            self.set_status(f"Preparing Windows cursor set into {prep_dir}")
            summary = prepare_windows_cursor_set(source_dir, prep_dir)
            mapping_path = Path(summary["mapping_json"]).resolve()
            payload = load_mapping_payload(mapping_path)
            self.apply_payload(payload)
            self.current_mapping_path = mapping_path
            if self.theme_name_var.get().strip() in {"", "Custom-cursor"}:
                self.theme_name_var.set(slugify_name(source_dir.name))
            self.set_status(f"Auto-filled {summary['selected_slot_count']} slots from {source_dir.name}")
            messagebox.showinfo(
                "Auto-Fill Complete",
                f"Prepared {summary['selected_slot_count']} slots.\n\nMapping JSON:\n{mapping_path}",
            )
        except Exception as exc:  # noqa: BLE001
            self.set_status("Auto-fill failed")
            messagebox.showerror("Auto-fill failed", str(exc))

    def build_and_package(self):
        try:
            selected_slots, resolved = self.gather_mapping()
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
        theme_name = theme_name.strip()
        safe_theme_name = slugify_name(theme_name)
        self.theme_name_var.set(theme_name)

        work_root = Path(self.work_root_var.get().strip()).expanduser()
        if not str(work_root):
            messagebox.showerror("Missing output root", "Choose an output root first.")
            return
        work_root.mkdir(parents=True, exist_ok=True)

        payload = build_payload(
            selected_slots,
            resolved,
            DEFAULT_CURSOR_SIZES,
            self.scale_filter_var.get(),
        )
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
                DEFAULT_CURSOR_SIZES,
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
            self.set_status(f"Built theme and tarball: {tar_path}")
            messagebox.showinfo(
                "Build Complete",
                f"Theme directory:\n{final_theme_dir}\n\nTarball:\n{tar_path}\n\nMapping JSON:\n{mapping_path}",
            )
        except Exception as exc:  # noqa: BLE001
            self.set_status("Build failed")
            messagebox.showerror("Build failed", str(exc))


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load", type=Path, help="preload a mapping JSON into the GUI")
    parser.add_argument("--auto-close-ms", type=int, default=0, help="close automatically after N milliseconds")
    args = parser.parse_args(argv)

    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    app = MappingApp(root)

    if args.load:
        payload = load_mapping_payload(args.load)
        app.apply_payload(payload)
        app.current_mapping_path = args.load.resolve()

    if args.auto_close_ms > 0:
        root.after(args.auto_close_ms, root.destroy)

    root.mainloop()


if __name__ == "__main__":
    main()
