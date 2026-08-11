from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
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

    def test_hook_actor_closeout_without_session_fails_closed(self) -> None:
        for actor in ("codex", "claude", "codebuddy"):
            with self.subTest(actor=actor):
                module = load_memoryctl()
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        ["memoryctl", "--actor", actor, "closeout", "--dry-run"],
                    ),
                    mock.patch.object(
                        module,
                        "resolve",
                        return_value=SimpleNamespace(
                            session_id="",
                            search_scope=actor,
                            hook_protocol="codex" if actor == "codex" else "claude",
                        ),
                    ),
                    mock.patch.object(module.subprocess, "run") as invoked,
                    contextlib.redirect_stderr(stderr),
                ):
                    self.assertEqual(module.main(), 2)
                invoked.assert_not_called()
                self.assertIn("requires an active host session", stderr.getvalue())

    def test_explicit_session_closeout_is_always_claim_scoped(self) -> None:
        module = load_memoryctl()
        completed = SimpleNamespace(returncode=0)
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "memoryctl",
                    "--actor",
                    "claude",
                    "closeout",
                    "--dry-run",
                    "--session-id",
                    "explicit-session",
                ],
            ),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)
        command = invoked.call_args.args[0]
        self.assertIn("--session-id", command)
        self.assertIn("explicit-session", command)
        self.assertIn("--claimed-only", command)

    def test_global_closeout_requires_explicit_global_flag(self) -> None:
        module = load_memoryctl()
        completed = SimpleNamespace(returncode=0)
        with (
            mock.patch.object(
                sys,
                "argv",
                ["memoryctl", "--actor", "claude", "closeout", "--global", "--dry-run"],
            ),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)
        self.assertNotIn("--claimed-only", invoked.call_args.args[0])

    def test_non_hook_actor_is_not_mistaken_for_hook_lifecycle(self) -> None:
        module = load_memoryctl()
        completed = SimpleNamespace(returncode=0)
        with (
            mock.patch.object(
                sys,
                "argv",
                ["memoryctl", "--actor", "cursor", "closeout", "--dry-run"],
            ),
            mock.patch.object(module.subprocess, "run", return_value=completed) as invoked,
        ):
            self.assertEqual(module.main(), 0)
        self.assertNotIn("--claimed-only", invoked.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
