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
        """Windows now verifies the DACL, so honesty means matching the platform.

        On POSIX the report must claim mode enforcement. On Windows it may only
        claim enforcement when a private DACL was actually observed; a machine
        without PowerShell still reports the unverified warning instead.
        """
        report = sqlite_permission_report(path)
        self.assertEqual(report["permission_model"], PERMISSION_MODEL)
        if POSIX_MODE_ENFORCED:
            self.assertTrue(report["mode_enforced"])
            self.assertEqual(report["warnings"], [])
        else:
            if report["mode_enforced"]:
                self.assertEqual(report["warnings"], [])
                self.assertNotIn(
                    "acl",
                    [issue.get("reason") for issue in report["issues"]],
                )
            else:
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
                # Windows: after harden_sqlite_files the DACL should be private.
                # If PowerShell is unavailable the report stays honestly unverified.
                report = sqlite_permission_report(database)
                self.assertEqual(report["permission_model"], PERMISSION_MODEL)
                if report["mode_enforced"]:
                    self.assertEqual(report["warnings"], [])
                else:
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
                # chmod is a no-op on Windows; an inherited DACL is reported as an
                # acl issue (repairable) rather than a hard failure.
                reasons = [issue.get("reason") for issue in before["issues"]]
                self.assertIn(reasons, ([], ["acl"]))
                if reasons == ["acl"]:
                    self.assertFalse(before["mode_enforced"])
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
            payload = json.loads(checked.stdout)
            permission_check = next(
                item for item in payload["checks"] if item["name"] == "state_db_permissions"
            )
            if POSIX_MODE_ENFORCED:
                self.assertEqual(checked.returncode, 2, checked.stdout + checked.stderr)
                self.assertEqual(permission_check["status"], "fail")
                self.assertEqual(mode(state_db), 0o644)
            else:
                # chmod is a no-op on Windows: the DACL created by the indexer is
                # still private, so doctor must NOT report a false violation.
                # (When PowerShell is unavailable it degrades to the unverified warn.)
                self.assertIn(permission_check["status"], ("pass", "warn"))
                if permission_check["status"] == "pass":
                    self.assertEqual(permission_check["detail"]["warnings"], [])
                else:
                    self.assertIn(
                        "windows_acl_unverified", permission_check["detail"]["warnings"]
                    )

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



@unittest.skipUnless(state.WINDOWS_ACL_SUPPORTED, "Windows DACL inspection only")
class WindowsAclTests(unittest.TestCase):
    """2026-11-24: Windows now inspects the DACL instead of only saying unverified.

    The old behaviour could not tell a private file from one readable by every
    authenticated user, so the state DB / search log / audit ledger were never
    actually checked on Windows.
    """

    def test_inherited_dacl_is_reported_and_then_hardened(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            target = Path(raw_root).resolve() / "state.sqlite"
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")

            # The ACL cache is process-wide; clear it so this test observes the
            # file's real inherited DACL rather than a cached verdict.
            state._WINDOWS_ACL_PRIVATE_CACHE.discard(str(target))
            report = state.windows_acl_report(target)
            if report is None:
                self.skipTest("PowerShell unavailable")
            # A freshly created temp file inherits BUILTIN\\Users /
            # Authenticated Users, which must not count as private.
            # Not private either because inheritance is still on (the DACL is not
            # protected) or because a broad grantee is present; both must be caught.
            self.assertFalse(report["private"])
            self.assertTrue(
                report["untrusted_grantees"] or not report["inheritance_protected"],
                report,
            )
            reasons = [
                issue.get("reason")
                for issue in state._inspect_private_file(target, required=True)
            ]
            self.assertEqual(reasons, ["acl"])

            state._WINDOWS_ACL_PRIVATE_CACHE.discard(str(target))
            self.assertTrue(state.harden_windows_acl(target))
            after = state.windows_acl_report(target)
            self.assertIsNotNone(after)
            self.assertTrue(after["private"])
            self.assertEqual(after["untrusted_grantees"], [])
            self.assertTrue(after["inheritance_protected"])
            self.assertEqual(state._inspect_private_file(target, required=True), [])

    def test_permissive_acl_is_repairable_not_fatal(self) -> None:
        """A permissive DACL must be tightened, not turned into a hard failure."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            database = Path(raw_root).resolve() / "state.sqlite"
            # secure_sqlite_connect hardens on open; the connection must succeed
            # even though the inherited DACL was not private beforehand.
            connection = secure_sqlite_connect(database)
            try:
                connection.execute("CREATE TABLE sample(value TEXT)")
            finally:
                connection.close()
            report = sqlite_permission_report(database)
            self.assertTrue(report["ok"], report)
            if report["mode_enforced"]:
                self.assertEqual(report["warnings"], [])

    def test_acl_query_failure_degrades_to_unverified(self) -> None:
        """No PowerShell -> stay honest (unverified), never claim safety."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            target = Path(raw_root).resolve() / "state.sqlite"
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
            with mock.patch.object(state, "_powershell", return_value=[]):
                self.assertIsNone(state.windows_acl_report(target))
                self.assertEqual(
                    state._inspect_private_file(target, required=True), []
                )


if __name__ == "__main__":
    unittest.main()
