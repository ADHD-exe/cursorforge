#!/usr/bin/env python3
"""Build a Linux Xcursor theme from a JSON mapping exported by source_slot_mapper_gui.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_glitch_theme import (
    HASH_ALIASES,
    build_cursor_file,
    ensure_clean_dir,
    prepare_scaled_frames,
    write_config,
    write_theme_metadata,
)
from slot_definitions import DEFAULT_CURSOR_SIZES
from windows_cursor_tool import extract_asset


def unique_extract_dir(base: Path, source_path: Path) -> Path:
    digest = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:8]
    return base / f"{source_path.stem}-{digest}"


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


def png_metadata(source_path: Path) -> dict:
    identify = find_identify_command()
    result = subprocess.run(
        identify + ["-format", "%w %h", str(source_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    width_str, height_str = result.stdout.strip().split()
    width = int(width_str)
    height = int(height_str)
    return {
        "source": str(source_path),
        "asset_type": "png",
        "frames": [
            {
                "png": str(source_path),
                "delay_ms": 50,
                "width": width,
                "height": height,
                "hotspot_x": 0,
                "hotspot_y": 0,
            }
        ],
    }


def metadata_from_json(source_path: Path) -> dict:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    frames = []
    for frame in payload.get("frames", []):
        frame_path = Path(frame["png"])
        if not frame_path.is_absolute():
            frame_path = (source_path.parent / frame_path).resolve()
        frame_copy = dict(frame)
        frame_copy["png"] = str(frame_path)
        frames.append(frame_copy)
    if not frames:
        raise ValueError(f"metadata JSON contains no frames: {source_path}")
    return {
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


def build_theme_from_mapping(
    mapping_path: Path,
    output_root: Path,
    theme_name: str,
    target_sizes: list[int],
) -> dict:
    payload = load_mapping(mapping_path)
    role_map = payload["resolved_role_map"]

    theme_dir = output_root / theme_name
    extracted_dir = output_root / "_extracted"
    configs_dir = output_root / "_configs"
    cursors_dir = theme_dir / "cursors"

    ensure_clean_dir(output_root)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)
    cursors_dir.mkdir(parents=True, exist_ok=True)

    asset_cache = {}
    manifest = {
        "theme_name": theme_name,
        "mapping_json": str(mapping_path),
        "target_sizes": target_sizes,
        "theme_dir": str(theme_dir),
        "built_assets": {},
    }

    for role_name, source_str in sorted(role_map.items()):
        source_path = Path(source_str).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"source file for role {role_name!r} does not exist: {source_path}")

        cache_key = str(source_path)
        if cache_key not in asset_cache:
            metadata = load_source_metadata(source_path, extracted_dir)
            asset_cache[cache_key] = prepare_scaled_frames(metadata, target_sizes)

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

    manifest_path = output_root / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mapping_json", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--theme-name", default="Custom-cursor")
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_CURSOR_SIZES),
    )
    args = parser.parse_args()

    sizes = sorted({int(part) for part in args.sizes.split(",") if part.strip()})
    manifest = build_theme_from_mapping(args.mapping_json, args.output_root, args.theme_name, sizes)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
