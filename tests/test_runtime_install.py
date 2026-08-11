from __future__ import annotations

import json
import os
import shutil
import subprocess
import sqlite3
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.subprocess_env import isolated_subprocess_env


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_runtime.py"


def make_directory_reparse(link: Path, target: Path) -> None:
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        if created.returncode != 0:
            raise OSError(created.stdout + created.stderr)
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_directory_reparse(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


class RuntimeInstallTests(unittest.TestCase):
    def run_doctor_with_config_reads_forbidden(
        self,
        doctor_path: Path,
        config_root: Path,
    ) -> subprocess.CompletedProcess[str]:
        probe = textwrap.dedent(
            """
            import os
            import runpy
            import sys

            doctor_path = sys.argv[1]
            sys.path.insert(0, os.path.dirname(doctor_path))
            import agent_memory_env

            def forbidden_config_read():
                raise RuntimeError("external_config_read")

            agent_memory_env.load_config = forbidden_config_read
            sys.argv = [doctor_path, "--json"]
            runpy.run_path(doctor_path, run_name="__main__")
            """
        )
        return subprocess.run(
            [sys.executable, "-c", probe, str(doctor_path)],
            cwd=config_root.parent,
            env=isolated_subprocess_env(
                {
                    "AGENT_MEMORY_CONFIG_ROOT": str(config_root),
                    "AGENT_MEMORY_CONFIG_FILE": str(
                        config_root / "config" / "agent-memory.toml"
                    ),
                    "AGENT_MEMORY_STATE_DB": str(config_root / "state.sqlite"),
                }
            ),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def test_doctor_rejects_config_root_reparse_before_loading_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            external = base / "external-runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(external), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            config_file = external / "config" / "agent-memory.toml"
            config_file.write_text("memory_root = 'must-not-read'\n", encoding="utf-8")
            sentinel = external / "config" / "external-sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            runtime_link = base / "runtime-link"
            try:
                make_directory_reparse(runtime_link, external)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                doctor = self.run_doctor_with_config_reads_forbidden(
                    runtime_link / "scripts" / "agent_memory_doctor.py",
                    runtime_link,
                )

                self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
                self.assertNotIn("external_config_read", doctor.stderr)
                payload = json.loads(doctor.stdout)
                self.assertEqual(
                    [item["name"] for item in payload["checks"]],
                    ["runtime_config_paths"],
                )
                self.assertIn(
                    "reparse",
                    json.dumps(payload["checks"][0]["detail"]["unsafe_paths"]).lower(),
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            finally:
                remove_directory_reparse(runtime_link)

    def test_doctor_rejects_config_directory_reparse_before_loading_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            runtime = base / "runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            external_config = base / "external-config"
            shutil.move(str(runtime / "config"), str(external_config))
            config_file = external_config / "agent-memory.toml"
            config_file.write_text("memory_root = 'must-not-read'\n", encoding="utf-8")
            sentinel = external_config / "external-sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            config_link = runtime / "config"
            try:
                make_directory_reparse(config_link, external_config)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                doctor = self.run_doctor_with_config_reads_forbidden(
                    runtime / "scripts" / "agent_memory_doctor.py",
                    runtime,
                )

                self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
                self.assertNotIn("external_config_read", doctor.stderr)
                payload = json.loads(doctor.stdout)
                self.assertEqual(
                    [item["name"] for item in payload["checks"]],
                    ["runtime_config_paths"],
                )
                self.assertIn(
                    "reparse",
                    json.dumps(payload["checks"][0]["detail"]["unsafe_paths"]).lower(),
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            finally:
                remove_directory_reparse(config_link)

    def test_install_rejects_managed_parent_reparse_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            runtime = base / "runtime"
            runtime.mkdir()
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            link = runtime / "scripts"
            try:
                make_directory_reparse(link, outside)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                result = subprocess.run(
                    [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"], "StateSecurityError")
                self.assertIn("reparse", payload["detail"].lower())
                self.assertEqual(
                    sorted(path.name for path in outside.iterdir()),
                    ["sentinel.txt"],
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
                self.assertFalse((runtime / "config" / "runtime-manifest.json").exists())
            finally:
                remove_directory_reparse(link)

    def test_verify_and_doctor_reject_template_parent_reparse_without_traversal(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            runtime = base / "runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            shutil.rmtree(runtime / "templates")
            outside = base / "outside-templates"
            outside.mkdir()
            sentinel = outside / "AGENTS.md"
            sentinel.write_text("external sentinel\n", encoding="utf-8")
            link = runtime / "templates"
            try:
                make_directory_reparse(link, outside)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                verified = subprocess.run(
                    [
                        sys.executable,
                        str(INSTALLER),
                        "--config-root",
                        str(runtime),
                        "--verify",
                        "--json",
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(verified.returncode, 2, verified.stdout + verified.stderr)
                verify_payload = json.loads(verified.stdout)
                self.assertTrue(verify_payload["unsafe_paths"])
                self.assertIn("reparse", json.dumps(verify_payload["unsafe_paths"]).lower())
                self.assertIn(str(link), verify_payload["symlinked"])

                doctor_env = isolated_subprocess_env(
                    {
                        "AGENT_MEMORY_CONFIG_ROOT": str(runtime),
                        "AGENT_MEMORY_STATE_DB": str(runtime / "missing-state.sqlite"),
                    }
                )
                doctor = subprocess.run(
                    [sys.executable, str(runtime / "scripts" / "agent_memory_doctor.py"), "--json"],
                    cwd=runtime,
                    env=doctor_env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
                doctor_payload = json.loads(doctor.stdout)
                manifest_check = next(
                    item for item in doctor_payload["checks"] if item["name"] == "runtime_manifest"
                )
                self.assertEqual(manifest_check["status"], "fail")
                self.assertTrue(manifest_check["detail"]["unsafe_paths"])
                self.assertIn(
                    "reparse",
                    json.dumps(manifest_check["detail"]["unsafe_paths"]).lower(),
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "external sentinel\n")
            finally:
                remove_directory_reparse(link)

    def test_doctor_rejects_scripts_parent_reparse_before_other_checks(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            runtime = base / "runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            outside_scripts = base / "outside-scripts"
            shutil.move(str(runtime / "scripts"), str(outside_scripts))
            link = runtime / "scripts"
            try:
                make_directory_reparse(link, outside_scripts)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                doctor = self.run_doctor_with_config_reads_forbidden(
                    link / "agent_memory_doctor.py",
                    runtime,
                )

                self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
                self.assertNotIn("external_config_read", doctor.stderr)
                payload = json.loads(doctor.stdout)
                self.assertEqual(
                    [item["name"] for item in payload["checks"]],
                    ["runtime_config_paths"],
                )
                self.assertIn(
                    "reparse",
                    json.dumps(payload["checks"][0]["detail"]["unsafe_paths"]).lower(),
                )
            finally:
                remove_directory_reparse(link)

    def test_template_manifest_inventory_and_disk_set_are_exact(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            runtime = Path(raw_root).resolve() / "runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            manifest_path = runtime / "config" / "runtime-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            inventory = manifest["template_inventory"]
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["template_count"], len(inventory))
            self.assertEqual(set(manifest["template_files"]), set(inventory))
            self.assertEqual(
                manifest["integrity_model"],
                "sha256_drift_detection_not_authentication",
            )

            removed_name = "templates/vault/STRUCTURE.md"
            manifest["template_files"].pop(removed_name)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (runtime / removed_name).unlink()
            verified = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--config-root",
                    str(runtime),
                    "--verify",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(verified.returncode, 2, verified.stdout + verified.stderr)
            payload = json.loads(verified.stdout)
            self.assertIn(removed_name, payload["closure"]["template_hash_missing"])
            self.assertIn(removed_name, payload["closure"]["template_disk_missing"])

    def test_doctor_uses_the_canonical_runtime_inventory(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            config_root = Path(raw_root).resolve() / "runtime"
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,sys;"
                        f"sys.path.insert(0,{str(REPO_ROOT / 'scripts')!r});"
                        "import agent_memory_doctor as doctor;"
                        "import install_runtime as runtime;"
                        "print(json.dumps({"
                        "'same_object': doctor.RUNTIME_FILES is runtime.CORE_FILES,"
                        "'same_values': doctor.RUNTIME_FILES == runtime.CORE_FILES"
                        "}))"
                    ),
                ],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(
                    {
                        "AGENT_MEMORY_CONFIG_ROOT": str(config_root),
                        "AGENT_MEMORY_STATE_DB": str(config_root / "state.sqlite"),
                    }
                ),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
            self.assertEqual(
                json.loads(probe.stdout),
                {"same_object": True, "same_values": True},
            )

    def test_verify_rejects_unexpected_disk_template(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            runtime = Path(raw_root).resolve() / "runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(runtime), "--json"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            unexpected_name = "templates/vault/unexpected-local-template.md"
            (runtime / unexpected_name).write_text("unexpected\n", encoding="utf-8")

            verified = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--config-root",
                    str(runtime),
                    "--verify",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(verified.returncode, 2, verified.stdout + verified.stderr)
            payload = json.loads(verified.stdout)
            self.assertIn(unexpected_name, payload["closure"]["template_disk_unexpected"])

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
