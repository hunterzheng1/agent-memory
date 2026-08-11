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
    def test_install_rejects_managed_file_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            scripts = root / "scripts"
            scripts.mkdir()
            outside = root / "outside.py"
            outside.write_text("DO_NOT_CHANGE = True\n", encoding="utf-8")
            managed = scripts / "agent_memory_check.py"
            try:
                managed.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            result = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"], "StateSecurityError")
            self.assertIn("must not be a symlink", payload["detail"])
            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE = True\n")

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
            for adapter in (
                "install-windows.ps1",
                "stop-hook.ps1",
                "install-codex-hook.ps1",
                "audit-task.ps1",
            ):
                self.assertIn(adapter, payload["changed"])
                self.assertTrue((scripts / adapter).is_file())
            self.assertIn("requirements-vector.lock", payload["changed"])
            self.assertTrue((scripts / "agent_memory_lock.py").is_file())
            self.assertTrue((scripts / "agent_memory_paths.py").is_file())
            self.assertTrue((root / "requirements-vector.lock").is_file())
            self.assertTrue((root / "templates" / "vault" / "AGENTS.md").is_file())
            self.assertEqual(
                (root / "templates" / "vault" / ".gitignore").read_text(encoding="utf-8"),
                ".obsidian/\n",
            )
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

            (root / "templates" / "vault" / ".gitignore").write_text(
                "tampered\n", encoding="utf-8"
            )
            drift = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--verify", "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(drift.returncode, 2, drift.stdout + drift.stderr)
            self.assertIn("templates/vault/.gitignore", json.loads(drift.stdout)["template_mismatched"])

            repaired = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)

            manifest_path = root / "config" / "runtime-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"].pop("stop-hook.ps1")
            manifest["template_files"]["../outside-template"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            incomplete = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--verify", "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(incomplete.returncode, 2, incomplete.stdout + incomplete.stderr)
            incomplete_payload = json.loads(incomplete.stdout)
            self.assertIn("stop-hook.ps1", incomplete_payload["closure"]["core_missing"])
            self.assertIn(
                "../outside-template",
                incomplete_payload["closure"]["template_unsafe_names"],
            )

            manifest_repair = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(manifest_repair.returncode, 0, manifest_repair.stdout + manifest_repair.stderr)

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

    def test_installed_runtime_can_bootstrap_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "运行 runtime with spaces"
            outside = root / "outside cwd"
            vault = root / "记忆 vault with spaces"
            outside.mkdir()

            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                cwd=REPO_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

            bootstrap = subprocess.run(
                [
                    sys.executable,
                    str(runtime / "scripts" / "bootstrap.py"),
                    "--memory-root",
                    str(vault),
                    "--init-git",
                ],
                cwd=outside,
                env=isolated_subprocess_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("git_baseline=created", bootstrap.stdout)
            self.assertTrue((vault / "AGENTS.md").is_file())
            self.assertTrue((vault / ".git" / "HEAD").is_file())

            verify = subprocess.run(
                [sys.executable, str(runtime / "scripts" / "install_runtime.py"), "--config-root", str(runtime), "--verify", "--json"],
                cwd=outside,
                env=isolated_subprocess_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertTrue(json.loads(verify.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
