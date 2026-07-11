"""codex_memory_*.py 兼容包装测试。"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    path = SCRIPT_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name.replace(".py", "")] = module
    spec.loader.exec_module(module)
    return module


class TestCompatWrappers(unittest.TestCase):
    def test_codex_wrappers_export_main(self):
        for script in (
            "codex_memory_search.py",
            "codex_memory_closeout.py",
            "codex_memory_audit.py",
            "codex_memory_audit_autorun.py",
        ):
            with self.subTest(script=script):
                module = load_script(script)
                self.assertTrue(callable(module.main))

    def test_agent_evolution_initializes_required_state_tables(self):
        """The canonical entrypoint must not be a wrapper that imports itself."""
        script = SCRIPT_DIR / "agent_evolution.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            (vault / "agent" / "cases").mkdir(parents=True)
            state_db = root / "state.sqlite"
            env = os.environ | {
                "AGENT_MEMORY_ROOT": str(vault),
                "AGENT_MEMORY_STATE_DB": str(state_db),
            }
            result = subprocess.run(
                [sys.executable, str(script), "--init", "--scan", "--report"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            conn = sqlite3.connect(state_db)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            self.assertTrue({"memory_files", "agent_case_state", "reminders"} <= tables)

    def test_zvec_search_uses_configured_python_interpreter(self):
        configured_python = r"C:\\memory-runtime\\python.exe"
        previous = os.environ.get("AGENT_MEMORY_ZVEC_PYTHON")
        os.environ["AGENT_MEMORY_ZVEC_PYTHON"] = configured_python
        try:
            module = load_script("agent_memory_search.py")
        finally:
            if previous is None:
                os.environ.pop("AGENT_MEMORY_ZVEC_PYTHON", None)
            else:
                os.environ["AGENT_MEMORY_ZVEC_PYTHON"] = previous

        captured: list[list[str]] = []

        def fake_run(command, **_kwargs):
            captured.append(command)
            return SimpleNamespace(returncode=0, stdout='{"query":"x","results":[]}', stderr="")

        args = SimpleNamespace(no_zvec=False, query="x", limit=3, zvec_timeout=1, zvec_max_distance=0.72)
        with patch.object(module.subprocess, "run", side_effect=fake_run):
            rows, warnings = module.zvec_search(args)

        self.assertEqual(rows, [])
        self.assertEqual(warnings, [])
        self.assertEqual(captured[0][0], configured_python)
        self.assertEqual(captured[0][1], str(module.ZVEC_SCRIPT))


if __name__ == "__main__":
    unittest.main()
