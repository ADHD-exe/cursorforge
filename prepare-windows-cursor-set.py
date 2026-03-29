#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys

SCRIPT = Path(__file__).resolve().parent / 'tools' / 'prepare_windows_cursor_set.py'
sys.argv[0] = str(SCRIPT)
runpy.run_path(str(SCRIPT), run_name='__main__')
