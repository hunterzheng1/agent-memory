from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory_claim as claim
import agent_memory_check as check
import agent_memory_closeout as closeout
import agent_memory_doctor as doctor
import agent_memory_index as index
import install_runtime as runtime


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class DurabilityGuardTests(unittest.TestCase):
    def test_index_initializes_recovery_audit_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        index.init_db(conn)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        self.assertIn("memory_deletion_observations", tables)
        self.assertIn("memory_committed_observations", tables)

    def test_claim_and_index_recovery_schemas_stay_in_parity(self) -> None:
        claim_conn = sqlite3.connect(":memory:")
        index_conn = sqlite3.connect(":memory:")
        claim.ensure_schema(claim_conn)
        index.init_db(index_conn)
        for table in (
            "memory_file_observations",
            "memory_deletion_observations",
            "memory_committed_observations",
        ):
            with self.subTest(table=table):
                claim_columns = [tuple(row[1:6]) for row in claim_conn.execute(f"PRAGMA table_info({table})")]
                index_columns = [tuple(row[1:6]) for row in index_conn.execute(f"PRAGMA table_info({table})")]
                self.assertEqual(claim_columns, index_columns)
        claim_conn.close()
        index_conn.close()

    def test_legacy_recovery_rows_migrate_to_untrusted_defaults(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE memory_deletion_observations (
              observation_id TEXT PRIMARY KEY, path TEXT NOT NULL, rel_path TEXT NOT NULL,
              sentinel TEXT NOT NULL, actor TEXT NOT NULL, user_authorized INTEGER NOT NULL,
              deletion_commit TEXT NOT NULL, parent_commit TEXT NOT NULL,
              prior_sha256 TEXT NOT NULL, trash_sha256 TEXT NOT NULL,
              trash_path_sha256 TEXT NOT NULL, evidence_ref_sha256 TEXT NOT NULL,
              evidence_ref_length INTEGER NOT NULL, observed_at TEXT NOT NULL
            );
            CREATE TABLE memory_committed_observations (
              observation_id TEXT PRIMARY KEY, path TEXT NOT NULL, rel_path TEXT NOT NULL,
              sha256 TEXT NOT NULL, actor TEXT NOT NULL, user_authorized INTEGER NOT NULL,
              intent_id TEXT NOT NULL, receipt_id TEXT NOT NULL, proposal_commit TEXT NOT NULL,
              observed_git_head TEXT NOT NULL, audit_chain_sha256 TEXT NOT NULL,
              evidence_ref_sha256 TEXT NOT NULL, evidence_ref_length INTEGER NOT NULL,
              observed_at TEXT NOT NULL
            );
            INSERT INTO memory_deletion_observations VALUES (
              'legacy-delete', 'p', 'r', 's', 'human', 1, 'c', 'p', 'h', 'h', 't', 'e', 1, 'now'
            );
            INSERT INTO memory_committed_observations VALUES (
              'legacy-commit', 'p', 'r', 'h', 'human', 1, 'i', 'r', 'c', 'c', 'a', 'e', 1, 'now'
            );
            """
        )
        claim.ensure_schema(conn)
        for table in ("memory_deletion_observations", "memory_committed_observations"):
            with self.subTest(table=table):
                row = conn.execute(
                    f"SELECT approval_trust, can_authorize_action, approval_receipt_sha256 "
                    f"FROM {table}"
                ).fetchone()
                self.assertEqual(row, ("self_attested", 0, ""))
        conn.close()

    def test_committed_observation_cas_rejects_post_commit_file_removal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            vault = root / "AgentMemory"
            note = vault / "项目" / "removed-after-commit.md"
            note.parent.mkdir(parents=True)
            content = b"# Approved snapshot\n"
            note.write_bytes(content)
            git(root, "init", "-q")
            git(root, "config", "user.name", "CAS Test")
            git(root, "config", "user.email", "test@example.invalid")
            git(root, "add", "AgentMemory")
            git(root, "commit", "-qm", "approved commit")
            commit = git(root, "rev-parse", "HEAD")
            blob = git(root, "rev-parse", f"{commit}:AgentMemory/项目/removed-after-commit.md")
            note.unlink()
            with (
                mock.patch.object(claim, "VAULT_ROOT", vault),
                mock.patch.object(claim, "GIT_ROOT", root),
                mock.patch.object(claim, "STATE_DB", root / "state.sqlite"),
            ):
                with self.assertRaisesRegex(ValueError, "CONTENT_CHANGED_AFTER_COMMIT"):
                    claim.record_file_observations(
                        "cas-session",
                        "codex",
                        [note],
                        committed_bindings={
                            str(note.resolve()): {
                                "raw_sha256": hashlib.sha256(content).hexdigest(),
                                "git_commit": commit,
                                "git_blob_oid": blob,
                            }
                        },
                    )
            self.assertFalse((root / "state.sqlite").exists())

    def test_doctor_reports_unobserved_closeout_history(self) -> None:
        pending = closeout.GitEntry(
            status="M",
            repo_path="AgentMemory/项目/pending.md",
            path=Path("/tmp/AgentMemory/项目/pending.md"),
        )
        with (
            mock.patch.object(closeout, "last_observed_git_head", return_value="a" * 40),
            mock.patch.object(closeout, "current_git_head", return_value=("b" * 40, [])),
            mock.patch.object(closeout, "git_history_entries", return_value=([pending], [])),
            mock.patch.object(closeout, "unobserved_history_entries", return_value=[pending]),
            mock.patch.object(closeout, "relative_to_vault", return_value="项目/pending.md"),
        ):
            healthy, detail = doctor.closeout_observation_health()
        self.assertFalse(healthy)
        self.assertEqual(detail["pending_count"], 1)
        self.assertEqual(detail["pending_existing"], ["项目/pending.md"])
        self.assertEqual(detail["pending_deleted"], [])

    def test_doctor_accepts_fully_observed_closeout_history(self) -> None:
        with (
            mock.patch.object(closeout, "last_observed_git_head", return_value="a" * 40),
            mock.patch.object(closeout, "current_git_head", return_value=("b" * 40, [])),
            mock.patch.object(closeout, "git_history_entries", return_value=([], [])),
            mock.patch.object(closeout, "unobserved_history_entries", return_value=[]),
        ):
            healthy, detail = doctor.closeout_observation_health()
        self.assertTrue(healthy)
        self.assertEqual(detail["pending_count"], 0)

    def test_doctor_validates_claude_compatible_hook_lifecycle(self) -> None:
        def hooks(actor: str, *, session_end_nonblocking: bool = True) -> dict[str, object]:
            nonblocking = " --non-blocking" if session_end_nonblocking else ""
            return {
                "Stop": [{"hooks": [{
                    "type": "command",
                    "command": (
                        f"agent_memory_stop_hook.py --actor {actor} --protocol claude "
                        "--event stop-hook --auto-closeout"
                    ),
                    "timeout": 320,
                }]}],
                "SessionEnd": [{"hooks": [{
                    "type": "command",
                    "command": (
                        f"agent_memory_stop_hook.py --actor {actor} --protocol claude "
                        f"--event session-end{nonblocking} --auto-closeout"
                    ),
                    "timeout": 60,
                }]}],
                "SessionStart": [{"hooks": [{
                    "type": "command",
                    "command": f"agent_memory_session_hook.py --actor {actor}",
                    "timeout": 10,
                }]}],
            }

        for actor in ("claude", "codebuddy"):
            with self.subTest(actor=actor):
                healthy, detail = doctor.claude_compatible_hook_semantics(hooks(actor), actor)
                self.assertTrue(healthy, detail)
                broken, broken_detail = doctor.claude_compatible_hook_semantics(
                    hooks(actor, session_end_nonblocking=False),
                    actor,
                )
                self.assertFalse(broken)
                self.assertFalse(broken_detail["session_end_non_blocking"])

        impostor = hooks("claude")
        for event in ("Stop", "SessionEnd", "SessionStart"):
            command = impostor[event][0]["hooks"][0]["command"]
            impostor[event][0]["hooks"][0]["command"] = command.replace(
                "--actor claude",
                "--actor claude-malicious",
            )
        healthy, _detail = doctor.claude_compatible_hook_semantics(impostor, "claude")
        self.assertFalse(healthy)

    def test_search_log_privacy_detects_plain_used_paths_after_query_redaction(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE memory_search_log(query TEXT, used_paths TEXT)"
        )
        conn.execute(
            "INSERT INTO memory_search_log VALUES (?, ?)",
            ("[redacted:abc123]", "项目/private-customer.md"),
        )
        healthy, detail = doctor.search_log_privacy_health(conn)
        conn.close()
        self.assertFalse(healthy)
        self.assertEqual(detail["legacy_raw_rows"], 1)
    def test_local_check_covers_runtime_dependency_closure(self) -> None:
        checked_files = {path.name for path in check.REQUIRED_LOCAL_FILES}
        self.assertEqual(checked_files, set(runtime.CORE_FILES))

    def test_runtime_health_requires_path_resolver_dependency(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            with mock.patch.multiple(
                doctor,
                SCRIPT_ROOT=tmp / "empty-scripts",
                STATE_DB=tmp / "missing-state.sqlite",
                CONFIG_ROOT=tmp / "runtime",
            ):
                checks = doctor.collect_checks()

        runtime = next(item for item in checks if item["name"] == "runtime_files")
        self.assertIn("agent_memory_paths.py", runtime["detail"]["missing"])

    def test_derived_repair_uses_configured_semantic_python(self) -> None:
        configured_python = Path("/configured/vector/python")
        with mock.patch.object(doctor, "SEMANTIC_ENABLED", True), mock.patch.object(
            doctor, "ZVEC_PYTHON", configured_python
        ), mock.patch.object(
            doctor,
            "run",
            side_effect=[
                {"ok": True, "detail": "sqlite rebuilt"},
                {"ok": True, "detail": "zvec rebuilt"},
            ],
        ) as run_mock:
            actions = doctor.repair_derived()
        self.assertEqual([item["action"] for item in actions], ["rebuild_sqlite_fts", "rebuild_zvec"])
        vector_command = run_mock.call_args_list[1].args[0]
        self.assertEqual(vector_command[0], str(configured_python))
        self.assertTrue(vector_command[1].endswith("agent_memory_zvec_index.py"))

    def test_semantic_python_detects_missing_interpreter(self) -> None:
        with mock.patch.object(doctor, "ZVEC_PYTHON", Path("/definitely/missing/python")):
            ok, detail = doctor.verify_semantic_python_runtime()
        self.assertFalse(ok)
        self.assertEqual(detail["error"], "python_missing_or_broken_symlink")

    def test_semantic_python_accepts_live_base_interpreter(self) -> None:
        with mock.patch.object(doctor, "ZVEC_PYTHON", Path(sys.executable)):
            ok, detail = doctor.verify_semantic_python_runtime()
        self.assertTrue(ok, detail)
        self.assertTrue(detail["base_exists"])

    def test_remote_backup_warns_when_memory_commit_ages_out(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            remote = tmp / "remote.git"
            work = tmp / "work"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "--initial-branch=main", str(work)], check=True)
            git(work, "config", "user.name", "Agent Memory Test")
            git(work, "config", "user.email", "test@example.invalid")
            memory_root = work / "AgentMemory"
            memory_root.mkdir()
            note = memory_root / "note.md"
            note.write_text("baseline\n", encoding="utf-8")
            git(work, "add", "AgentMemory/note.md")
            git(work, "commit", "-qm", "baseline")
            git(work, "remote", "add", "origin", str(remote))
            git(work, "push", "-qu", "origin", "main")

            with mock.patch.object(doctor, "GIT_ROOT", work):
                healthy, detail = doctor.git_remote_backup_health("AgentMemory")
            self.assertTrue(healthy, detail)
            self.assertEqual(detail["ahead_memory"], 0)

            note.write_text("local memory change\n", encoding="utf-8")
            git(work, "add", "AgentMemory/note.md")
            git(work, "commit", "-qm", "local memory")
            future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)
            with mock.patch.object(doctor, "GIT_ROOT", work):
                healthy, detail = doctor.git_remote_backup_health("AgentMemory", now=future)
            self.assertFalse(healthy)
            self.assertEqual(detail["ahead_memory"], 1)
            self.assertGreaterEqual(detail["oldest_unpushed_age_days"], 3)

    def test_doctor_reports_stale_claim_without_exposing_session_id(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE memory_session_claims (
              session_hash TEXT NOT NULL,
              actor TEXT NOT NULL,
              path TEXT NOT NULL,
              rel_path TEXT NOT NULL,
              status TEXT NOT NULL,
              claimed_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            )
            """
        )
        now = dt.datetime(2026, 7, 12, tzinfo=dt.timezone.utc)
        conn.executemany(
            "INSERT INTO memory_session_claims VALUES (?, ?, ?, ?, 'active', ?, ?, NULL)",
            [
                ("fresh-session", "codex", "/fresh.md", "fresh.md", now.isoformat(), now.isoformat()),
                (
                    "stale-session",
                    "claude",
                    "/stale.md",
                    "stale.md",
                    (now - dt.timedelta(days=2)).isoformat(),
                    (now - dt.timedelta(days=2)).isoformat(),
                ),
            ],
        )
        healthy, detail = doctor.session_claim_hygiene(conn, now=now)
        conn.close()
        self.assertFalse(healthy)
        self.assertEqual(detail["active"], 2)
        self.assertEqual(detail["stale"][0]["rel_path"], "stale.md")
        self.assertNotIn("session_hash", detail["stale"][0])

    def test_precommit_dirty_baseline_is_only_allowed_when_explicit(self) -> None:
        strict = doctor.memory_git_baseline_result(1, True, allow_dirty_memory=False)
        closeout = doctor.memory_git_baseline_result(1, True, allow_dirty_memory=True)
        self.assertEqual(strict[0], "warn")
        self.assertEqual(closeout[0], "pass")
        self.assertFalse(strict[2]["allowed_precommit"])
        self.assertTrue(closeout[2]["allowed_precommit"])

    def test_codebuddy_stop_hook_requires_actor_flag_in_settings(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            vault.mkdir()
            state_db = tmp / "state.sqlite"
            with sqlite3.connect(state_db) as conn:
                index.init_db(conn)
            settings = tmp / "settings.json"
            settings.write_text(
                '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"python agent_memory_stop_hook.py --actor codebuddy --protocol claude"}]}]}}',
                encoding="utf-8",
            )
            weak = tmp / "weak.json"
            weak.write_text(
                '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"python agent_memory_stop_hook.py --actor claude"}]}]}}',
                encoding="utf-8",
            )
            host = {"codebuddy_settings_json": str(settings)}
            doctor_globals = {
                "STATE_DB": state_db,
                "VAULT_ROOT": vault,
                "GIT_ROOT": tmp,
                "CONFIG_ROOT": tmp / "runtime",
                "AUDIT_LOG": tmp / "audit.jsonl",
                "CLOSEOUT_LOG": tmp / "closeout.jsonl",
                "SEMANTIC_ENABLED": False,
            }
            with mock.patch.multiple(doctor, HOST_CONFIG=host, **doctor_globals):
                checks = doctor.collect_checks(allow_dirty_memory=True)
            by_name = {c["name"]: c for c in checks}
            self.assertEqual(by_name["codebuddy_stop_hook"]["status"], "pass")

            host_weak = {"codebuddy_settings_json": str(weak)}
            with mock.patch.multiple(doctor, HOST_CONFIG=host_weak, **doctor_globals):
                checks_weak = doctor.collect_checks(allow_dirty_memory=True)
            by_weak = {c["name"]: c for c in checks_weak}
            self.assertEqual(by_weak["codebuddy_stop_hook"]["status"], "warn")

    def test_stale_claim_preview_and_expiry_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "AgentMemory"
            vault.mkdir()
            note = vault / "note.md"
            note.write_text("memory\n", encoding="utf-8")
            state_db = tmp / "state.sqlite"
            with mock.patch.object(claim, "VAULT_ROOT", vault), mock.patch.object(claim, "STATE_DB", state_db):
                claim.claim_paths("codex", "old-session", [str(note)])
                with sqlite3.connect(state_db) as conn:
                    conn.execute(
                        "UPDATE memory_session_claims SET updated_at='2000-01-01T00:00:00+00:00'"
                    )
                    conn.commit()
                self.assertEqual(claim.all_active_claim_rows(max_age_hours=24), [])
                self.assertEqual(
                    claim.active_claim_rows("old-session", "codex", max_age_hours=24),
                    [],
                )
                rows, applied = claim.expire_stale_claims(24, apply=False)
                self.assertEqual((len(rows), applied), (1, 0))
                with sqlite3.connect(state_db) as conn:
                    conn.execute(
                        "UPDATE memory_session_claims SET updated_at=?",
                        (claim.utc_now(),),
                    )
                    conn.commit()
                with mock.patch.object(claim, "stale_active_claim_rows", return_value=rows):
                    _, applied = claim.expire_stale_claims(24, apply=True)
                self.assertEqual(applied, 0)
                with sqlite3.connect(state_db) as conn:
                    conn.execute(
                        "UPDATE memory_session_claims SET updated_at='2000-01-01T00:00:00+00:00'"
                    )
                    conn.commit()
                rows, applied = claim.expire_stale_claims(24, apply=True)
                self.assertEqual((len(rows), applied), (1, 1))
                with sqlite3.connect(state_db) as conn:
                    status = conn.execute("SELECT status FROM memory_session_claims").fetchone()[0]
                self.assertEqual(status, "expired")


if __name__ == "__main__":
    unittest.main()
