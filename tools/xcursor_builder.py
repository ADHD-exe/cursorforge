#!/usr/bin/env python3
"""Shared Xcursor theme builder helpers and the legacy glitch-theme entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from slot_definitions import (
    DEFAULT_CURSOR_SIZES,
    DEFAULT_SCALE_FILTER,
    SCALE_FILTER_CHOICES,
    normalize_cursor_sizes,
)
from windows_cursor_tool import extract_asset, sanitize_path_component


VARIANTS = {
    "v1": {
        "default": "start.ani",
        "arrow": "start.ani",
        "help": "help.cur",
        "text": "text.ani",
        "progress": "start.ani",
        "wait": "wait.ani",
    },
    "v2": {
        "default": "start2.ani",
        "arrow": "start2.ani",
        "help": "help2.cur",
        "text": "text2.ani",
        "progress": "start2.ani",
        "wait": "wait2.ani",
    },
    "v3": {
        "default": "start3.ani",
        "arrow": "start3.ani",
        "help": "help3.cur",
        "text": "text.ani",
        "progress": "start3.ani",
        "wait": "wait2.ani",
    },
}


FILE_ROLE_MAP = {
    "alias": "link.cur",
    "all-scroll": "move.cur",
    "arrow": "__ARROW__",
    "bd_double_arrow": "diag2.cur",
    "bottom_left_corner": "diag1.cur",
    "bottom_right_corner": "diag2.cur",
    "bottom_side": "vert.cur",
    "cell": "cross.cur",
    "center-main": "__DEFAULT__",
    "center_main": "__DEFAULT__",
    "center_ptr": "cross.cur",
    "circle": "no.cur",
    "clock": "__WAIT__",
    "closedhand": "move.cur",
    "col-resize": "hori.cur",
    "color-picker": "pen.cur",
    "context-menu": "__HELP__",
    "copy": "link.cur",
    "cross": "cross.cur",
    "cross_reverse": "cross.cur",
    "crosshair": "cross.cur",
    "crossed_circle": "no.cur",
    "default": "__DEFAULT__",
    "diamond_cross": "cross.cur",
    "dnd-ask": "__HELP__",
    "dnd-copy": "link.cur",
    "dnd-link": "link.cur",
    "dnd-move": "move.cur",
    "dnd-no-drop": "no.cur",
    "dnd-none": "no.cur",
    "dot": "cross.cur",
    "dot_box_mask": "cross.cur",
    "dotbox": "cross.cur",
    "down-arrow": "vert.cur",
    "draft": "pen.cur",
    "draft_large": "pen.cur",
    "draft_small": "pen.cur",
    "dragging": "move.cur",
    "draped_box": "cross.cur",
    "e-resize": "hori.cur",
    "ew-resize": "hori.cur",
    "fd_double_arrow": "diag1.cur",
    "fleur": "move.cur",
    "forbidden": "no.cur",
    "grabbing": "move.cur",
    "hand": "hand.cur",
    "hand1": "hand.cur",
    "hand2": "hand.cur",
    "help": "__HELP__",
    "h_double_arrow": "hori.cur",
    "half-busy": "__PROGRESS__",
    "horizontal-text": "__TEXT__",
    "ibeam": "__TEXT__",
    "icon": "cross.cur",
    "kill": "person.cur",
    "left-arrow": "hori.cur",
    "left-main": "__DEFAULT__",
    "left_ptr": "__DEFAULT__",
    "left_ptr_help": "__HELP__",
    "left_ptr_watch": "__PROGRESS__",
    "left_side": "hori.cur",
    "link": "link.cur",
    "move": "move.cur",
    "n-resize": "vert.cur",
    "ne-resize": "diag1.cur",
    "nesw-resize": "diag1.cur",
    "no-drop": "no.cur",
    "not-allowed": "no.cur",
    "ns-resize": "vert.cur",
    "nw-resize": "diag2.cur",
    "nwse-resize": "diag2.cur",
    "openhand": "move.cur",
    "pencil": "pen.cur",
    "pirate": "person.cur",
    "plus": "cross.cur",
    "pointer": "hand.cur",
    "pointer2": "hand.cur",
    "pointing_hand": "hand.cur",
    "progress": "__PROGRESS__",
    "question_arrow": "__HELP__",
    "right-arrow": "hori.cur",
    "right-main": "__DEFAULT__",
    "right_ptr": "__DEFAULT__",
    "right_side": "hori.cur",
    "row-resize": "vert.cur",
    "s-resize": "vert.cur",
    "sb_h_double_arrow": "hori.cur",
    "sb_v_double_arrow": "vert.cur",
    "sb_up_arrow": "vert.cur",
    "scan": "link.cur",
    "se-resize": "diag2.cur",
    "size_all": "move.cur",
    "size_bdiag": "diag1.cur",
    "size_fdiag": "diag2.cur",
    "size_hor": "hori.cur",
    "size_ver": "vert.cur",
    "split_h": "hori.cur",
    "split_v": "vert.cur",
    "sw-resize": "diag1.cur",
    "target": "cross.cur",
    "tcross": "cross.cur",
    "text": "__TEXT__",
    "top_left_arrow": "__DEFAULT__",
    "top_left_corner": "diag2.cur",
    "top_right_arrow": "__HELP__",
    "top_right_corner": "diag1.cur",
    "top_side": "vert.cur",
    "up-arrow": "vert.cur",
    "up_arrow": "vert.cur",
    "v_double_arrow": "vert.cur",
    "vertical-text": "__TEXT__",
    "ver-resize": "vert.cur",
    "w-resize": "hori.cur",
    "wait": "__WAIT__",
    "watch": "__WAIT__",
    "wayland-cursor": "__DEFAULT__",
    "whats_this": "__HELP__",
    "x-cursor": "no.cur",
    "xterm": "__TEXT__",
    "zoom-in": "person.cur",
    "zoom-out": "person.cur",
    "zoom_in": "person.cur",
    "zoom_out": "person.cur",
}


HASH_ALIASES = {
    "alias": [
        "3085a0e285430894940527032f8b26df",
        "640fb0e74195791501fd1ed57b41487f",
        "a2a266d0498c3104214a47bd64ab0fc8",
    ],
    "copy": [
        "1081e37283d90000800003c07f3ef6bf",
        "6407b0e94181790501fd1e167b474872",
        "b66166c04f8c3109214a4fbd64a50fc8",
    ],
    "help": [
        "5c6cd98b3f3ebcb1f9c7f1c204630408",
        "d9ce0ab605698f320427677b458ad60b",
    ],
    "pointer": [
        "9d800788f1b08800ae810202380a0822",
        "e29285e634086352946a0e7090d73106",
    ],
    "progress": [
        "00000000000000020006000e7e9ffc3f",
        "08e8e1c95fe2fc01f976f1e063a24ccd",
        "3ecb610c1bf2410f44200f48c40d3599",
    ],
    "size_ver": [
        "00008160000006810000408080010102",
    ],
    "move": [
        "4498f0e0c1937ffe01fd06f973665830",
        "9081237383d90e509aa00f00170e968f",
        "fcf21c00b30f7e3f83fe0dfd12e71cff",
    ],
}


def resolve_source(asset_name: str, variant_name: str) -> str:
    variant = VARIANTS[variant_name]
    if asset_name == "__DEFAULT__":
        return variant["default"]
    if asset_name == "__ARROW__":
        return variant["arrow"]
    if asset_name == "__HELP__":
        return variant["help"]
    if asset_name == "__TEXT__":
        return variant["text"]
    if asset_name == "__PROGRESS__":
        return variant["progress"]
    if asset_name == "__WAIT__":
        return variant["wait"]
    return asset_name


def find_image_tool() -> str:
    for tool_name in ("magick", "convert"):
        tool_path = shutil.which(tool_name)
        if tool_path:
            return tool_path
    raise RuntimeError("ImageMagick is required but neither 'magick' nor 'convert' was found")


def find_identify_command() -> list[str]:
    for command in (["magick", "identify"], ["identify"]):
        try:
            subprocess.run(
                command + ["-version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return command
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("ImageMagick identify is required but was not found")


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def scale_hotspot(value: int, old_size: int, new_size: int) -> int:
    if old_size <= 0:
        return value
    scaled = int(round(value * new_size / old_size))
    return max(0, min(new_size - 1, scaled))


def identify_png_size(source_png: Path) -> tuple[int, int]:
    identify = find_identify_command()
    result = subprocess.run(
        identify + ["-format", "%w %h", str(source_png)],
        check=True,
        capture_output=True,
        text=True,
    )
    width_str, height_str = result.stdout.strip().split()
    return int(width_str), int(height_str)


def normalize_entry(entry: dict, fallback_entry_index: int) -> dict:
    if "png" not in entry:
        raise ValueError("metadata entry is missing a png path")

    width = int(entry["width"])
    height = int(entry["height"])
    if width <= 0 or height <= 0:
        raise ValueError(f"metadata entry dimensions must be positive, got {width}x{height}")

    hotspot_x = int(entry.get("hotspot_x", 0))
    hotspot_y = int(entry.get("hotspot_y", 0))
    if hotspot_x < 0 or hotspot_x >= width:
        raise ValueError(f"metadata hotspot_x {hotspot_x} is outside entry width {width}")
    if hotspot_y < 0 or hotspot_y >= height:
        raise ValueError(f"metadata hotspot_y {hotspot_y} is outside entry height {height}")

    normalized = {
        "png": str(Path(entry["png"]).expanduser()),
        "width": width,
        "height": height,
        "hotspot_x": hotspot_x,
        "hotspot_y": hotspot_y,
        "entry_index": int(entry.get("entry_index", entry.get("index", fallback_entry_index))),
    }
    if "colors" in entry and entry["colors"] is not None:
        normalized["colors"] = int(entry["colors"])
        if normalized["colors"] < 0:
            raise ValueError("metadata colors must be non-negative")
    if "image_size" in entry and entry["image_size"] is not None:
        normalized["image_size"] = int(entry["image_size"])
        if normalized["image_size"] < 0:
            raise ValueError("metadata image_size must be non-negative")
    return normalized


def normalize_metadata(metadata: dict) -> dict:
    raw_frames = metadata.get("frames", [])
    if not isinstance(raw_frames, list):
        raise ValueError("metadata frames must be a list")

    normalized = {
        "format_version": metadata.get("format_version", 1),
        "source": metadata.get("source"),
        "asset_type": metadata.get("asset_type", "unknown"),
        "frames": [],
    }

    for frame_index, frame in enumerate(raw_frames):
        if not isinstance(frame, dict):
            raise ValueError(f"frame {frame_index} must be an object")
        delay_ms = int(frame.get("delay_ms", 50))
        if delay_ms < 0:
            raise ValueError(f"frame {frame_index} delay_ms must be non-negative")
        if "entries" in frame:
            if not isinstance(frame["entries"], list):
                raise ValueError(f"frame {frame_index} entries must be a list")
            entries = []
            for entry_index, entry in enumerate(frame["entries"], start=1):
                if not isinstance(entry, dict):
                    raise ValueError(f"frame {frame_index} entry {entry_index} must be an object")
                entries.append(normalize_entry(entry, entry_index))
        else:
            entries = [normalize_entry(frame, frame_index + 1)]

        if not entries:
            raise ValueError(f"frame {frame_index} contains no native entries")

        normalized["frames"].append(
            {
                "frame_index": int(frame.get("frame_index", frame_index)),
                "delay_ms": delay_ms,
                "entries": entries,
            }
        )

    if not normalized["frames"]:
        raise ValueError("metadata contains no frames")
    return normalized


def validate_scale_filter(scale_filter: str) -> str:
    filter_name = (scale_filter or DEFAULT_SCALE_FILTER).strip().lower()
    if filter_name not in SCALE_FILTER_CHOICES:
        choices = ", ".join(SCALE_FILTER_CHOICES)
        raise ValueError(f"unsupported scale filter {scale_filter!r}; expected one of: {choices}")
    return filter_name


def choose_best_entry(entries: list[dict], target_size: int) -> dict:
    if not entries:
        raise ValueError("frame contains no native entries")

    def color_rank(item: dict) -> int:
        colors = int(item.get("colors", 0) or 0)
        # In CUR headers a color count of 0 typically means >= 256 colors or truecolor.
        return 1_000_000 if colors == 0 else colors

    def image_size_rank(item: dict) -> int:
        return int(item.get("image_size", 0) or 0)

    def smallest_fit_key(item: dict) -> tuple[int, int, int, int, int]:
        # Prefer the smallest native size that still satisfies the requested
        # nominal cursor size, then rank equal-sized entries by richer payload.
        return (
            max(item["width"], item["height"]),
            item["width"] * item["height"],
            -image_size_rank(item),
            -color_rank(item),
            int(item.get("entry_index", 0)),
        )

    def largest_fallback_key(item: dict) -> tuple[int, int, int, int, int]:
        return (
            max(item["width"], item["height"]),
            item["width"] * item["height"],
            image_size_rank(item),
            color_rank(item),
            -int(item.get("entry_index", 0)),
        )

    fitting_entries = [entry for entry in entries if entry["width"] >= target_size and entry["height"] >= target_size]
    if fitting_entries:
        return min(fitting_entries, key=smallest_fit_key)
    return max(entries, key=largest_fallback_key)


def ensure_scaled_png(
    source_png: Path,
    generated_dir: Path,
    target_size: int,
    scale_filter: str,
) -> Path:
    generated_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = sanitize_path_component(source_png.stem)
    digest = hashlib.sha256(str(source_png.expanduser().resolve()).encode("utf-8")).hexdigest()[:10]
    output_path = generated_dir / f"{safe_stem}_{digest}_{scale_filter}_{target_size}.png"
    if output_path.exists():
        return output_path

    image_tool = find_image_tool()
    subprocess.run(
        [image_tool, str(source_png), "-filter", scale_filter, "-resize", f"{target_size}x{target_size}", str(output_path)],
        check=True,
    )
    return output_path


def prepare_scaled_frames(
    metadata: dict,
    target_sizes: list[int],
    scale_filter: str = DEFAULT_SCALE_FILTER,
    generated_dir: Path | None = None,
) -> dict:
    normalized = normalize_metadata(metadata)
    filter_name = validate_scale_filter(scale_filter)
    build_frames = {
        "source": normalized["source"],
        "asset_type": normalized["asset_type"],
        "scale_filter": filter_name,
        "frames": [],
    }

    cache_dir = generated_dir if generated_dir is not None else None

    for frame in normalized["frames"]:
        for size in target_sizes:
            native_entry = choose_best_entry(frame["entries"], size)
            source_png = Path(native_entry["png"])
            if native_entry["width"] == size and native_entry["height"] == size:
                output_png = source_png
            else:
                scaled_dir = cache_dir if cache_dir is not None else source_png.parent
                output_png = ensure_scaled_png(source_png, scaled_dir, size, filter_name)
            output_width, output_height = identify_png_size(output_png)

            build_frames["frames"].append(
                {
                    "png": str(output_png),
                    "delay_ms": frame["delay_ms"],
                    "width": output_width,
                    "height": output_height,
                    # Keep the requested cursor size as the Xcursor nominal size,
                    # but preserve the emitted PNG dimensions and per-axis hotspot scaling.
                    "nominal_size": size,
                    "hotspot_x": scale_hotspot(native_entry["hotspot_x"], native_entry["width"], output_width),
                    "hotspot_y": scale_hotspot(native_entry["hotspot_y"], native_entry["height"], output_height),
                    "frame_index": frame["frame_index"],
                    "entry_index": native_entry.get("entry_index"),
                    "native_width": native_entry["width"],
                    "native_height": native_entry["height"],
                    "native_image_size": native_entry.get("image_size"),
                    "native_colors": native_entry.get("colors"),
                }
            )

    return build_frames


def write_config(config_path: Path, metadata: dict) -> None:
    lines = ["# nominal_size xhot yhot path delay"]
    for frame in metadata["frames"]:
        frame_path = Path(frame["png"])
        nominal_size = int(frame.get("nominal_size", frame["width"]))
        lines.append(
            f"{nominal_size} {frame['hotspot_x']} {frame['hotspot_y']} {frame_path.name} {frame['delay_ms']}"
        )
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_cursor_file(config_path: Path, frames_dir: Path, output_path: Path) -> None:
    subprocess.run(
        ["xcursorgen", "--prefix", str(frames_dir), str(config_path), str(output_path)],
        check=True,
    )


def write_theme_metadata(
    theme_dir: Path,
    theme_name: str = "ADHD-cursor",
    comment: str = "Glitch live cursor theme converted from a Windows animated cursor set",
) -> None:
    (theme_dir / "index.theme").write_text(
        "\n".join(
            [
                "[Icon Theme]",
                f"Name={theme_name}",
                f"Comment={comment}",
                "Example=default",
                "Inherits=Adwaita",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (theme_dir / "cursor.theme").write_text(
        f"[Icon Theme]\nInherits={theme_name}\n",
        encoding="utf-8",
    )


def build_theme(
    source_dir: Path,
    build_root: Path,
    variant_name: str,
    target_sizes: list[int],
    scale_filter: str = DEFAULT_SCALE_FILTER,
) -> dict:
    variant_dir = build_root / f"variant-{variant_name}"
    extracted_dir = variant_dir / "extracted"
    configs_dir = variant_dir / "configs"
    theme_dir = variant_dir / "ADHD-cursor"
    cursors_dir = theme_dir / "cursors"

    ensure_clean_dir(variant_dir)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)
    cursors_dir.mkdir(parents=True, exist_ok=True)

    filter_name = validate_scale_filter(scale_filter)
    asset_cache = {}
    manifest = {
        "variant": variant_name,
        "target_sizes": target_sizes,
        "scale_filter": filter_name,
        "source_dir": str(source_dir),
        "built_assets": {},
        "theme_dir": str(theme_dir),
    }

    for role_name, source_asset in sorted(FILE_ROLE_MAP.items()):
        asset_name = resolve_source(source_asset, variant_name)
        if asset_name not in asset_cache:
            asset_path = source_dir / asset_name
            extracted_asset_dir = extracted_dir / sanitize_path_component(Path(asset_name).stem)
            metadata = extract_asset(asset_path, extracted_asset_dir)
            asset_cache[asset_name] = prepare_scaled_frames(
                metadata,
                target_sizes,
                scale_filter=filter_name,
                generated_dir=extracted_asset_dir,
            )
        metadata = asset_cache[asset_name]
        role_frames_dir = Path(metadata["frames"][0]["png"]).parent
        config_path = configs_dir / f"{role_name}.conf"
        output_path = cursors_dir / role_name
        write_config(config_path, metadata)
        build_cursor_file(config_path, role_frames_dir, output_path)
        manifest["built_assets"][role_name] = {
            "source_asset": asset_name,
            "config": str(config_path),
            "cursor_file": str(output_path),
        }

    for target, hashes in HASH_ALIASES.items():
        for alias_name in hashes:
            alias_path = cursors_dir / alias_name
            if alias_path.exists() or alias_path.is_symlink():
                alias_path.unlink()
            os.symlink(target, alias_path)

    write_theme_metadata(theme_dir)
    manifest_path = variant_dir / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("build_root", type=Path)
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="v3")
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_CURSOR_SIZES),
        help="comma-separated cursor sizes to emit",
    )
    parser.add_argument(
        "--scale-filter",
        default=DEFAULT_SCALE_FILTER,
        choices=SCALE_FILTER_CHOICES,
        help="ImageMagick resize filter to use when scaling is required",
    )
    args = parser.parse_args()

    sizes = normalize_cursor_sizes(args.sizes, fallback=DEFAULT_CURSOR_SIZES)
    manifest = build_theme(args.source_dir, args.build_root, args.variant, sizes, scale_filter=args.scale_filter)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
