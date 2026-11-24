from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory_env as env


class VenvLayoutTests(unittest.TestCase):
    """2026-11-24: the venv interpreter path is platform specific.

    doctor used to hard-code ``.venv/bin/python`` (the POSIX layout), so on
    Windows it always reported a missing interpreter even when the venv was
    installed correctly — and semantic checks were skipped for the wrong reason.
    """

    def test_windows_layout_uses_scripts_python_exe(self) -> None:
        with mock.patch.object(env, "WINDOWS", True):
            self.assertEqual(
                env.venv_python("C:/runtime"),
                Path("C:/runtime") / ".venv" / "Scripts" / "python.exe",
            )

    def test_posix_layout_uses_bin_python(self) -> None:
        with mock.patch.object(env, "WINDOWS", False):
            resolved = env.venv_python("/opt/runtime")
        # Compare the tail, not the absolute form: on Windows Path("/opt/...")
        # gains a drive letter, which is irrelevant to the layout contract.
        self.assertEqual(resolved.parts[-3:], (".venv", "bin", "python"), resolved)
        self.assertIn("runtime", str(resolved))

    def test_relative_and_tilde_roots_are_absolute(self) -> None:
        with mock.patch.object(env, "WINDOWS", False):
            resolved = env.venv_python("~/runtime")
        self.assertTrue(resolved.is_absolute(), resolved)
        self.assertNotIn("~", str(resolved))
        self.assertTrue(str(resolved).endswith(os.path.join(".venv", "bin", "python")), resolved)

    def test_doctor_default_follows_platform(self) -> None:
        import importlib

        import agent_memory_doctor as doctor

        try:
            with mock.patch.object(env, "WINDOWS", True):
                with mock.patch.object(
                    doctor, "ZVEC_PYTHON", env.venv_python(doctor.CONFIG_ROOT)
                ):
                    self.assertEqual(
                        Path(doctor.ZVEC_PYTHON).name, "python.exe", doctor.ZVEC_PYTHON
                    )
            with mock.patch.object(env, "WINDOWS", False):
                self.assertEqual(
                    Path(env.venv_python(doctor.CONFIG_ROOT)).name, "python"
                )
        finally:
            importlib.reload(doctor)


if __name__ == "__main__":
    unittest.main()
