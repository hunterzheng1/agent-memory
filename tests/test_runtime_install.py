from __future__ import annotations

import errno
import json
import hashlib
import os
import shutil
import subprocess
import sqlite3
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tests.subprocess_env import isolated_subprocess_env


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_runtime.py"


def windows_short_path(path: Path) -> Path | None:
    if os.name != "nt":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path_name = kernel32.GetShortPathNameW
    get_short_path_name.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path_name.restype = ctypes.c_uint32
    required = get_short_path_name(str(path), None, 0)
    if required == 0:
        return None
    buffer = ctypes.create_unicode_buffer(required)
    written = get_short_path_name(str(path), buffer, required)
    if written == 0 or written >= required:
        return None
    shortened = Path(buffer.value)
    if os.path.normcase(str(shortened)) == os.path.normcase(str(path)):
        return None
    return shortened


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
    def test_directory_fsync_only_ignores_explicitly_unsupported_errors(self) -> None:
        probe = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            scripts_root, raw_errno, expectation = sys.argv[1:]
            sys.path.insert(0, scripts_root)
            import install_runtime

            error_number = int(raw_errno)
            probe_path = Path(".")
            install_runtime.os.name = "posix"
            install_runtime.os.open = lambda *_args, **_kwargs: 123
            install_runtime.os.close = lambda _descriptor: None
            def fail_fsync(_descriptor):
                raise OSError(error_number, "injected directory fsync failure")
            install_runtime.os.fsync = fail_fsync
            try:
                install_runtime._fsync_directory(probe_path)
            except OSError as exc:
                if expectation == "raise" and exc.errno == error_number:
                    raise SystemExit(0)
                raise SystemExit(2)
            raise SystemExit(0 if expectation == "ignore" else 3)
            """
        )
        for error_number, expectation in (
            (errno.EIO, "raise"),
            (errno.EINVAL, "ignore"),
            (errno.ENOTSUP, "ignore"),
        ):
            with self.subTest(error_number=error_number):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        probe,
                        str(REPO_ROOT / "scripts"),
                        str(error_number),
                        expectation,
                    ],
                    cwd=REPO_ROOT,
                    env=isolated_subprocess_env(),
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )

    @unittest.skipUnless(os.name == "nt", "Win32 durability primitives are Windows-specific")
    def test_windows_durability_primitives_and_runtime_contract(self) -> None:
        probe = textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            scripts_root, raw_root = sys.argv[1:]
            sys.path.insert(0, scripts_root)
            import install_runtime

            root = Path(raw_root)
            source = root / "source.bin"
            target = root / "target.bin"
            source.write_bytes(b"durable-probe")
            install_runtime._fsync_file(source)
            install_runtime._fsync_directory(root)
            install_runtime._durable_replace(source, target)
            if source.exists() or target.read_bytes() != b"durable-probe":
                raise SystemExit(3)
            payload = install_runtime.install(root / "runtime", False)
            manifest = json.loads(
                (root / "runtime" / "config" / "runtime-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            print(json.dumps({"payload": payload, "manifest": manifest}))
            """
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(REPO_ROOT / "scripts"), raw_root],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        observed = json.loads(completed.stdout)
        for subject in (observed["payload"], observed["manifest"]):
            self.assertIs(subject["process_crash_recovery"], True)
            self.assertEqual(subject["power_loss_durability"], "verified")

    @unittest.skipUnless(os.name == "nt", "Win32 durability primitives are Windows-specific")
    def test_windows_durability_failure_is_fail_closed(self) -> None:
        probe = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            scripts_root, raw_root = sys.argv[1:]
            sys.path.insert(0, scripts_root)
            import install_runtime

            def unsupported(*_args, **_kwargs):
                raise install_runtime.WindowsDurabilityError(
                    "windows_durability_unsupported: injected FlushFileBuffers failure"
                )
            install_runtime._windows_flush_path = unsupported
            install_runtime.install(Path(raw_root) / "runtime", False)
            """
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(REPO_ROOT / "scripts"), raw_root],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(
            "windows_durability_unsupported",
            completed.stdout + completed.stderr,
        )

    @unittest.skipUnless(os.name == "nt", "Win32 durability primitives are Windows-specific")
    def test_windows_install_routes_critical_files_through_durable_wrappers(self) -> None:
        probe = textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path

            scripts_root, raw_root = sys.argv[1:]
            sys.path.insert(0, scripts_root)
            import install_runtime

            root = Path(raw_root) / "runtime"
            install_runtime.install(root, False)
            original_replace = install_runtime._durable_replace
            original_fsync_file = install_runtime._fsync_file
            replacements = []
            flushed = []
            def observed_replace(source, target):
                replacements.append(os.fspath(target))
                return original_replace(source, target)
            def observed_fsync(path):
                flushed.append(os.fspath(path))
                return original_fsync_file(path)
            install_runtime._durable_replace = observed_replace
            install_runtime._fsync_file = observed_fsync
            install_runtime.install(root, False)
            print(json.dumps({"replacements": replacements, "flushed": flushed}))
            """
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(REPO_ROOT / "scripts"), raw_root],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            runtime_root = Path(raw_root) / "runtime"
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        observed = json.loads(completed.stdout)
        replacement_targets = {
            os.path.normcase(os.path.normpath(path)) for path in observed["replacements"]
        }
        self.assertIn(
            os.path.normcase(str(runtime_root / ".runtime-install-journal.json")),
            replacement_targets,
        )
        self.assertIn(
            os.path.normcase(str(runtime_root / "scripts" / "install_runtime.py")),
            replacement_targets,
        )
        self.assertIn(
            os.path.normcase(str(runtime_root / "config" / "runtime-manifest.json")),
            replacement_targets,
        )
        self.assertTrue(
            any(f"{os.sep}.rollback{os.sep}" in path for path in observed["flushed"]),
            observed,
        )

    @unittest.skipUnless(os.name == "nt", "Win32 durability primitives are Windows-specific")
    def test_windows_durability_uses_required_win32_flags_and_checks_flush(self) -> None:
        probe = textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            scripts_root = sys.argv[1]
            sys.path.insert(0, scripts_root)
            import install_runtime

            calls = []
            class Function:
                def __init__(self, name, result):
                    self.name = name
                    self.result = result
                    self.argtypes = None
                    self.restype = None
                def __call__(self, *args):
                    calls.append([self.name, *args])
                    return self.result
            class Kernel32:
                def __init__(self):
                    self.CreateFileW = Function("CreateFileW", 123)
                    self.FlushFileBuffers = Function("FlushFileBuffers", 1)
                    self.CloseHandle = Function("CloseHandle", 1)
                    self.MoveFileExW = Function("MoveFileExW", 1)
            kernel32 = Kernel32()
            install_runtime.ctypes.WinDLL = lambda *_args, **_kwargs: kernel32
            install_runtime._windows_flush_path(Path(r"C:\\durability-probe"), directory=True)
            install_runtime._windows_replace_write_through(
                Path(r"C:\\source.tmp"), Path(r"C:\\target.json")
            )
            kernel32.FlushFileBuffers.result = 0
            rejected = False
            try:
                install_runtime._windows_flush_path(
                    Path(r"C:\\durability-probe"), directory=True
                )
            except install_runtime.WindowsDurabilityError:
                rejected = True
            print(json.dumps({"calls": calls, "rejected": rejected}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(REPO_ROOT / "scripts")],
            cwd=REPO_ROOT,
            env=isolated_subprocess_env(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        observed = json.loads(completed.stdout)
        create_call = next(call for call in observed["calls"] if call[0] == "CreateFileW")
        self.assertEqual(create_call[2], 0x40000000)
        self.assertEqual(create_call[3], 0x00000007)
        self.assertEqual(create_call[5], 3)
        self.assertEqual(create_call[6] & 0x02000000, 0x02000000)
        self.assertEqual(create_call[6] & 0x80000000, 0x80000000)
        move_call = next(call for call in observed["calls"] if call[0] == "MoveFileExW")
        self.assertEqual(move_call[3], 0x00000009)
        self.assertTrue(observed["rejected"])

    def make_verified_old_runtime(self, root: Path) -> dict[str, bytes]:
        installed = subprocess.run(
            [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        manifest_path = root / "config" / "runtime-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name in list(manifest["files"])[:4]:
            target = root / "scripts" / name
            old_bytes = target.read_bytes() + b"\n# prior verified runtime\n"
            target.write_bytes(old_bytes)
            manifest["files"][name] = hashlib.sha256(old_bytes).hexdigest()
        manifest["source_commit"] = "prior-verified-runtime"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        local_adapter = root / "scripts" / "local_adapter.py"
        local_adapter.write_bytes(b"LOCAL = True\n")
        verified = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--config-root",
                str(root),
                "--verify",
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        return self.snapshot_runtime_files(root)

    @staticmethod
    def snapshot_runtime_files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def run_runtime_install_with_fault(
        self,
        root: Path,
        fault: str,
    ) -> subprocess.CompletedProcess[str]:
        probe = textwrap.dedent(
            """
            import os
            import sys

            scripts_root, config_root, fault = sys.argv[1:]
            sys.path.insert(0, scripts_root)
            import install_runtime

            original_copy2 = install_runtime.shutil.copy2
            original_replace = install_runtime._durable_replace
            original_write_journal = install_runtime._write_runtime_journal
            copy_count = 0
            publish_count = 0
            failed = False
            rollback_failed = False

            def injected_copy2(source, target, *args, **kwargs):
                global copy_count, failed
                copy_count += 1
                if fault == "hard-exit-stage-copy-2" and copy_count == 2 and not failed:
                    original_copy2(source, target, *args, **kwargs)
                    os._exit(91)
                if fault == "stage-copy-2" and copy_count == 2 and not failed:
                    failed = True
                    raise OSError("injected second staged copy failure")
                return original_copy2(source, target, *args, **kwargs)

            def injected_replace(source, target, *args, **kwargs):
                global publish_count, failed, rollback_failed
                target_path = os.path.abspath(os.fspath(target))
                root_path = os.path.abspath(config_root)
                manifest_path = os.path.join(root_path, "config", "runtime-manifest.json")
                journal_path = os.path.join(root_path, ".runtime-install-journal.json")
                source_path = os.path.abspath(os.fspath(source))
                if fault == "hard-exit-before-first-journal-replace" and target_path == journal_path:
                    os._exit(91)
                is_publish = (
                    os.path.commonpath((root_path, target_path)) == root_path
                    and ".runtime-stage-" in source_path
                )
                if is_publish and target_path != manifest_path:
                    publish_count += 1
                if fault == "hard-exit-publish-2" and publish_count == 2 and not failed:
                    original_replace(source, target, *args, **kwargs)
                    os._exit(91)
                if fault == "publish-middle" and publish_count == 2 and not failed:
                    failed = True
                    raise OSError("injected middle publish failure")
                if fault == "manifest-replace" and target_path == manifest_path and not failed:
                    failed = True
                    raise OSError("injected manifest replace failure")
                if fault == "hard-exit-after-manifest" and target_path == manifest_path and not failed:
                    original_replace(source, target, *args, **kwargs)
                    os._exit(91)
                if fault == "publish-and-rollback" and publish_count == 2 and not failed:
                    failed = True
                    raise OSError("injected publish failure before rollback")
                if (
                    fault == "hard-exit-recovery-1"
                    and ".rollback" in source_path
                    and not rollback_failed
                ):
                    original_replace(source, target, *args, **kwargs)
                    os._exit(92)
                if (
                    fault == "publish-and-rollback"
                    and failed
                    and not rollback_failed
                    and ".rollback" in source_path
                ):
                    rollback_failed = True
                    raise OSError("injected rollback failure")
                return original_replace(source, target, *args, **kwargs)

            def injected_write_journal(root, payload):
                original_write_journal(root, payload)
                if fault == "hard-exit-after-committed" and payload.get("state") == "committed":
                    os._exit(91)

            install_runtime.shutil.copy2 = injected_copy2
            install_runtime._durable_replace = injected_replace
            install_runtime._write_runtime_journal = injected_write_journal
            install_runtime.install(install_runtime.Path(config_root), False)
            """
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                str(REPO_ROOT / "scripts"),
                str(root),
                fault,
            ],
            cwd=REPO_ROOT,
            env=isolated_subprocess_env(),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def assert_failed_install_preserved_old_runtime(
        self,
        root: Path,
        before: dict[str, bytes],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot_runtime_files(root), before)
        self.assertFalse(list(root.glob(".runtime-stage-*")))
        self.assertFalse(list(root.glob(".runtime-backup-*")))
        verified = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--config-root",
                str(root),
                "--verify",
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(
            (root / "scripts" / "local_adapter.py").read_bytes(),
            b"LOCAL = True\n",
        )

    def test_install_rolls_back_when_second_staged_copy_fails(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            before = self.make_verified_old_runtime(root)

            result = self.run_runtime_install_with_fault(root, "stage-copy-2")

            self.assert_failed_install_preserved_old_runtime(root, before, result)

    def test_install_rolls_back_when_middle_publish_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            before = self.make_verified_old_runtime(root)

            result = self.run_runtime_install_with_fault(root, "publish-middle")

            self.assert_failed_install_preserved_old_runtime(root, before, result)

    def test_install_rolls_back_when_manifest_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            before = self.make_verified_old_runtime(root)

            result = self.run_runtime_install_with_fault(root, "manifest-replace")

            self.assert_failed_install_preserved_old_runtime(root, before, result)

    def test_install_recovers_after_process_dies_during_publish(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            before = self.make_verified_old_runtime(root)

            crashed = self.run_runtime_install_with_fault(root, "hard-exit-publish-2")

            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            self.assertEqual(
                (root / "config" / "runtime-manifest.json").read_bytes(),
                before["config/runtime-manifest.json"],
            )
            self.assertNotEqual(
                (root / "scripts" / "agent_memory_audit.py").read_bytes(),
                before["scripts/agent_memory_audit.py"],
            )
            journal = json.loads(
                (root / ".runtime-install-journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["schema"], 1)
            self.assertEqual(journal["config_root"], ".")
            self.assertEqual(journal["state"], "publishing")
            self.assertEqual(
                set(journal),
                {
                    "schema",
                    "config_root",
                    "stage",
                    "state",
                    "previous_manifest_sha256",
                    "actions",
                },
            )
            self.assertEqual(
                [action["status"] for action in journal["actions"]],
                ["published", "prepared"],
            )
            self.assertTrue(
                all(
                    set(action)
                    == {
                        "relative",
                        "kind",
                        "existed",
                        "status",
                        "desired_sha256",
                        "backup_sha256",
                    }
                    for action in journal["actions"]
                )
            )
            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertTrue(json.loads(recovered.stdout)["recovered_transaction"])
            verified = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--config-root",
                    str(root),
                    "--verify",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertEqual(
                (root / "scripts" / "local_adapter.py").read_bytes(),
                b"LOCAL = True\n",
            )
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_recovers_legacy_manifest_after_process_dies_during_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            manifest_path = root / "config" / "runtime-manifest.json"
            legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            legacy_manifest.pop("process_crash_recovery")
            legacy_manifest.pop("power_loss_durability")
            manifest_path.write_text(
                json.dumps(legacy_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            strict_verify = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--config-root",
                    str(root),
                    "--verify",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertNotEqual(strict_verify.returncode, 0)

            crashed = self.run_runtime_install_with_fault(root, "hard-exit-publish-2")
            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)

            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            payload = json.loads(recovered.stdout)
            self.assertTrue(payload["recovered_transaction"])
            upgraded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIs(upgraded_manifest["process_crash_recovery"], True)
            self.assertEqual(upgraded_manifest["power_loss_durability"], "verified")
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_recovers_after_process_dies_after_manifest_replace(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            before = self.make_verified_old_runtime(root)

            crashed = self.run_runtime_install_with_fault(root, "hard-exit-after-manifest")

            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            journal = json.loads(
                (root / ".runtime-install-journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["state"], "publishing")
            self.assertEqual(
                journal["actions"][-1]["relative"],
                "config/runtime-manifest.json",
            )
            self.assertEqual(journal["actions"][-1]["status"], "prepared")
            self.assertNotEqual(
                (root / "config" / "runtime-manifest.json").read_bytes(),
                before["config/runtime-manifest.json"],
            )
            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertTrue(json.loads(recovered.stdout)["recovered_transaction"])
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_cleans_staging_transaction_after_process_dies(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            before = self.make_verified_old_runtime(root)

            crashed = self.run_runtime_install_with_fault(root, "hard-exit-stage-copy-2")

            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            journal = json.loads(
                (root / ".runtime-install-journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["state"], "staging")
            self.assertEqual(journal["actions"], [])
            for relative, content in before.items():
                self.assertEqual((root / relative).read_bytes(), content)
            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertTrue(json.loads(recovered.stdout)["recovered_transaction"])
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_cleans_stage_and_journal_temp_when_first_journal_replace_dies(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            self.make_verified_old_runtime(root)

            crashed = self.run_runtime_install_with_fault(
                root,
                "hard-exit-before-first-journal-replace",
            )

            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertTrue(list(root.glob(".runtime-stage-*")))
            self.assertTrue(list(root.glob("..runtime-install-journal.json.*.tmp")))
            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertFalse(list(root.glob(".runtime-stage-*")))
            self.assertFalse(list(root.glob("..runtime-install-journal.json.*.tmp")))

    def test_install_cleans_committed_transaction_after_process_dies(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            self.make_verified_old_runtime(root)

            crashed = self.run_runtime_install_with_fault(root, "hard-exit-after-committed")

            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            journal = json.loads(
                (root / ".runtime-install-journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["state"], "committed")
            verified_before_cleanup = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--config-root",
                    str(root),
                    "--verify",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(
                verified_before_cleanup.returncode,
                0,
                verified_before_cleanup.stdout + verified_before_cleanup.stderr,
            )
            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertTrue(json.loads(recovered.stdout)["recovered_transaction"])
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_recovery_is_idempotent_after_second_process_dies(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            self.make_verified_old_runtime(root)
            first_crash = self.run_runtime_install_with_fault(root, "hard-exit-publish-2")
            self.assertEqual(first_crash.returncode, 91, first_crash.stdout + first_crash.stderr)

            recovery_crash = self.run_runtime_install_with_fault(root, "hard-exit-recovery-1")

            self.assertEqual(
                recovery_crash.returncode,
                92,
                recovery_crash.stdout + recovery_crash.stderr,
            )
            self.assertTrue((root / ".runtime-install-journal.json").is_file())
            recovered = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertTrue(json.loads(recovered.stdout)["recovered_transaction"])
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_recovery_rejects_escaping_journal_action(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            workspace = Path(raw_root).resolve()
            root = workspace / "runtime"
            external = workspace / "outside.txt"
            external.write_bytes(b"outside-sentinel")
            self.make_verified_old_runtime(root)
            crashed = self.run_runtime_install_with_fault(root, "hard-exit-publish-2")
            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            journal_path = root / ".runtime-install-journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["actions"][0]["relative"] = "../outside.txt"
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            rejected = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
            self.assertIn("transaction action is invalid", rejected.stdout)
            self.assertEqual(external.read_bytes(), b"outside-sentinel")
            self.assertTrue(journal_path.is_file())
            self.assertTrue(list(root.glob(".runtime-stage-*")))

    def test_install_recovery_rejects_stage_reparse_without_traversal(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            workspace = Path(raw_root).resolve()
            root = workspace / "runtime"
            external = workspace / "external-stage"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_bytes(b"external-stage-sentinel")
            self.make_verified_old_runtime(root)
            crashed = self.run_runtime_install_with_fault(root, "hard-exit-publish-2")
            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            journal = json.loads(
                (root / ".runtime-install-journal.json").read_text(encoding="utf-8")
            )
            stage = root / journal["stage"]
            shutil.rmtree(stage)
            make_directory_reparse(stage, external)
            try:
                rejected = subprocess.run(
                    [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                    cwd=REPO_ROOT,
                    env=isolated_subprocess_env(),
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                self.assertIn("transaction stage is unsafe", rejected.stdout)
                self.assertEqual(sentinel.read_bytes(), b"external-stage-sentinel")
            finally:
                remove_directory_reparse(stage)

    def test_install_recovery_rejects_nested_backup_parent_reparse(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            workspace = Path(raw_root).resolve()
            root = workspace / "runtime"
            before = self.make_verified_old_runtime(root)
            crashed = self.run_runtime_install_with_fault(root, "hard-exit-publish-2")
            self.assertEqual(crashed.returncode, 91, crashed.stdout + crashed.stderr)
            journal = json.loads(
                (root / ".runtime-install-journal.json").read_text(encoding="utf-8")
            )
            stage = root / journal["stage"]
            backup_scripts = stage / ".rollback" / "scripts"
            shutil.rmtree(backup_scripts)
            external = workspace / "external-backups"
            external.mkdir()
            expected_external: dict[str, bytes] = {}
            for action in journal["actions"]:
                relative = action["relative"]
                if relative.startswith("scripts/"):
                    name = relative.removeprefix("scripts/")
                    content = before[relative]
                    (external / name).write_bytes(content)
                    expected_external[name] = content
            make_directory_reparse(backup_scripts, external)
            try:
                rejected = subprocess.run(
                    [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                    cwd=REPO_ROOT,
                    env=isolated_subprocess_env(),
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                self.assertIn("unsafe", rejected.stdout)
                for name, content in expected_external.items():
                    self.assertEqual((external / name).read_bytes(), content)
            finally:
                remove_directory_reparse(backup_scripts)

    def test_install_cleans_safe_orphan_stage_without_journal(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            self.make_verified_old_runtime(root)
            orphan = root / ".runtime-stage-orphan"
            orphan.mkdir()
            (orphan / "partial.bin").write_bytes(b"partial-stage")

            installed = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertFalse(orphan.exists())
            self.assertFalse((root / ".runtime-install-journal.json").exists())
            self.assertFalse(list(root.glob(".runtime-stage-*")))

    def test_install_preserves_orphan_rollback_evidence_without_journal(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            self.make_verified_old_runtime(root)
            recovery = root / ".runtime-stage-orphan" / ".rollback"
            recovery.mkdir(parents=True)
            evidence = recovery / "prior-runtime.bin"
            evidence.write_bytes(b"rollback-evidence")

            rejected = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
            self.assertIn("recovery_path=", rejected.stdout)
            self.assertEqual(evidence.read_bytes(), b"rollback-evidence")

    def test_live_runtime_install_lock_rejects_concurrent_process(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            workspace = Path(raw_root).resolve()
            root = workspace / "missing" / "nested" / "runtime"
            ready = workspace / "ready"
            release = workspace / "release"
            holder_probe = textwrap.dedent(
                """
                import sys
                import time
                from pathlib import Path

                scripts_root, config_root, ready_path, release_path = sys.argv[1:]
                sys.path.insert(0, scripts_root)
                import install_runtime

                original_stage = install_runtime._stage_runtime
                def blocking_stage(root, manifest):
                    Path(ready_path).write_text("ready", encoding="utf-8")
                    while not Path(release_path).exists():
                        time.sleep(0.02)
                    return original_stage(root, manifest)
                install_runtime._stage_runtime = blocking_stage
                install_runtime.install(Path(config_root), False)
                """
            )
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_probe,
                    str(REPO_ROOT / "scripts"),
                    str(root),
                    str(ready),
                    str(release),
                ],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                for _ in range(500):
                    if ready.exists():
                        break
                    if holder.poll() is not None:
                        break
                    time.sleep(0.02)
                if not ready.exists():
                    if holder.poll() is None:
                        self.fail("runtime install lock holder did not reach the barrier")
                    holder_stdout, holder_stderr = holder.communicate(timeout=5)
                    self.fail(holder_stdout + holder_stderr)
                contender = subprocess.run(
                    [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                    cwd=REPO_ROOT,
                    env=isolated_subprocess_env(),
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertNotEqual(contender.returncode, 0, contender.stdout + contender.stderr)
                self.assertIn("already in progress", contender.stdout)
            finally:
                release.write_text("release", encoding="utf-8")
            holder_stdout, holder_stderr = holder.communicate(timeout=60)
            self.assertEqual(holder.returncode, 0, holder_stdout + holder_stderr)
            after_release = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(after_release.returncode, 0, after_release.stdout + after_release.stderr)

    @unittest.skipUnless(os.name == "nt", "NTFS short aliases are Windows-specific")
    def test_runtime_install_lock_unifies_real_long_and_short_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            workspace = Path(raw_root).resolve()
            root = workspace / "Agent Memory Long Runtime Directory"
            root.mkdir()
            short_root = windows_short_path(root)
            if short_root is None:
                self.skipTest("the test volume does not expose an 8.3 alias")
            self.assertIn("~", str(short_root))
            ready = workspace / "alias-lock-ready"
            release = workspace / "alias-lock-release"
            holder_probe = textwrap.dedent(
                """
                import sys
                import time
                from pathlib import Path

                scripts_root, config_root, ready_path, release_path = sys.argv[1:]
                sys.path.insert(0, scripts_root)
                import install_runtime

                original_stage = install_runtime._stage_runtime
                def blocking_stage(root, manifest):
                    Path(ready_path).write_text("ready", encoding="utf-8")
                    while not Path(release_path).exists():
                        time.sleep(0.02)
                    return original_stage(root, manifest)
                install_runtime._stage_runtime = blocking_stage
                install_runtime.install(Path(config_root), False)
                """
            )
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_probe,
                    str(REPO_ROOT / "scripts"),
                    str(root),
                    str(ready),
                    str(release),
                ],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            contender: subprocess.CompletedProcess[str] | None = None
            try:
                for _ in range(500):
                    if ready.exists() or holder.poll() is not None:
                        break
                    time.sleep(0.02)
                if not ready.exists():
                    holder_stdout, holder_stderr = holder.communicate(timeout=5)
                    self.fail(holder_stdout + holder_stderr)
                contender = subprocess.run(
                    [
                        sys.executable,
                        str(INSTALLER),
                        "--config-root",
                        str(short_root),
                        "--json",
                    ],
                    cwd=REPO_ROOT,
                    env=isolated_subprocess_env(),
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
            finally:
                release.write_text("release", encoding="utf-8")
                holder_stdout, holder_stderr = holder.communicate(timeout=60)
            self.assertEqual(holder.returncode, 0, holder_stdout + holder_stderr)
            assert contender is not None
            self.assertNotEqual(contender.returncode, 0, contender.stdout + contender.stderr)
            self.assertIn("already in progress", contender.stdout)

            after_release = subprocess.run(
                [sys.executable, str(INSTALLER), "--config-root", str(short_root), "--json"],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(
                after_release.returncode,
                0,
                after_release.stdout + after_release.stderr,
            )

    @unittest.skipUnless(os.name == "nt", "Windows path canonicalization is Windows-specific")
    def test_runtime_lock_material_uses_handle_final_path_in_mocked_fallback(self) -> None:
        probe = textwrap.dedent(
            """
            import os
            import stat
            import sys
            from pathlib import Path
            from types import SimpleNamespace

            scripts_root = sys.argv[1]
            sys.path.insert(0, scripts_root)
            import install_runtime

            metadata = SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_dev=73,
                st_ino=991,
                st_file_attributes=0,
            )
            install_runtime.os.lstat = lambda _path: metadata
            install_runtime._windows_final_path_identity = lambda _path: (
                r"C:\\Canonical Parent\\Agent Memory Runtime",
                (73, 991),
            )
            long_material = install_runtime._canonical_runtime_lock_material(
                Path(r"C:\\Canonical Parent\\Agent Memory Runtime")
            )
            short_material = install_runtime._canonical_runtime_lock_material(
                Path(r"C:\\CANONI~1\\AGENTM~1")
            )
            if long_material != short_material:
                raise SystemExit(f"{long_material!r} != {short_material!r}")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(REPO_ROOT / "scripts")],
            cwd=REPO_ROOT,
            env=isolated_subprocess_env(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows file identities are Windows-specific")
    def test_windows_handle_identity_accepts_python_314_widened_device_id(self) -> None:
        probe = textwrap.dedent(
            """
            import sys
            from types import SimpleNamespace

            sys.path.insert(0, sys.argv[1])
            import install_runtime

            metadata = SimpleNamespace(
                st_dev=0x0478680B5CCE3968,
                st_ino=3096224748854910,
            )
            if not install_runtime._windows_handle_identity_matches(
                metadata,
                (0x5CCE3968, 3096224748854910),
            ):
                raise SystemExit("widened Python st_dev was not matched")
            if install_runtime._windows_handle_identity_matches(
                metadata,
                (0x5CCE3968, 3096224748854911),
            ):
                raise SystemExit("different file index was accepted")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(REPO_ROOT / "scripts")],
            cwd=REPO_ROOT,
            env=isolated_subprocess_env(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_install_preserves_recovery_backup_when_rollback_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve() / "runtime"
            self.make_verified_old_runtime(root)

            result = self.run_runtime_install_with_fault(root, "publish-and-rollback")

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("rollback was incomplete", result.stderr)
            stages = list(root.glob(".runtime-stage-*"))
            self.assertEqual(len(stages), 1)
            recovery = stages[0] / ".rollback"
            self.assertTrue(recovery.is_dir())
            self.assertTrue(any(path.is_file() for path in recovery.rglob("*")))

    def run_entry_with_external_config_reads_forbidden(
        self,
        entry_path: Path,
        entry_arguments: list[str],
        config_file: Path,
        external_file: Path,
        *,
        replace_before_open: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        probe = textwrap.dedent(
            """
            import io
            import os
            import runpy
            import sys

            entry_path, config_path, external_path, race_mode, *entry_arguments = sys.argv[1:]
            sys.path.insert(0, os.path.dirname(entry_path))
            import agent_memory_env

            external_metadata = os.stat(external_path)
            external_identity = (external_metadata.st_dev, external_metadata.st_ino)
            original_io_open = io.open
            original_os_read = os.read

            def points_at_external(path):
                try:
                    metadata = os.stat(path)
                except (OSError, TypeError, ValueError):
                    return False
                return (metadata.st_dev, metadata.st_ino) == external_identity

            def guarded_io_open(path, *args, **kwargs):
                if points_at_external(path):
                    raise RuntimeError("external_config_read")
                return original_io_open(path, *args, **kwargs)

            def guarded_os_read(descriptor, size):
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == external_identity:
                    raise RuntimeError("external_config_read")
                return original_os_read(descriptor, size)

            io.open = guarded_io_open
            os.read = guarded_os_read
            if race_mode == "race":
                original_stable_loader = agent_memory_env.load_config_stable

                def replace_config():
                    os.unlink(config_path)
                    os.symlink(external_path, config_path)

                def racing_stable_loader(path, **kwargs):
                    kwargs["before_open"] = replace_config
                    return original_stable_loader(path, **kwargs)

                agent_memory_env.load_config_stable = racing_stable_loader
            sys.argv = [entry_path, *entry_arguments]
            runpy.run_path(entry_path, run_name="__main__")
            """
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                str(entry_path),
                str(config_file),
                str(external_file),
                "race" if replace_before_open else "no-race",
                *entry_arguments,
            ],
            cwd=REPO_ROOT,
            env=isolated_subprocess_env(
                {"AGENT_MEMORY_CONFIG_FILE": str(config_file)}
            ),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def test_global_entrypoints_reject_config_parent_reparse_without_reading_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            external_dir = base / "external-config"
            external_dir.mkdir()
            external_file = external_dir / "agent-memory.toml"
            external_file.write_text(
                "user_id = 'external-config-payload'\n",
                encoding="utf-8",
            )
            config_parent = base / "config-link"
            try:
                make_directory_reparse(config_parent, external_dir)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                entrypoints = (
                    (REPO_ROOT / "scripts" / "memoryctl", ["--actor", "codex", "version"]),
                    (
                        REPO_ROOT / "scripts" / "agent_memory_check.py",
                        ["--skip-state-db"],
                    ),
                )
                for entry_path, arguments in entrypoints:
                    with self.subTest(entry=entry_path.name):
                        result = self.run_entry_with_external_config_reads_forbidden(
                            entry_path,
                            arguments,
                            config_parent / "agent-memory.toml",
                            external_file,
                        )
                        self.assertNotEqual(
                            result.returncode,
                            0,
                            result.stdout + result.stderr,
                        )
                        self.assertNotIn("external_config_read", result.stderr)
                        self.assertNotIn(
                            "external-config-payload",
                            result.stdout + result.stderr,
                        )
                        self.assertIn("ConfigPathSecurityError", result.stderr)
            finally:
                remove_directory_reparse(config_parent)

    def test_global_entrypoints_reject_malformed_explicit_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            config_file = base / "invalid-agent-memory.toml"
            config_file.write_text("memory_root = [\n", encoding="utf-8")
            entrypoints = (
                (REPO_ROOT / "scripts" / "memoryctl", ["--actor", "codex", "version"]),
                (
                    REPO_ROOT / "scripts" / "agent_memory_check.py",
                    ["--skip-state-db"],
                ),
            )
            for entry_path, arguments in entrypoints:
                with self.subTest(entry=entry_path.name):
                    result = subprocess.run(
                        [sys.executable, str(entry_path), *arguments],
                        cwd=REPO_ROOT,
                        env=isolated_subprocess_env(
                            {"AGENT_MEMORY_CONFIG_FILE": str(config_file)}
                        ),
                        text=True,
                        capture_output=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )
                    self.assertIn("ConfigPathSecurityError", result.stderr)
                    self.assertIn("invalid_config", result.stderr)

    def test_global_entrypoints_reject_config_file_replacement_before_read(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            base = Path(raw_root).resolve()
            external_file = base / "external-agent-memory.toml"
            external_file.write_text(
                "user_id = 'external-race-payload'\n",
                encoding="utf-8",
            )
            capability_link = base / "symlink-capability.toml"
            try:
                capability_link.symlink_to(external_file)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            else:
                capability_link.unlink()

            entrypoints = (
                (REPO_ROOT / "scripts" / "memoryctl", ["--actor", "codex", "version"]),
                (
                    REPO_ROOT / "scripts" / "agent_memory_check.py",
                    ["--skip-state-db"],
                ),
            )
            for index, (entry_path, arguments) in enumerate(entrypoints):
                with self.subTest(entry=entry_path.name):
                    config_file = base / f"agent-memory-{index}.toml"
                    config_file.write_text(
                        "user_id = 'safe-config'\n",
                        encoding="utf-8",
                    )
                    result = self.run_entry_with_external_config_reads_forbidden(
                        entry_path,
                        arguments,
                        config_file,
                        external_file,
                        replace_before_open=True,
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )
                    self.assertNotIn("external_config_read", result.stderr)
                    self.assertNotIn(
                        "external-race-payload",
                        result.stdout + result.stderr,
                    )
                    self.assertIn("ConfigPathSecurityError", result.stderr)
                    self.assertIn("config_file_changed", result.stderr)

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

    def run_doctor_with_config_replacement_race(
        self,
        doctor_path: Path,
        config_root: Path,
        config_file: Path,
        external_file: Path,
    ) -> subprocess.CompletedProcess[str]:
        probe = textwrap.dedent(
            """
            import io
            import os
            import runpy
            import sys

            doctor_path, config_path, external_path = sys.argv[1:]
            sys.path.insert(0, os.path.dirname(doctor_path))
            import agent_memory_env

            external_metadata = os.stat(external_path)
            external_identity = (external_metadata.st_dev, external_metadata.st_ino)
            original_io_open = io.open
            original_os_read = os.read

            def points_at_external(path):
                try:
                    metadata = os.stat(path)
                except (OSError, TypeError, ValueError):
                    return False
                return (metadata.st_dev, metadata.st_ino) == external_identity

            def guarded_io_open(path, *args, **kwargs):
                if points_at_external(path):
                    raise RuntimeError("external_config_read")
                return original_io_open(path, *args, **kwargs)

            def guarded_os_read(descriptor, size):
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == external_identity:
                    raise RuntimeError("external_config_read")
                return original_os_read(descriptor, size)

            io.open = guarded_io_open
            os.read = guarded_os_read

            def replace_before_open():
                os.unlink(config_path)
                os.symlink(external_path, config_path)

            if hasattr(agent_memory_env, "load_config_stable"):
                original_loader = agent_memory_env.load_config_stable

                def racing_loader(path):
                    return original_loader(path, before_open=replace_before_open)

                agent_memory_env.load_config_stable = racing_loader
            else:
                original_loader = agent_memory_env.load_config

                def racing_loader():
                    replace_before_open()
                    return original_loader()

                agent_memory_env.load_config = racing_loader

            sys.argv = [doctor_path, "--json"]
            runpy.run_path(doctor_path, run_name="__main__")
            """
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                str(doctor_path),
                str(config_file),
                str(external_file),
            ],
            cwd=config_root.parent,
            env=isolated_subprocess_env(
                {
                    "AGENT_MEMORY_CONFIG_ROOT": str(config_root),
                    "AGENT_MEMORY_CONFIG_FILE": str(config_file),
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

    def test_doctor_rejects_config_replacement_before_reading_external_target(self) -> None:
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
            config_file = runtime / "config" / "agent-memory.toml"
            config_file.write_text("user_id = 'safe-config'\n", encoding="utf-8")
            external = base / "external-config.toml"
            external.write_text("user_id = 'external-read-payload'\n", encoding="utf-8")
            capability_link = base / "symlink-capability.toml"
            try:
                capability_link.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            else:
                capability_link.unlink()

            doctor = self.run_doctor_with_config_replacement_race(
                runtime / "scripts" / "agent_memory_doctor.py",
                runtime,
                config_file,
                external,
            )

            self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
            self.assertNotIn("external_config_read", doctor.stderr)
            self.assertNotIn("external-read-payload", doctor.stdout + doctor.stderr)
            payload = json.loads(doctor.stdout)
            self.assertEqual(
                [item["name"] for item in payload["checks"]],
                ["runtime_config_paths"],
            )
            self.assertIn(
                "changed",
                json.dumps(payload["checks"][0]["detail"]["unsafe_paths"]).lower(),
            )

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

    def test_verify_reports_dangerous_manifest_names_without_traceback(self) -> None:
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
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            dangerous_names = (
                "templates/vault/NUL",
                "templates/vault/con.txt",
                "templates/vault/trailing. ",
                "templates/vault/question?.md",
                "templates/vault/colon:name.md",
                "templates/vault/control\x01name.md",
                "templates/vault/control\x85name.md",
                "templates/vault/control\x9fname.md",
                "templates/vault/format\u200ename.md",
                "templates/vault/zero\x00name.md",
            )
            for name in dangerous_names:
                with self.subTest(name=repr(name)):
                    manifest = json.loads(json.dumps(original))
                    manifest["template_files"][name] = "0" * 64
                    manifest["template_inventory"].append(name)
                    manifest["template_count"] += 1
                    manifest_path.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )

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

                    self.assertEqual(
                        verified.returncode,
                        2,
                        verified.stdout + verified.stderr,
                    )
                    self.assertNotIn("Traceback", verified.stderr)
                    payload = json.loads(verified.stdout)
                    self.assertIn(name, payload["closure"]["template_unsafe_names"])

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
