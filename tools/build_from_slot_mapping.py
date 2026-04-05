#!/usr/bin/env python3
"""Build a Linux Xcursor theme from a JSON mapping exported by source_slot_mapper_gui.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from xcursor_builder import (
    HASH_ALIASES,
    build_cursor_file,
    ensure_clean_dir,
    file_cache_token,
    prepare_scaled_frames,
    prepare_scaled_frames_for_size,
    write_config,
    write_theme_metadata,
)
from slot_definitions import (
    DEFAULT_CURSOR_SIZES,
    DEFAULT_SCALE_FILTER,
    SCALE_FILTER_CHOICES,
    normalize_cursor_sizes,
)
from preview_cache import (
    MAX_OUTPUT_PREVIEW_DIRS,
    MAX_SOURCE_CACHE_DIRS,
    cache_artifact_dir,
    prune_cache_dir,
    source_cache_identity,
    touch_cache_path,
)
from workspace_paths import configure_project_tmp
from windows_cursor_tool import extract_asset, sanitize_path_component


MAPPING_FORMAT_VERSION = 2


def unique_extract_dir(base: Path, source_path: Path) -> Path:
    return cache_artifact_dir(base, source_path)


def owned_build_root(output_root: Path, theme_name: str) -> Path:
    return output_root / "_cursorforge-build" / sanitize_path_component(theme_name)


def parse_size_list(raw_sizes: str | list[int] | None) -> list[int]:
    return normalize_cursor_sizes(raw_sizes, fallback=DEFAULT_CURSOR_SIZES)


def load_mapping(mapping_path: Path) -> dict:
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    if "resolved_role_map" not in payload or not isinstance(payload["resolved_role_map"], dict):
        raise ValueError("mapping JSON is missing a resolved_role_map object")
    return payload


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


def identify_png_size(source_path: Path) -> tuple[int, int]:
    identify = find_identify_command()
    result = subprocess.run(
        identify + ["-format", "%w %h", str(source_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    width_str, height_str = result.stdout.strip().split()
    return int(width_str), int(height_str)


def png_metadata(source_path: Path) -> dict:
    width, height = identify_png_size(source_path)
    return {
        "format_version": 2,
        "source": str(source_path),
        "asset_type": "png",
        "frames": [
            {
                "frame_index": 0,
                "delay_ms": 50,
                "entries": [
                    {
                        "png": str(source_path),
                        "width": width,
                        "height": height,
                        "hotspot_x": 0,
                        "hotspot_y": 0,
                        "entry_index": 1,
                        "image_size": source_path.stat().st_size,
                    }
                ],
            }
        ],
    }


def resolve_png_entry(entry: dict, base_dir: Path, fallback_index: int) -> dict:
    if "png" not in entry:
        raise ValueError("metadata entry is missing a png path")

    entry_copy = dict(entry)
    png_path = Path(entry_copy["png"])
    if not png_path.is_absolute():
        png_path = (base_dir / png_path).resolve()
    if not png_path.exists():
        raise FileNotFoundError(f"metadata PNG does not exist: {png_path}")

    actual_width, actual_height = identify_png_size(png_path)
    width = entry_copy.get("width")
    height = entry_copy.get("height")
    if width is None or height is None:
        width, height = actual_width, actual_height
    else:
        width = int(width)
        height = int(height)
        if (width, height) != (actual_width, actual_height):
            raise ValueError(
                f"metadata dimensions for {png_path} are {width}x{height}, "
                f"but the PNG is {actual_width}x{actual_height}"
            )

    if width <= 0 or height <= 0:
        raise ValueError(f"metadata entry dimensions must be positive, got {width}x{height} for {png_path}")

    entry_copy["png"] = str(png_path)
    entry_copy["width"] = width
    entry_copy["height"] = height
    entry_copy["hotspot_x"] = int(entry_copy.get("hotspot_x", 0))
    entry_copy["hotspot_y"] = int(entry_copy.get("hotspot_y", 0))
    if entry_copy["hotspot_x"] < 0 or entry_copy["hotspot_x"] >= width:
        raise ValueError(f"hotspot_x {entry_copy['hotspot_x']} is outside {width}px width for {png_path}")
    if entry_copy["hotspot_y"] < 0 or entry_copy["hotspot_y"] >= height:
        raise ValueError(f"hotspot_y {entry_copy['hotspot_y']} is outside {height}px height for {png_path}")
    entry_copy["entry_index"] = int(entry_copy.get("entry_index", entry_copy.get("index", fallback_index)))
    if "colors" in entry_copy and entry_copy["colors"] is not None:
        entry_copy["colors"] = int(entry_copy["colors"])
        if entry_copy["colors"] < 0:
            raise ValueError(f"colors must be non-negative for {png_path}")
    if "image_size" in entry_copy and entry_copy["image_size"] is not None:
        entry_copy["image_size"] = int(entry_copy["image_size"])
        if entry_copy["image_size"] < 0:
            raise ValueError(f"image_size must be non-negative for {png_path}")
    else:
        entry_copy["image_size"] = png_path.stat().st_size
    return entry_copy


def metadata_from_json(source_path: Path) -> dict:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"metadata JSON root must be an object: {source_path}")
    raw_frames = payload.get("frames", [])
    if not isinstance(raw_frames, list):
        raise ValueError(f"metadata JSON frames must be a list: {source_path}")
    frames = []
    for frame_index, frame in enumerate(raw_frames):
        if not isinstance(frame, dict):
            raise ValueError(f"metadata frame {frame_index} must be an object: {source_path}")
        delay_ms = int(frame.get("delay_ms", 50))
        if delay_ms < 0:
            raise ValueError(f"metadata frame {frame_index} delay_ms must be non-negative: {source_path}")
        if "entries" in frame:
            raw_entries = frame["entries"]
        else:
            raw_entries = [frame]
        if not isinstance(raw_entries, list):
            raise ValueError(f"metadata frame {frame_index} entries must be a list: {source_path}")
        if not raw_entries:
            raise ValueError(f"metadata frame {frame_index} entries list is empty: {source_path}")
        resolved_entries = [
            resolve_png_entry(entry, source_path.parent, entry_index)
            for entry_index, entry in enumerate(raw_entries, start=1)
        ]
        frames.append(
            {
                "frame_index": int(frame.get("frame_index", frame_index)),
                "icon_index": frame.get("icon_index"),
                "delay_ms": delay_ms,
                "nominal_size": frame.get("nominal_size"),
                "entries": sorted(
                    resolved_entries,
                    key=lambda item: (
                        item["width"],
                        item["height"],
                        -int(item.get("image_size", 0) or 0),
                        item["entry_index"],
                    ),
                ),
            }
        )
    if not frames:
        raise ValueError(f"metadata JSON contains no frames: {source_path}")
    return {
        "format_version": int(payload.get("format_version", 1)),
        "source": payload.get("source", str(source_path)),
        "asset_type": payload.get("asset_type", "json"),
        "frames": frames,
    }


def load_source_metadata(source_path: Path, extracted_dir: Path) -> dict:
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        return metadata_from_json(source_path)
    if suffix == ".png":
        return png_metadata(source_path)
    return extract_asset(source_path, unique_extract_dir(extracted_dir, source_path))


def localize_metadata_frames(metadata: dict, localized_dir: Path) -> dict:
    localized_dir.mkdir(parents=True, exist_ok=True)
    localized = {
        "source": metadata.get("source"),
        "asset_type": metadata.get("asset_type"),
        "scale_filter": metadata.get("scale_filter", DEFAULT_SCALE_FILTER),
        "frames": [],
    }

    for sequence_index, frame in enumerate(metadata.get("frames", [])):
        source_png = Path(frame["png"]).expanduser().resolve()
        suffix = source_png.suffix or ".png"
        digest = hashlib.sha256(
            f"{source_png}::{file_cache_token(source_png)}".encode("utf-8")
        ).hexdigest()[:10]
        frame_index = int(frame.get("frame_index", sequence_index))
        entry_index = int(frame.get("entry_index", 1) or 1)
        target_name = (
            f"f{frame_index:03d}_n{int(frame.get('nominal_size', frame['width'])):03d}_"
            f"{int(frame['width']):03d}x{int(frame['height']):03d}_e{entry_index:02d}_"
            f"{sanitize_path_component(source_png.stem)}_{digest}{suffix}"
        )
        target_path = localized_dir / target_name
        if source_png != target_path and not target_path.exists():
            shutil.copy2(source_png, target_path)
        if source_png == target_path:
            output_png = source_png
        else:
            output_png = target_path

        frame_copy = dict(frame)
        frame_copy["png"] = str(output_png)
        localized["frames"].append(frame_copy)

    if not localized["frames"]:
        raise ValueError("localized metadata contains no frames")
    return localized


def choose_preview_nominal_size(target_sizes: list[int], preferred_size: int = 32) -> int:
    sizes = parse_size_list(target_sizes)
    return min(sizes, key=lambda size: (abs(size - preferred_size), size))


def prepare_output_preview_metadata(
    source_path: Path,
    preview_root: Path,
    target_sizes: list[int] | None = None,
    *,
    scale_filter: str | None = None,
    preview_nominal_size: int | None = None,
    source_metadata: dict | None = None,
    source_cache_root: Path | None = None,
) -> dict:
    source_path = source_path.expanduser().resolve()
    preview_root = preview_root.expanduser().resolve()
    preview_root.mkdir(parents=True, exist_ok=True)

    sizes = parse_size_list(target_sizes)
    filter_name = (scale_filter or DEFAULT_SCALE_FILTER).strip().lower()
    if filter_name not in SCALE_FILTER_CHOICES:
        choices = ", ".join(SCALE_FILTER_CHOICES)
        raise ValueError(f"unsupported scale filter {filter_name!r}; expected one of: {choices}")

    source_cache_root = (source_cache_root or preview_root.parent / "_source").expanduser().resolve()
    asset_work_dir = unique_extract_dir(preview_root, source_path)
    metadata = source_metadata if source_metadata is not None else load_source_metadata(source_path, source_cache_root)
    available_nominal_sizes = list(sizes)
    desired_size = (
        int(preview_nominal_size)
        if preview_nominal_size is not None
        else choose_preview_nominal_size(available_nominal_sizes)
    )
    selected_nominal_size = min(available_nominal_sizes, key=lambda size: (abs(size - desired_size), size))
    if source_path.suffix.lower() not in {".json", ".png"}:
        touch_cache_path(unique_extract_dir(source_cache_root, source_path))
        prune_cache_dir(source_cache_root, MAX_SOURCE_CACHE_DIRS)
    prepared = prepare_scaled_frames_for_size(
        metadata,
        selected_nominal_size,
        scale_filter=filter_name,
        generated_dir=asset_work_dir,
    )
    localized = localize_metadata_frames(prepared, asset_work_dir)
    preview_frames = localized["frames"]
    if not preview_frames:
        raise ValueError(f"no prepared frames available for preview size {selected_nominal_size}")

    touch_cache_path(asset_work_dir)
    prune_cache_dir(preview_root, MAX_OUTPUT_PREVIEW_DIRS)

    return {
        "source": localized.get("source"),
        "asset_type": localized.get("asset_type"),
        "scale_filter": localized.get("scale_filter", filter_name),
        "available_nominal_sizes": available_nominal_sizes,
        "preview_nominal_size": selected_nominal_size,
        "frames": preview_frames,
    }


def build_theme_from_mapping(
    mapping_path: Path,
    output_root: Path,
    theme_name: str,
    target_sizes: list[int] | None = None,
    scale_filter: str | None = None,
) -> dict:
    payload = load_mapping(mapping_path)
    role_map = payload["resolved_role_map"]
    build_options = payload.get("build_options", {})

    sizes = parse_size_list(target_sizes or build_options.get("target_sizes"))
    filter_name = (scale_filter or build_options.get("scale_filter") or DEFAULT_SCALE_FILTER).strip().lower()
    if filter_name not in SCALE_FILTER_CHOICES:
        choices = ", ".join(SCALE_FILTER_CHOICES)
        raise ValueError(f"unsupported scale filter {filter_name!r}; expected one of: {choices}")

    output_root = output_root.expanduser().resolve()
    theme_dir = output_root / theme_name
    build_root = owned_build_root(output_root, theme_name)
    extracted_dir = build_root / "extracted"
    configs_dir = build_root / "configs"
    cursors_dir = theme_dir / "cursors"

    output_root.mkdir(parents=True, exist_ok=True)
    ensure_clean_dir(theme_dir)
    ensure_clean_dir(build_root)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)
    cursors_dir.mkdir(parents=True, exist_ok=True)

    asset_cache = {}
    manifest = {
        "mapping_format_version": MAPPING_FORMAT_VERSION,
        "theme_name": theme_name,
        "mapping_json": str(mapping_path),
        "target_sizes": sizes,
        "scale_filter": filter_name,
        "theme_dir": str(theme_dir),
        "build_root": str(build_root),
        "built_assets": {},
    }

    for role_name, source_str in sorted(role_map.items()):
        source_path = Path(source_str).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"source file for role {role_name!r} does not exist: {source_path}")

        cache_key = source_cache_identity(source_path)
        if cache_key not in asset_cache:
            asset_work_dir = unique_extract_dir(extracted_dir, source_path)
            metadata = load_source_metadata(source_path, extracted_dir)
            prepared = prepare_scaled_frames(
                metadata,
                sizes,
                scale_filter=filter_name,
                generated_dir=asset_work_dir,
            )
            asset_cache[cache_key] = localize_metadata_frames(prepared, asset_work_dir)

        metadata = asset_cache[cache_key]
        frames_dir = Path(metadata["frames"][0]["png"]).parent
        config_path = configs_dir / f"{role_name}.conf"
        output_path = cursors_dir / role_name
        write_config(config_path, metadata)
        build_cursor_file(config_path, frames_dir, output_path)

        manifest["built_assets"][role_name] = {
            "source_path": str(source_path),
            "config": str(config_path),
            "cursor_file": str(output_path),
        }

    for target, aliases in HASH_ALIASES.items():
        if not (cursors_dir / target).exists():
            continue
        for alias_name in aliases:
            alias_path = cursors_dir / alias_name
            if alias_path.exists() or alias_path.is_symlink():
                alias_path.unlink()
            os.symlink(target, alias_path)

    write_theme_metadata(
        theme_dir,
        theme_name=theme_name,
        comment="Cursor theme built from a slot-mapping JSON exported by the source slot mapper",
    )

    manifest_path = build_root / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main() -> int:
    configure_project_tmp()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mapping_json", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--theme-name", default="Custom-cursor")
    parser.add_argument(
        "--sizes",
        default=None,
        help="comma-separated cursor sizes to emit; omit to use build_options.target_sizes from the mapping JSON",
    )
    parser.add_argument(
        "--scale-filter",
        default=None,
        choices=SCALE_FILTER_CHOICES,
        help="ImageMagick resize filter to use when scaling is required; omit to use the mapping JSON value",
    )
    args = parser.parse_args()

    sizes = parse_size_list(args.sizes) if args.sizes else None
    manifest = build_theme_from_mapping(
        args.mapping_json,
        args.output_root,
        args.theme_name,
        sizes,
        scale_filter=args.scale_filter,
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
