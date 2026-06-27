#!/usr/bin/env python3
"""Backward-compat wrapper. Real implementation in agent_memory_check.py."""
import importlib.util
import sys
from pathlib import Path

_real = Path(__file__).resolve().parent / "agent_memory_check.py"
spec = importlib.util.spec_from_file_location("agent_memory_check_module", _real)
if not spec or not spec.loader:
    raise RuntimeError(f"cannot_load_wrapper {_real}")
_mod = importlib.util.module_from_spec(spec)
sys.modules["agent_memory_check_module"] = _mod
spec.loader.exec_module(_mod)
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
if __name__ == "__main__":
    raise SystemExit(_mod.main())
