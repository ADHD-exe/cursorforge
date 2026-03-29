#!/usr/bin/env python3
"""Shared cursor slot definitions and heuristics."""

from __future__ import annotations

import re


DEFAULT_CURSOR_SIZES = [24, 32, 36, 48, 64, 96, 128, 192]
DEFAULT_SCALE_FILTER = "point"
SCALE_FILTER_CHOICES = ("point", "mitchell", "lanczos")


SLOT_DEFS = [
    {
        "key": "default_pointer",
        "label": "Default Pointer",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "arrow",
            "center-main",
            "center_main",
            "default",
            "left-main",
            "left_ptr",
            "right-main",
            "right_ptr",
            "top_left_arrow",
            "wayland-cursor",
        ],
        "keywords": ("arrow", "default", "normal", "pointer", "cursor", "start"),
    },
    {
        "key": "help",
        "label": "Help / Context",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "context-menu",
            "dnd-ask",
            "help",
            "left_ptr_help",
            "question_arrow",
            "top_right_arrow",
            "whats_this",
        ],
        "keywords": ("help", "question", "context"),
    },
    {
        "key": "progress",
        "label": "Progress",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "half-busy",
            "left_ptr_watch",
            "progress",
        ],
        "keywords": ("progress", "working", "appstart", "appstarting", "start"),
    },
    {
        "key": "wait",
        "label": "Wait",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "clock",
            "wait",
            "watch",
        ],
        "keywords": ("wait", "busy", "loading", "load", "hourglass"),
    },
    {
        "key": "text",
        "label": "Text / I-Beam",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "horizontal-text",
            "ibeam",
            "text",
            "vertical-text",
            "xterm",
        ],
        "keywords": ("text", "beam", "ibeam"),
    },
    {
        "key": "link_alias",
        "label": "Link / Alias",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "alias",
            "copy",
            "dnd-copy",
            "dnd-link",
            "link",
            "scan",
        ],
        "keywords": ("link", "alias", "copy", "shortcut"),
    },
    {
        "key": "hand",
        "label": "Hand",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "hand",
            "hand1",
            "hand2",
            "pointer",
            "pointer2",
            "pointing_hand",
        ],
        "keywords": ("hand",),
    },
    {
        "key": "move",
        "label": "Move / Grab",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "all-scroll",
            "closedhand",
            "dnd-move",
            "dragging",
            "fleur",
            "grabbing",
            "move",
            "openhand",
            "size_all",
        ],
        "keywords": ("move", "grab", "grabbing", "drag", "allscroll", "fleur"),
    },
    {
        "key": "forbidden",
        "label": "Forbidden / No Drop",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "circle",
            "crossed_circle",
            "dnd-no-drop",
            "dnd-none",
            "forbidden",
            "no-drop",
            "not-allowed",
            "x-cursor",
        ],
        "keywords": ("no", "nodrop", "forbidden", "notallowed", "stop", "unavailable"),
    },
    {
        "key": "resize_horizontal",
        "label": "Resize Horizontal",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "col-resize",
            "e-resize",
            "ew-resize",
            "h_double_arrow",
            "left-arrow",
            "left_side",
            "right-arrow",
            "right_side",
            "sb_h_double_arrow",
            "size_hor",
            "split_h",
            "w-resize",
        ],
        "keywords": ("hori", "horizontal", "leftright", "ew", "sizehor", "we"),
    },
    {
        "key": "resize_vertical",
        "label": "Resize Vertical",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "bottom_side",
            "down-arrow",
            "n-resize",
            "ns-resize",
            "row-resize",
            "s-resize",
            "sb_up_arrow",
            "sb_v_double_arrow",
            "size_ver",
            "split_v",
            "top_side",
            "up-arrow",
            "up_arrow",
            "v_double_arrow",
            "ver-resize",
        ],
        "keywords": ("vert", "vertical", "updown", "ns", "sizever"),
    },
    {
        "key": "resize_diag_back",
        "label": "Resize Diag Back",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "bottom_left_corner",
            "fd_double_arrow",
            "ne-resize",
            "nesw-resize",
            "size_bdiag",
            "sw-resize",
            "top_right_corner",
        ],
        "keywords": ("diag1", "bdiag", "nesw", "swne"),
    },
    {
        "key": "resize_diag_forward",
        "label": "Resize Diag Forward",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "bd_double_arrow",
            "bottom_right_corner",
            "nw-resize",
            "nwse-resize",
            "se-resize",
            "size_fdiag",
            "top_left_corner",
        ],
        "keywords": ("diag2", "fdiag", "nwse", "senw"),
    },
    {
        "key": "crosshair",
        "label": "Crosshair / Target",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "cell",
            "center_ptr",
            "cross",
            "cross_reverse",
            "crosshair",
            "diamond_cross",
            "dot",
            "dot_box_mask",
            "dotbox",
            "draped_box",
            "icon",
            "plus",
            "target",
            "tcross",
        ],
        "keywords": ("cross", "crosshair", "precision", "target"),
    },
    {
        "key": "pen",
        "label": "Pen / Draft",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "color-picker",
            "draft",
            "draft_large",
            "draft_small",
            "pencil",
        ],
        "keywords": ("pen", "pencil", "write", "draft", "color"),
    },
    {
        "key": "special_misc",
        "label": "Special Misc",
        "allowed_extensions": (".ani", ".cur", ".png", ".json"),
        "roles": [
            "kill",
            "pirate",
            "zoom-in",
            "zoom-out",
            "zoom_in",
            "zoom_out",
        ],
        "keywords": ("person", "pirate", "zoom", "kill", "pin"),
    },
]


WINDOWS_ROLE_TO_SLOT = {
    "arrow": "default_pointer",
    "pointer": "default_pointer",
    "help": "help",
    "start": "progress",
    "work": "progress",
    "wait": "wait",
    "busy": "wait",
    "cross": "crosshair",
    "beam": "text",
    "text": "text",
    "pen": "pen",
    "hand": "pen",
    "no": "forbidden",
    "unavailable": "forbidden",
    "vert": "resize_vertical",
    "vertical": "resize_vertical",
    "hori": "resize_horizontal",
    "horz": "resize_horizontal",
    "horizontal": "resize_horizontal",
    "dgn1": "resize_diag_back",
    "dgn2": "resize_diag_forward",
    "move": "move",
    "link": "link_alias",
    "pin": "special_misc",
    "person": "special_misc",
}


UNUSED = "-- unused --"
SLOT_LABELS = [UNUSED] + [slot["label"] for slot in SLOT_DEFS]
SLOT_BY_LABEL = {slot["label"]: slot for slot in SLOT_DEFS}
SLOT_BY_KEY = {slot["key"]: slot for slot in SLOT_DEFS}


def normalized_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", name.lower()) if token]


def flatten_name(name: str) -> str:
    return "".join(normalized_tokens(name))


def score_slot_match(name: str, slot: dict) -> int:
    tokens = normalized_tokens(name)
    flat = flatten_name(name)
    score = 0

    for keyword in slot.get("keywords", ()):
        keyword_flat = flatten_name(keyword)
        if keyword in tokens:
            score += 5
        elif keyword_flat and keyword_flat in flat:
            score += 3

    for token in normalized_tokens(slot["label"]):
        if token in tokens:
            score += 2

    return score
