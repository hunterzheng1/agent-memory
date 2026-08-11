from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def load_memoryctl():
    path = SCRIPTS_ROOT / "memoryctl"
    loader = importlib.machinery.SourceFileLoader("memoryctl_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class MemoryctlCommandTests(unittest.TestCase):
    def test_zvec_uses_configured_python_interpreter(self) -> None:
        module = load_memoryctl()
        configured_python = r"C:\vector-runtime\python.exe"
        completed = SimpleNamespace(returncode=0)

        with mock.patch.dict(
            os.environ,
            {"AGENT_MEMORY_ZVEC_PYTHON": configured_python},
            clear=True,
        ), mock.patch.object(
            sys,
            "argv",
            ["memoryctl", "--actor", "codex", "zvec", "--report"],
        ), mock.patch.object(
            module.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertEqual(module.main(), 0)

        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [configured_python, str(module.COMMANDS["zvec"]), "--report"],
        )

    def test_non_zvec_command_uses_current_python_interpreter(self) -> None:
        module = load_memoryctl()
        completed = SimpleNamespace(returncode=0)

        with mock.patch.dict(
            os.environ,
            {"AGENT_MEMORY_ZVEC_PYTHON": r"C:\vector-runtime\python.exe"},
            clear=True,
        ), mock.patch.object(
            sys,
            "argv",
            ["memoryctl", "--actor", "codex", "check", "--json"],
        ), mock.patch.object(
            module.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertEqual(module.main(), 0)

        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [sys.executable, str(module.COMMANDS["check"]), "--json"],
        )


if __name__ == "__main__":
    unittest.main()
