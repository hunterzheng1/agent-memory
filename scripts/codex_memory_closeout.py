#!/usr/bin/env python3
"""Compatibility wrapper — delegates to agent_memory_closeout.py."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).with_name("agent_memory_closeout.py")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sys.argv = [str(TARGET), *args]
    runpy.run_path(str(TARGET), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
