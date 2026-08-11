from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def load_stop_hook():
    path = REPO_ROOT / "scripts" / "agent_memory_stop_hook.py"
    spec = importlib.util.spec_from_file_location("test_stop_hook_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


class StopHookProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_stop_hook()

    def test_claude_failure_blocks_with_json(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "notify"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = self.module.report_failure(
                "claude", {"status": "error", "error": "synthetic failure"}
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("synthetic failure", payload["reason"])
        self.assertEqual(stderr.getvalue(), "")

    def test_codex_failure_requests_continuation(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "notify"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = self.module.report_failure(
                "codex", {"status": "error", "error": "synthetic failure"}
            )

        self.assertEqual(returncode, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Continue this turn", stderr.getvalue())
        self.assertIn("synthetic failure", stderr.getvalue())

    def test_codebuddy_stop_uses_claude_protocol_on_failure(self) -> None:
        """CodeBuddy hooks configure --actor codebuddy --protocol claude (UT-021)."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "notify"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = self.module.report_failure(
                "claude", {"status": "error", "error": "codebuddy closeout failed"}
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("codebuddy closeout failed", payload["reason"])
        self.assertEqual(stderr.getvalue(), "")

    def test_protocol_defaults_are_derived_for_every_hook_actor(self) -> None:
        for actor, expected_protocol in (
            ("codex", "codex"),
            ("claude", "claude"),
            ("codebuddy", "claude"),
        ):
            with self.subTest(actor=actor):
                with mock.patch.object(
                    sys,
                    "argv",
                    ["agent_memory_stop_hook.py", "--actor", actor],
                ):
                    args = self.module.parse_args()
                self.assertEqual(args.protocol, expected_protocol)

    def test_codebuddy_accepts_compatible_explicit_protocol(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["agent_memory_stop_hook.py", "--actor", "codebuddy", "--protocol", "claude"],
        ):
            args = self.module.parse_args()

        self.assertEqual(args.protocol, "claude")

    def test_codebuddy_rejects_conflicting_explicit_protocol(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["agent_memory_stop_hook.py", "--actor", "codebuddy", "--protocol", "codex"],
            ),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.module.parse_args()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("conflicts", stderr.getvalue())

    def test_codebuddy_session_key_uses_native_env(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CODEBUDDY_SESSION_ID": "cb-stop-session", "CLAUDE_SESSION_ID": "claude-x"},
            clear=True,
        ):
            self.assertEqual(self.module.session_key({}, "codebuddy"), "cb-stop-session")

    def test_session_end_failure_is_notification_only(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "notify") as notified,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = self.module.report_failure(
                "claude",
                {"status": "error", "error": "synthetic session-end failure"},
                non_blocking=True,
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        notified.assert_called_once()

    def test_session_end_closeout_is_attributed_and_skips_audit(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"status": "ok"}),
            stderr="",
        )
        with mock.patch.object(self.module.subprocess, "run", return_value=completed) as invoked:
            result = self.module.run_closeout(
                {"session_id": "claude-session-end"},
                "claude",
                55,
                "session-end",
            )
        self.assertEqual(result["status"], "ok")
        command = invoked.call_args.args[0]
        self.assertEqual(command[command.index("--trigger") + 1], "session-end")
        self.assertIn("--skip-audit", command)

    def test_reentered_claude_or_codebuddy_stop_does_not_block_again(self) -> None:
        for actor in ("claude", "codebuddy"):
            with self.subTest(actor=actor):
                args = types.SimpleNamespace(
                    actor=actor,
                    protocol="claude",
                    event="stop-hook",
                    non_blocking=False,
                    auto_closeout=True,
                    timeout=300,
                )
                stdout = io.StringIO()
                with (
                    mock.patch.object(self.module, "parse_args", return_value=args),
                    mock.patch.object(
                        self.module,
                        "read_payload",
                        return_value={"session_id": f"{actor}-reentry", "stop_hook_active": True},
                    ),
                    mock.patch.object(self.module, "pending_paths", return_value=[Path("/tmp/pending.md")]),
                    mock.patch.object(self.module, "active_claim_rows", return_value=[]),
                    mock.patch.object(self.module, "all_active_claim_rows", return_value=[]),
                    mock.patch.object(self.module, "notify") as notified,
                    contextlib.redirect_stdout(stdout),
                ):
                    returncode = self.module.main()
                self.assertEqual(returncode, 0)
                self.assertEqual(stdout.getvalue(), "")
                notified.assert_called_once()

    def test_first_stop_still_blocks_real_unclaimed_change(self) -> None:
        args = types.SimpleNamespace(
            actor="claude",
            protocol="claude",
            event="stop-hook",
            non_blocking=False,
            auto_closeout=True,
            timeout=300,
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(self.module, "parse_args", return_value=args),
            mock.patch.object(
                self.module,
                "read_payload",
                return_value={"session_id": "claude-first-stop", "stop_hook_active": False},
            ),
            mock.patch.object(self.module, "pending_paths", return_value=[Path("/tmp/pending.md")]),
            mock.patch.object(self.module, "active_claim_rows", return_value=[]),
            mock.patch.object(self.module, "all_active_claim_rows", return_value=[]),
            mock.patch.object(self.module, "notify"),
            contextlib.redirect_stdout(stdout),
        ):
            returncode = self.module.main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("not claimed", payload["reason"])

    def test_auto_closeout_stays_quiet_for_another_active_session_claim(self) -> None:
        pending = Path("other-codebuddy-session.md").resolve()
        args = types.SimpleNamespace(
            actor="codebuddy",
            protocol="claude",
            event="stop-hook",
            non_blocking=False,
            auto_closeout=True,
            timeout=300,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "parse_args", return_value=args),
            mock.patch.object(
                self.module,
                "read_payload",
                return_value={"session_id": "current-codebuddy-session"},
            ),
            mock.patch.object(self.module, "pending_paths", return_value=[pending]),
            mock.patch.object(self.module, "active_claim_rows", return_value=[]),
            mock.patch.object(
                self.module,
                "all_active_claim_rows",
                return_value=[
                    {
                        "path": str(pending),
                        "actor": "codebuddy",
                        "session_hash": "other-session-hash",
                    }
                ],
            ),
            mock.patch.object(self.module, "run_due_audit") as run_due_audit,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = self.module.main()

        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        run_due_audit.assert_called_once_with()

    def test_auto_closeout_still_blocks_when_current_claim_closeout_fails(self) -> None:
        args = types.SimpleNamespace(
            actor="codebuddy",
            protocol="claude",
            event="stop-hook",
            non_blocking=False,
            auto_closeout=True,
            timeout=300,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "parse_args", return_value=args),
            mock.patch.object(
                self.module,
                "read_payload",
                return_value={"session_id": "current-codebuddy-session"},
            ),
            mock.patch.object(self.module, "pending_paths", return_value=[]),
            mock.patch.object(
                self.module,
                "active_claim_rows",
                return_value=[{"path": "current-session-memory.md"}],
            ),
            mock.patch.object(
                self.module,
                "run_closeout",
                return_value={"status": "error", "error": "synthetic closeout failure"},
            ),
            mock.patch.object(self.module, "audit_lifecycle_failure"),
            mock.patch.object(self.module, "notify"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = self.module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("synthetic closeout failure", payload["reason"])
        self.assertEqual(stderr.getvalue(), "")

    def test_clean_session_end_is_nonblocking_and_skips_weekly_audit(self) -> None:
        args = types.SimpleNamespace(
            actor="claude",
            protocol="claude",
            event="session-end",
            non_blocking=True,
            auto_closeout=True,
            timeout=55,
        )
        with (
            mock.patch.object(self.module, "parse_args", return_value=args),
            mock.patch.object(self.module, "read_payload", return_value={"session_id": "clean-end"}),
            mock.patch.object(self.module, "pending_paths", return_value=[]),
            mock.patch.object(self.module, "active_claim_rows", return_value=[]),
            mock.patch.object(self.module, "run_due_audit") as audit,
        ):
            returncode = self.module.main()
        self.assertEqual(returncode, 0)
        audit.assert_not_called()

    def test_missing_session_is_quiet_when_all_changes_belong_to_other_session(self) -> None:
        pending = Path("/tmp/other-session.md").resolve()
        args = types.SimpleNamespace(
            actor="claude",
            protocol="claude",
            event="stop-hook",
            non_blocking=False,
            auto_closeout=True,
            timeout=300,
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(self.module, "parse_args", return_value=args),
            mock.patch.object(self.module, "read_payload", return_value={}),
            mock.patch.object(self.module, "pending_paths", return_value=[pending]),
            mock.patch.object(self.module, "active_claim_rows", return_value=[]),
            mock.patch.object(
                self.module,
                "all_active_claim_rows",
                return_value=[
                    {
                        "path": str(pending),
                        "actor": "codex",
                        "session_hash": "1" * 16,
                    }
                ],
            ),
            mock.patch.object(self.module, "run_due_audit") as audit,
            contextlib.redirect_stdout(stdout),
        ):
            returncode = self.module.main()
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.getvalue(), "")
        audit.assert_called_once()

    def test_missing_session_same_actor_claim_blocks_claude_and_codebuddy_first_stop(self) -> None:
        pending = Path("/tmp/same-actor-session-unknown.md").resolve()
        for actor in ("claude", "codebuddy"):
            with self.subTest(actor=actor):
                args = types.SimpleNamespace(
                    actor=actor,
                    protocol="claude",
                    event="stop-hook",
                    non_blocking=False,
                    auto_closeout=True,
                    timeout=300,
                )
                stdout = io.StringIO()
                with (
                    mock.patch.object(self.module, "parse_args", return_value=args),
                    mock.patch.object(self.module, "read_payload", return_value={}),
                    mock.patch.object(self.module, "pending_paths", return_value=[pending]),
                    mock.patch.object(self.module, "active_claim_rows", return_value=[]),
                    mock.patch.object(
                        self.module,
                        "all_active_claim_rows",
                        return_value=[
                            {
                                "path": str(pending),
                                "actor": actor,
                                "session_hash": "2" * 16,
                            }
                        ],
                    ),
                    mock.patch.object(self.module, "notify"),
                    contextlib.redirect_stdout(stdout),
                ):
                    returncode = self.module.main()
                payload = json.loads(stdout.getvalue())
                self.assertEqual(returncode, 0)
                self.assertEqual(payload["decision"], "block")
                self.assertIn("session id", payload["reason"])

    def test_missing_session_same_actor_session_end_audits_and_notifies_nonblocking(self) -> None:
        pending = Path("/tmp/session-end-session-unknown.md").resolve()
        args = types.SimpleNamespace(
            actor="claude",
            protocol="claude",
            event="session-end",
            non_blocking=True,
            auto_closeout=True,
            timeout=55,
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(self.module, "parse_args", return_value=args),
            mock.patch.object(self.module, "read_payload", return_value={}),
            mock.patch.object(self.module, "pending_paths", return_value=[pending]),
            mock.patch.object(self.module, "active_claim_rows", return_value=[]),
            mock.patch.object(
                self.module,
                "all_active_claim_rows",
                return_value=[
                    {
                        "path": str(pending),
                        "actor": "claude",
                        "session_hash": "3" * 16,
                    }
                ],
            ),
            mock.patch.object(self.module, "notify") as notified,
            mock.patch.object(self.module, "audit_lifecycle_failure") as audited,
            contextlib.redirect_stdout(stdout),
        ):
            returncode = self.module.main()
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.getvalue(), "")
        notified.assert_called_once()
        audited.assert_called_once()


class StopHookGitBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_stop_hook()
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tempdir.name).resolve()
        self.vault = self.root / "Agent记忆"
        self.vault.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Agent Memory Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        self.note = self.vault / "AGENTS.md"
        self.note.write_text("# Agent Memory\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "baseline")
        self.baseline = git(self.root, "rev-parse", "HEAD")
        self.log_path = self.root / "closeout.jsonl"
        self.module.GIT_ROOT = self.root
        self.module.VAULT_ROOT = self.vault
        self.module.LOG_PATH = self.log_path
        self.module.STATE_DB = self.root / "state.sqlite"

    def tearDown(self) -> None:
        try:
            self.tempdir.cleanup()
        except PermissionError:
            # Windows may briefly keep SQLite handles open after context exit.
            pass

    def record_bound_observation(
        self,
        path: Path,
        *,
        actor: str = "codex",
        commit: str | None = None,
    ) -> None:
        relative = path.relative_to(self.root).as_posix()
        observed_commit = commit or git(self.root, "rev-parse", "HEAD")
        blob_oid = git(self.root, "rev-parse", f"{observed_commit}:{relative}")
        blob_bytes = subprocess.run(
            ["git", "-C", str(self.root), "cat-file", "blob", blob_oid],
            capture_output=True,
            check=True,
        ).stdout
        with sqlite3.connect(self.module.STATE_DB) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_file_observations ("
                "path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, actor TEXT NOT NULL, "
                "session_hash TEXT NOT NULL DEFAULT '', git_commit TEXT NOT NULL, "
                "git_blob_oid TEXT NOT NULL, git_blob_sha256 TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_file_observations VALUES (?, ?, ?, 'session', ?, ?, ?)",
                (
                    str(path),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    actor,
                    observed_commit,
                    blob_oid,
                    hashlib.sha256(blob_bytes).hexdigest(),
                ),
            )

    def test_dirty_markdown_is_detected_under_renamed_vault(self) -> None:
        self.note.write_text("# Agent Memory\n\nChanged.\n", encoding="utf-8")
        self.assertEqual(self.module.dirty_paths(), [self.note.resolve()])

    def test_lexical_identity_preserves_unicode_and_windows_case_rules(self) -> None:
        unicode_path = self.vault / "项目" / "记忆-ß.md"
        normalized = self.module.normalize_path("Agent记忆/项目/记忆-ß.md")
        self.assertEqual(normalized, unicode_path.absolute())
        self.assertEqual(
            self.module.lexical_path_key(normalized),
            self.module.lexical_path_key(unicode_path),
        )
        case_variant = Path(str(unicode_path.parent).upper()) / unicode_path.name
        if os.name == "nt":
            self.assertEqual(
                self.module.lexical_path_key(case_variant),
                self.module.lexical_path_key(unicode_path),
            )
        else:
            self.assertNotEqual(
                self.module.lexical_path_key(case_variant),
                self.module.lexical_path_key(unicode_path),
            )

    def test_observation_lookup_uses_platform_lexical_case_key(self) -> None:
        self.record_bound_observation(self.note, commit=self.baseline)
        stored_path = self.note
        if os.name == "nt":
            stored_path = Path(str(self.note.parent).upper()) / self.note.name
            with sqlite3.connect(self.module.STATE_DB) as conn:
                conn.execute(
                    "UPDATE memory_file_observations SET path=?",
                    (str(stored_path),),
                )

        self.assertEqual(self.module.unobserved_paths([self.note.absolute()]), [])

    def test_porcelain_z_rename_keeps_lexical_old_and_new_paths(self) -> None:
        old_path = self.vault / "项目" / "旧记忆.md"
        new_path = self.vault / "项目" / "新记忆.md"
        old_path.parent.mkdir()
        old_path.write_text("# Rename me\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/项目/旧记忆.md")
        git(self.root, "commit", "-qm", "add rename source")
        rename_baseline = git(self.root, "rev-parse", "HEAD")
        git(self.root, "mv", "Agent记忆/项目/旧记忆.md", "Agent记忆/项目/新记忆.md")

        self.assertEqual(
            {self.module.lexical_path_key(path) for path in self.module.dirty_paths()},
            {
                self.module.lexical_path_key(old_path),
                self.module.lexical_path_key(new_path),
            },
        )
        self.assertEqual(
            self.module.parse_porcelain_z(
                "C  Agent记忆/项目/副本.md\0Agent记忆/项目/源.md\0"
            ),
            ["Agent记忆/项目/副本.md"],
        )
        git(self.root, "commit", "-qm", "rename memory")
        self.log_path.write_text(
            json.dumps({"git_observed_through": rename_baseline}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            {self.module.lexical_path_key(path) for path in self.module.historical_paths()},
            {
                self.module.lexical_path_key(old_path),
                self.module.lexical_path_key(new_path),
            },
        )

    def test_dirty_symlink_escape_keeps_lexical_path_pending_without_reading_target(self) -> None:
        external = self.root / "outside.md"
        external.write_text("# Outside target\n", encoding="utf-8")
        self.note.unlink()
        try:
            self.note.symlink_to(external)
        except OSError as exc:
            self.note.write_text("# Agent Memory\n", encoding="utf-8")
            self.skipTest(f"filesystem symlink unavailable: {exc}")
        self.assertIn(
            "T",
            git(self.root, "status", "--porcelain=v1", "--", "Agent记忆/AGENTS.md")[:2],
        )

        with mock.patch.object(
            self.module,
            "file_sha256",
            side_effect=AssertionError("dirty symlink target must never be read"),
        ), mock.patch.object(
            self.module,
            "parse_args",
            return_value=types.SimpleNamespace(
                actor="claude",
                protocol="claude",
                event="stop-hook",
                non_blocking=False,
                auto_closeout=True,
                timeout=30,
            ),
        ), mock.patch.object(
            self.module,
            "read_payload",
            return_value={"session_id": "symlink-stop-session"},
        ), mock.patch.object(
            self.module,
            "active_claim_rows",
            return_value=[],
        ), mock.patch.object(
            self.module,
            "all_active_claim_rows",
            return_value=[],
        ), mock.patch.object(
            self.module,
            "audit_lifecycle_failure",
        ), mock.patch.object(
            self.module,
            "notify",
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(self.module.dirty_paths(), [self.note.absolute()])
            self.assertEqual(self.module.pending_paths(), [self.note.absolute()])
            self.assertIsNone(self.module.safe_path_mtime(self.note.absolute()))
            self.assertEqual(self.module.main(), 0)
        decision = json.loads(stdout.getvalue())
        self.assertEqual(decision["decision"], "block")
        self.assertIn("not claimed", decision["reason"])

    def test_parent_component_symlink_escape_is_pending_when_supported(self) -> None:
        project = self.vault / "项目"
        note = project / "note.md"
        project.mkdir()
        note.write_text("# Project note\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/项目/note.md")
        git(self.root, "commit", "-qm", "add project note")
        external = self.root / "outside-project"
        external.mkdir()
        (external / "note.md").write_text("# External project note\n", encoding="utf-8")
        shutil.rmtree(project)
        try:
            project.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            project.mkdir()
            note.write_text("# Project note\n", encoding="utf-8")
            self.skipTest(f"directory symlink unavailable: {exc}")

        with mock.patch.object(
            self.module,
            "file_sha256",
            side_effect=AssertionError("parent symlink target must never be read"),
        ):
            self.assertIn(note.absolute(), self.module.dirty_paths())
            self.assertIn(note.absolute(), self.module.pending_paths())

    def test_historical_regular_to_symlink_mode_stays_pending_with_matching_bytes(self) -> None:
        self.record_bound_observation(self.note, commit=self.baseline)
        old_blob = git(self.root, "rev-parse", f"{self.baseline}:Agent记忆/AGENTS.md")
        git(self.root, "update-index", "--cacheinfo", f"120000,{old_blob},Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "change note to symlink mode")
        git(self.root, "config", "core.symlinks", "false")
        git(self.root, "checkout", "-f", "HEAD")
        self.assertEqual(
            git(self.root, "status", "--porcelain=v1", "--", "Agent记忆/AGENTS.md"),
            "",
        )
        self.assertTrue(
            git(self.root, "ls-tree", "HEAD", "--", "Agent记忆/AGENTS.md").startswith(
                "120000 blob "
            )
        )
        self.log_path.write_text(
            json.dumps({"git_observed_through": self.baseline}) + "\n",
            encoding="utf-8",
        )

        lexical_note = self.note.absolute()
        self.assertEqual(self.module.historical_paths(), [lexical_note])
        self.assertEqual(self.module.unobserved_paths([lexical_note]), [lexical_note])
        self.assertIn(lexical_note, self.module.pending_paths())

    def test_historical_regular_to_gitlink_mode_stays_pending(self) -> None:
        self.record_bound_observation(self.note, commit=self.baseline)
        git(self.root, "update-index", "--cacheinfo", f"160000,{self.baseline},Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "change note to gitlink mode")
        self.log_path.write_text(
            json.dumps({"git_observed_through": self.baseline}) + "\n",
            encoding="utf-8",
        )

        lexical_note = self.note.absolute()
        self.assertEqual(self.module.historical_paths(), [lexical_note])
        self.assertEqual(self.module.unobserved_paths([lexical_note]), [lexical_note])

    def test_missing_markdown_path_is_not_silently_dropped(self) -> None:
        missing = self.vault / "missing.md"
        self.assertEqual(
            self.module.normalize_path("Agent记忆/missing.md"),
            missing.resolve(),
        )
        self.assertEqual(self.module.unobserved_paths([missing]), [missing])

    def test_external_commit_after_observed_baseline_is_recovered(self) -> None:
        self.note.write_text("# Agent Memory\n\nCommitted externally.\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "external commit")
        self.log_path.write_text(
            json.dumps({"git_observed_through": self.baseline}) + "\n",
            encoding="utf-8",
        )

        self.assertEqual(self.module.historical_paths(), [self.note.resolve()])

    def test_pending_paths_ignores_content_with_matching_closeout_observation(self) -> None:
        self.note.write_text("# Agent Memory\n\nCommitted externally.\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "external commit")
        self.log_path.write_text(
            json.dumps({"git_observed_through": self.baseline}) + "\n",
            encoding="utf-8",
        )
        self.record_bound_observation(self.note)

        self.assertEqual(self.module.historical_paths(), [self.note.resolve()])
        self.assertEqual(self.module.pending_paths(), [])

        self.note.write_text("# Agent Memory\n\nChanged again.\n", encoding="utf-8")
        self.assertEqual(self.module.pending_paths(), [self.note.resolve()])

    def test_matching_self_attested_human_observation_cannot_silence_stop(self) -> None:
        self.note.write_text("# Agent Memory\n\nCommitted externally.\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "external commit")
        self.log_path.write_text(
            json.dumps({"git_observed_through": self.baseline}) + "\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(self.note.read_bytes()).hexdigest()
        with sqlite3.connect(self.module.STATE_DB) as conn:
            conn.execute(
                "CREATE TABLE memory_file_observations ("
                "path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, actor TEXT NOT NULL, "
                "session_hash TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO memory_file_observations(path, sha256, actor, session_hash) "
                "VALUES (?, ?, 'human', '')",
                (str(self.note), digest),
            )

        self.assertEqual(self.module.pending_paths(), [self.note.resolve()])


if __name__ == "__main__":
    unittest.main()
