from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from tests.subprocess_env import isolated_subprocess_env


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_runtime.py"


class RuntimeInstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_preserves_local_adapter(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            scripts = root / "scripts"
            local_adapter = scripts / "local_adapter.py"

            first = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            payload = json.loads(first.stdout)
            self.assertIn("memoryctl", payload["changed"])
            self.assertIn("agent_memory_lock.py", payload["changed"])
            self.assertIn("agent_memory_paths.py", payload["changed"])
            self.assertIn("requirements-vector.lock", payload["changed"])
            self.assertTrue((scripts / "agent_memory_lock.py").is_file())
            self.assertTrue((scripts / "agent_memory_paths.py").is_file())
            self.assertTrue((root / "requirements-vector.lock").is_file())
            local_adapter.write_text("LOCAL = True\n", encoding="utf-8")

            runtime_env = isolated_subprocess_env(
                {
                    "AGENT_MEMORY_CONFIG_ROOT": str(root),
                    "AGENT_MEMORY_ROOT": str(REPO_ROOT / "templates" / "vault"),
                    "AGENT_MEMORY_GIT_ROOT": str(REPO_ROOT),
                }
            )
            runtime = subprocess.run(
                [
                    sys.executable,
                    str(scripts / "memoryctl"),
                    "--actor",
                    "codex",
                    "check",
                    "--skip-state-db",
                ],
                cwd=root,
                env=runtime_env,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
            self.assertIn("agent_memory_check=ok", runtime.stdout)

            verify = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--verify", "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertTrue(json.loads(verify.stdout)["ok"])

            second = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(json.loads(second.stdout)["changed"], [])
            self.assertEqual(local_adapter.read_text(encoding="utf-8"), "LOCAL = True\n")

    def test_install_repairs_config_and_existing_sqlite_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve()
            config_dir = root / "config"
            config_dir.mkdir()
            config_file = config_dir / "agent-memory.toml"
            config_file.write_text("memory_root = '/tmp/example'\n", encoding="utf-8")
            state_db = root / "state.sqlite"
            connection = sqlite3.connect(state_db)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('ok')")
            connection.commit()
            for path in (root, config_file, state_db, Path(f"{state_db}-wal"), Path(f"{state_db}-shm")):
                path.chmod(0o755 if path == root else 0o644)

            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            payload = json.loads(installed.stdout)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(state_db.stat().st_mode), 0o600)
                for suffix in ("-wal", "-shm"):
                    self.assertEqual(stat.S_IMODE(Path(f"{state_db}{suffix}").stat().st_mode), 0o600)
            else:
                self.assertEqual(payload["permissions"]["permission_model"], "windows_acl_unverified")
                self.assertIn("windows_acl_unverified", payload["permissions"]["warnings"])
            connection.close()


if __name__ == "__main__":
    unittest.main()
