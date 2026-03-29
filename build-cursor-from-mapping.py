#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys

SCRIPT = Path(__file__).resolve().parent / 'tools' / 'build_from_slot_mapping.py'


def main() -> None:
    sys.argv[0] = str(SCRIPT)
    runpy.run_path(str(SCRIPT), run_name='__main__')


if __name__ == "__main__":
    main()
