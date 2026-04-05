#!/usr/bin/env python3
"""Shared workspace paths for CursorForge."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_TMP_ROOT = REPO_ROOT / "_tmp"

DEFAULT_WORK_ROOT = PROJECT_TMP_ROOT / "work"
DEFAULT_PREVIEW_ROOT_NAME = "_preview-cache"


def project_tmp_root() -> Path:
    return PROJECT_TMP_ROOT


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_project_tmp() -> Path:
    """Pin tempfile-backed scratch data to the repo-local tmp root."""

    ensure_directory(PROJECT_TMP_ROOT)
    os.environ["TMPDIR"] = str(PROJECT_TMP_ROOT)
    tempfile.tempdir = str(PROJECT_TMP_ROOT)
    return PROJECT_TMP_ROOT
