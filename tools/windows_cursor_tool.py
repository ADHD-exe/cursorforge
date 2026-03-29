#!/usr/bin/env python3
"""Inspect and extract Windows .cur/.ani cursor assets."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


METADATA_FORMAT_VERSION = 2


def sanitize_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "cursor"


def parse_cur_bytes(data: bytes) -> dict:
    if len(data) < 6:
        raise ValueError("cursor file is too small")
    reserved, file_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or file_type != 2 or count < 1:
        raise ValueError("not a CUR file")

    entries = []
    for index in range(count):
        offset = 6 + index * 16
        if offset + 16 > len(data):
            raise ValueError("truncated CUR directory entry")
        width, height, colors, _reserved, hotspot_x, hotspot_y, size, image_offset = struct.unpack_from(
            "<BBBBHHII", data, offset
        )
        entries.append(
            {
                "index": index + 1,
                "width": width or 256,
                "height": height or 256,
                "colors": colors,
                "hotspot_x": hotspot_x,
                "hotspot_y": hotspot_y,
                "image_size": size,
                "image_offset": image_offset,
            }
        )

    return {
        "type": "cur",
        "count": count,
        "entries": entries,
    }


def _iter_riff_chunks(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        chunk_id = data[pos : pos + 4].decode("ascii", errors="replace")
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        payload_start = pos + 8
        payload_end = payload_start + chunk_size
        if payload_end > end:
            raise ValueError(f"truncated RIFF chunk {chunk_id!r}")
        yield chunk_id, payload_start, payload_end
        pos = payload_end + (chunk_size & 1)


def _parse_u32_chunk(chunk_name: str, payload: bytes) -> list[int]:
    if len(payload) % 4 != 0:
        raise ValueError(f"ANI {chunk_name} chunk length {len(payload)} is not a multiple of 4 bytes")
    if not payload:
        return []
    return list(struct.unpack(f"<{len(payload) // 4}I", payload))


def parse_ani_bytes(data: bytes) -> dict:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"ACON":
        raise ValueError("not an ANI file")

    riff_size = struct.unpack_from("<I", data, 4)[0]
    riff_end = min(len(data), 8 + riff_size)

    anih = None
    rates = []
    sequence = []
    icons = []

    for chunk_id, payload_start, payload_end in _iter_riff_chunks(data, 12, riff_end):
        if chunk_id == "anih":
            if payload_end - payload_start < 36:
                raise ValueError("short anih chunk")
            fields = struct.unpack_from("<9I", data, payload_start)
            anih = {
                "header_size": fields[0],
                "frames": fields[1],
                "steps": fields[2],
                "width": fields[3],
                "height": fields[4],
                "bit_count": fields[5],
                "planes": fields[6],
                "display_rate_jiffies": fields[7],
                "flags": fields[8],
            }
        elif chunk_id == "rate":
            rates = _parse_u32_chunk("rate", data[payload_start:payload_end])
        elif chunk_id == "seq ":
            sequence = _parse_u32_chunk("seq", data[payload_start:payload_end])
        elif chunk_id == "LIST":
            if payload_end - payload_start < 4:
                continue
            list_type = data[payload_start : payload_start + 4].decode("ascii", errors="replace")
            if list_type != "fram":
                continue
            for subchunk_id, sub_start, sub_end in _iter_riff_chunks(data, payload_start + 4, payload_end):
                if subchunk_id == "icon":
                    icons.append(data[sub_start:sub_end])

    if not anih:
        raise ValueError("ANI file is missing an anih chunk")
    if not icons:
        raise ValueError("ANI file contains no icon frames")

    steps = anih["steps"] or len(sequence) or len(icons)
    if steps < 1:
        raise ValueError("ANI file contains no animation steps")
    if not sequence:
        sequence = list(range(min(steps, len(icons))))
    if not rates:
        rates = [anih["display_rate_jiffies"]] * steps
    if len(rates) < steps:
        rates.extend([anih["display_rate_jiffies"]] * (steps - len(rates)))

    frame_entries = []
    for step_index in range(steps):
        if step_index < len(sequence):
            icon_index = sequence[step_index]
        else:
            icon_index = step_index
        if icon_index >= len(icons):
            raise ValueError(
                f"ANI step {step_index} references icon index {icon_index}, "
                f"but only {len(icons)} embedded icon frame(s) are available"
            )
        icon_bytes = icons[icon_index]
        cur_info = parse_cur_bytes(icon_bytes)
        delay_jiffies = rates[step_index]
        frame_entries.append(
            {
                "step_index": step_index,
                "icon_index": icon_index,
                "delay_jiffies": delay_jiffies,
                "delay_ms": round(delay_jiffies * 1000 / 60),
                "entries": [
                    {
                        "index": entry["index"],
                        "width": entry["width"],
                        "height": entry["height"],
                        "colors": entry["colors"],
                        "hotspot_x": entry["hotspot_x"],
                        "hotspot_y": entry["hotspot_y"],
                        "image_size": entry["image_size"],
                    }
                    for entry in cur_info["entries"]
                ],
                "cur_bytes": icon_bytes,
            }
        )

    return {
        "type": "ani",
        "anih": anih,
        "frames_embedded": len(icons),
        "steps": steps,
        "frame_entries": frame_entries,
    }


def inspect_path(path: Path) -> dict:
    data = path.read_bytes()
    lower = path.suffix.lower()
    if lower == ".cur":
        return parse_cur_bytes(data)
    if lower == ".ani":
        return parse_ani_bytes(data)
    raise ValueError(f"unsupported file type: {path}")


def _run_icotool_extract(cur_path: Path, entry_index: int, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["icotool", "-x", f"--index={entry_index}", "--output", str(output_dir), str(cur_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pngs = sorted(output_dir.glob("*.png"))
    if not pngs:
        raise RuntimeError(f"icotool did not produce a PNG for {cur_path} entry {entry_index}")
    return pngs[0]


def extract_cur_entry_to_png(cur_path: Path, entry_index: int, output_png: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        extracted = _run_icotool_extract(cur_path, entry_index, Path(tmpdir))
        output_png.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(output_png))


def extract_cur_bytes_entry_to_png(cur_bytes: bytes, entry_index: int, output_png: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_cur = Path(tmpdir) / "frame.cur"
        tmp_cur.write_bytes(cur_bytes)
        extract_cur_entry_to_png(tmp_cur, entry_index, output_png)


def _extract_entries(
    entries: list[dict],
    output_dir: Path,
    safe_stem: str,
    frame_tag: str,
    delay_ms: int,
    source_index_key: str = "entry_index",
    cur_path: Path | None = None,
    cur_bytes: bytes | None = None,
) -> list[dict]:
    extracted_entries = []
    for entry in entries:
        output_png = output_dir / (
            f"{safe_stem}_{frame_tag}_e{entry['index']:02d}_{entry['width']}x{entry['height']}.png"
        )
        if cur_path is not None:
            extract_cur_entry_to_png(cur_path, entry["index"], output_png)
        elif cur_bytes is not None:
            extract_cur_bytes_entry_to_png(cur_bytes, entry["index"], output_png)
        else:
            raise ValueError("either cur_path or cur_bytes must be provided")

        extracted_entries.append(
            {
                "png": str(output_png),
                "width": entry["width"],
                "height": entry["height"],
                "colors": entry.get("colors"),
                "hotspot_x": entry["hotspot_x"],
                "hotspot_y": entry["hotspot_y"],
                "image_size": entry.get("image_size"),
                source_index_key: entry["index"],
                "delay_ms": delay_ms,
            }
        )

    extracted_entries.sort(key=lambda item: (item["width"], item["height"], item[source_index_key]))
    return extracted_entries


def extract_asset(path: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    info = inspect_path(path)
    safe_stem = sanitize_path_component(path.stem)
    metadata = {
        "format_version": METADATA_FORMAT_VERSION,
        "source": str(path),
        "asset_type": info["type"],
        "frames": [],
    }

    if info["type"] == "cur":
        frame_entries = _extract_entries(
            info["entries"],
            output_dir,
            safe_stem,
            "f000",
            delay_ms=50,
            cur_path=path,
        )
        metadata["frames"].append(
            {
                "frame_index": 0,
                "delay_ms": 50,
                "entries": frame_entries,
            }
        )
    else:
        for frame in info["frame_entries"]:
            frame_entries = _extract_entries(
                frame["entries"],
                output_dir,
                safe_stem,
                f"f{frame['step_index']:03d}",
                delay_ms=frame["delay_ms"],
                cur_bytes=frame["cur_bytes"],
            )
            metadata["frames"].append(
                {
                    "frame_index": frame["step_index"],
                    "icon_index": frame["icon_index"],
                    "delay_ms": frame["delay_ms"],
                    "entries": frame_entries,
                }
            )

    metadata_path = output_dir / f"{safe_stem}.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    metadata["metadata_json"] = str(metadata_path)
    return metadata


def command_inspect(paths: list[Path]) -> int:
    results = {}
    for path in paths:
        info = inspect_path(path)
        if info["type"] == "ani":
            serializable = dict(info)
            serializable["frame_entries"] = [
                {key: value for key, value in frame.items() if key != "cur_bytes"}
                for frame in info["frame_entries"]
            ]
            results[str(path)] = serializable
        else:
            results[str(path)] = info
    print(json.dumps(results, indent=2))
    return 0


def command_extract(paths: list[Path], output_dir: Path) -> int:
    results = {}
    for path in paths:
        results[str(path)] = extract_asset(path, output_dir / sanitize_path_component(path.stem))
    print(json.dumps(results, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="print JSON metadata")
    inspect_parser.add_argument("paths", nargs="+", type=Path)

    extract_parser = subparsers.add_parser("extract", help="extract PNG frames and metadata")
    extract_parser.add_argument("output_dir", type=Path)
    extract_parser.add_argument("paths", nargs="+", type=Path)

    args = parser.parse_args()
    if args.command == "inspect":
        return command_inspect(args.paths)
    return command_extract(args.paths, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
