from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.subprocess_env import isolated_subprocess_env


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_memory_state import (
    POSIX_MODE_ENFORCED,
    PERMISSION_MODEL,
    StateSecurityError,
    secure_append_text,
    secure_sqlite_connect,
    sqlite_permission_report,
)
import agent_memory_state as state


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class StatePermissionTests(unittest.TestCase):
    def assert_permission_report_is_honest(self, path: Path) -> None:
        report = sqlite_permission_report(path)
        self.assertEqual(report["permission_model"], PERMISSION_MODEL)
        self.assertEqual(report["mode_enforced"], POSIX_MODE_ENFORCED)
        if not POSIX_MODE_ENFORCED:
            self.assertIn("windows_acl_unverified", report["warnings"])

    def test_connection_context_closes_the_database_handle(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            database = Path(raw_root).resolve() / "state.sqlite"
            with secure_sqlite_connect(database) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            database.unlink()
            self.assertFalse(database.exists())

    def test_secure_append_creates_private_log_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            log_path = root / "logs" / "closeout.jsonl"
            secure_append_text(log_path, '{"status":"ok"}\n')
            secure_append_text(log_path, '{"status":"warning"}\n')
            if POSIX_MODE_ENFORCED:
                self.assertEqual(mode(root / "logs"), 0o700)
                self.assertEqual(mode(log_path), 0o600)
            self.assertEqual(len(log_path.read_text(encoding="utf-8").splitlines()), 2)

            target = root / "target.log"
            target.write_text("existing\n", encoding="utf-8")
            link = root / "logs" / "linked.jsonl"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(StateSecurityError):
                secure_append_text(link, "blocked\n")

    def test_secure_connect_creates_private_tree_database_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            database = root / "new" / "nested" / "state.sqlite"
            connection = secure_sqlite_connect(
                database,
                pragmas=("PRAGMA journal_mode=WAL",),
            )
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('ok')")
            connection.commit()

            self.assert_permission_report_is_honest(database)
            if POSIX_MODE_ENFORCED:
                self.assertEqual(mode(root / "new"), 0o700)
                self.assertEqual(mode(root / "new" / "nested"), 0o700)
                self.assertEqual(mode(database), 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                self.assertTrue(sidecar.is_file())
                if POSIX_MODE_ENFORCED:
                    self.assertEqual(mode(sidecar), 0o600)
            connection.close()

    def test_writable_connect_hardens_existing_parent_without_overclaiming_windows_acl(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            parent = Path(raw_root).resolve() / "existing"
            parent.mkdir()
            parent.chmod(0o777)
            database = parent / "state.sqlite"
            with mock.patch.object(
                state,
                "ensure_private_directory",
                wraps=state.ensure_private_directory,
            ) as ensure_mock:
                connection = secure_sqlite_connect(database)
                connection.close()

            ensure_mock.assert_called_once_with(parent, harden_existing=True)
            if POSIX_MODE_ENFORCED:
                self.assertEqual(mode(parent), 0o700)
            else:
                report = sqlite_permission_report(database)
                self.assertFalse(report["mode_enforced"])
                self.assertEqual(report["permission_model"], "windows_acl_unverified")
                self.assertIn("windows_acl_unverified", report["warnings"])

    def test_secure_connect_rejects_database_symlink(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            target = root / "target.sqlite"
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
            link = root / "state.sqlite"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(StateSecurityError):
                secure_sqlite_connect(link)

    def test_normal_connect_repairs_existing_mode_drift(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            database = Path(raw_root).resolve() / "state.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
            database.chmod(0o644)
            before = sqlite_permission_report(database)
            if POSIX_MODE_ENFORCED:
                self.assertFalse(before["ok"])
            else:
                self.assertTrue(before["ok"])
                self.assertIn("windows_acl_unverified", before["warnings"])
            connection = secure_sqlite_connect(database)
            connection.close()
            if POSIX_MODE_ENFORCED:
                self.assertEqual(mode(database), 0o600)
            self.assert_permission_report_is_honest(database)

    def test_doctor_fails_mode_drift_without_repairing_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            state_db = runtime / "state.sqlite"
            env = isolated_subprocess_env(
                {
                    "AGENT_MEMORY_ROOT": str(REPO_ROOT / "templates" / "vault"),
                    "AGENT_MEMORY_GIT_ROOT": str(REPO_ROOT),
                    "AGENT_MEMORY_CONFIG_ROOT": str(runtime),
                    "AGENT_MEMORY_STATE_DB": str(state_db),
                }
            )
            indexed = subprocess.run(
                [sys.executable, str(SCRIPTS / "agent_memory_index.py"), "--init", "--scan"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(indexed.returncode, 0, indexed.stdout + indexed.stderr)
            state_db.chmod(0o644)

            checked = subprocess.run(
                [sys.executable, str(SCRIPTS / "agent_memory_doctor.py"), "--json"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(checked.returncode, 2 if POSIX_MODE_ENFORCED else 0, checked.stdout + checked.stderr)
            payload = json.loads(checked.stdout)
            permission_check = next(
                item for item in payload["checks"] if item["name"] == "state_db_permissions"
            )
            self.assertEqual(permission_check["status"], "fail" if POSIX_MODE_ENFORCED else "warn")
            if POSIX_MODE_ENFORCED:
                self.assertEqual(mode(state_db), 0o644)
            else:
                self.assertIn("windows_acl_unverified", permission_check["detail"]["warnings"])

    def test_read_only_connect_falls_back_to_query_only_rw_for_wal_open_failure(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            database = Path(raw_root).resolve() / "state.sqlite"
            with secure_sqlite_connect(
                database,
                pragmas=("PRAGMA journal_mode=WAL",),
            ) as writer:
                writer.execute("CREATE TABLE sample(value TEXT)")
                writer.execute("INSERT INTO sample VALUES ('ok')")
                writer.commit()

                real_connect = sqlite3.connect
                real_execute = state.PrivateSQLiteConnection.execute
                failed_schema_probe = False

                def fail_first_schema_probe(connection, statement, *args, **kwargs):
                    nonlocal failed_schema_probe
                    if not failed_schema_probe and statement.strip().upper() == "PRAGMA SCHEMA_VERSION":
                        failed_schema_probe = True
                        raise sqlite3.OperationalError("unable to open database file")
                    return real_execute(connection, statement, *args, **kwargs)

                with (
                    mock.patch.object(state.sqlite3, "connect", wraps=real_connect) as connect_mock,
                    mock.patch.object(
                        state.PrivateSQLiteConnection,
                        "execute",
                        new=fail_first_schema_probe,
                    ),
                ):
                    with secure_sqlite_connect(
                        database,
                        read_only=True,
                        pragmas=("PRAGMA query_only=OFF",),
                    ) as reader:
                        self.assertEqual(reader.execute("PRAGMA query_only").fetchone()[0], 1)
                        self.assertEqual(reader.execute("SELECT value FROM sample").fetchone()[0], "ok")
                        with self.assertRaises(sqlite3.OperationalError):
                            reader.execute("UPDATE sample SET value='changed'")
                self.assertEqual(writer.execute("SELECT value FROM sample").fetchone()[0], "ok")

                targets = [str(call.args[0]) for call in connect_mock.call_args_list]
                self.assertEqual(len(targets), 2)
                self.assertTrue(targets[0].endswith("?mode=ro"), targets)
                self.assertTrue(targets[1].endswith("?mode=rw"), targets)

    def test_read_only_connect_never_creates_a_missing_database(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            database = Path(raw_root).resolve() / "missing.sqlite"
            with self.assertRaises(StateSecurityError):
                secure_sqlite_connect(database, read_only=True)
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
