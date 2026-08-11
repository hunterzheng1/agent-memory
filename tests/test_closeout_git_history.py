from __future__ import annotations

import contextlib
import importlib.util
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def load_closeout():
    path = SCRIPTS_ROOT / "agent_memory_closeout.py"
    spec = importlib.util.spec_from_file_location("test_closeout_module", path)
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


class CloseoutRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_closeout()
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tempdir.name).resolve()
        self.old_vault = self.root / "MemoryBeforeRename"
        self.new_vault = self.root / "Agent记忆"
        (self.old_vault / "项目").mkdir(parents=True)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Agent Memory Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        (self.old_vault / "项目" / "existing.md").write_text("# Existing\n", encoding="utf-8")
        git(self.root, "add", "MemoryBeforeRename/项目/existing.md")
        git(self.root, "commit", "-qm", "baseline")
        self.baseline = git(self.root, "rev-parse", "HEAD")
        self.module.REPO_ROOT = self.root
        self.module.VAULT_ROOT = self.new_vault

    def tearDown(self) -> None:
        try:
            self.tempdir.cleanup()
        except PermissionError:
            pass

    def migrate_without_commit(self) -> None:
        git(self.root, "mv", "MemoryBeforeRename", "Agent记忆")
        (self.new_vault / "项目" / "new.md").write_text("# New\n", encoding="utf-8")
        git(self.root, "add", "Agent记忆/项目/new.md")

    def assert_rename_and_add(self, entries) -> None:
        by_name = {entry.path.name: entry for entry in entries}
        self.assertEqual(set(by_name), {"existing.md", "new.md"})
        self.assertTrue(by_name["existing.md"].status.startswith("R"))
        self.assertEqual(
            by_name["existing.md"].previous_repo_path,
            "MemoryBeforeRename/项目/existing.md",
        )
        self.assertFalse(by_name["existing.md"].is_new)
        self.assertTrue(by_name["new.md"].is_new)

    def test_dirty_root_rename_is_not_treated_as_new_memory(self) -> None:
        self.migrate_without_commit()
        entries, warnings = self.module.git_status_entries()
        self.assertEqual(warnings, [])
        self.assert_rename_and_add(entries)

    def test_committed_root_rename_is_not_treated_as_new_memory(self) -> None:
        self.migrate_without_commit()
        git(self.root, "commit", "-qm", "rename vault")
        head = git(self.root, "rev-parse", "HEAD")
        entries, warnings = self.module.git_history_entries(self.baseline, head)
        self.assertEqual(warnings, [])
        self.assert_rename_and_add(entries)


class CloseoutHistoryCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_closeout()
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tempdir.name).resolve()
        self.vault = self.root / "AgentMemory"
        (self.vault / "项目").mkdir(parents=True)
        self.note = self.vault / "项目" / "note.md"
        self.metadata = self.vault / ".DS_Store"
        self.note.write_text("# Note\n", encoding="utf-8")
        self.metadata.write_bytes(b"metadata-v1")
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Agent Memory Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "add", "AgentMemory")
        git(self.root, "commit", "-qm", "baseline")
        self.baseline = git(self.root, "rev-parse", "HEAD")
        self.module.REPO_ROOT = self.root
        self.module.VAULT_ROOT = self.vault

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_history_and_dirty_status_ignore_tracked_non_markdown_metadata(self) -> None:
        self.note.write_text("# Note\n\nChanged.\n", encoding="utf-8")
        self.metadata.write_bytes(b"metadata-v2")
        dirty_entries, dirty_warnings = self.module.git_status_entries()
        self.assertEqual(dirty_warnings, [])
        self.assertEqual([entry.repo_path for entry in dirty_entries], ["AgentMemory/项目/note.md"])

        git(self.root, "add", "AgentMemory")
        git(self.root, "commit", "-qm", "external backup")
        head = git(self.root, "rev-parse", "HEAD")
        entries, warnings = self.module.git_history_entries(self.baseline, head)
        self.assertEqual(warnings, [])
        self.assertEqual([entry.repo_path for entry in entries], ["AgentMemory/项目/note.md"])

    def test_history_observation_is_bound_to_latest_path_commit_and_blob(self) -> None:
        self.module.STATE_DB = self.root / "state.sqlite"
        repo_path = "AgentMemory/项目/note.md"
        stable_content = b"# Stable observation\n"
        self.note.write_bytes(stable_content)
        git(self.root, "add", repo_path)
        git(self.root, "commit", "-qm", "observed snapshot")
        unrelated = self.root / "unrelated.txt"
        unrelated.write_text("later unrelated snapshot boundary\n", encoding="utf-8")
        git(self.root, "add", "unrelated.txt")
        git(self.root, "commit", "-qm", "later unrelated snapshot boundary")
        observation_commit = git(self.root, "rev-parse", "HEAD")
        observed_blob = git(self.root, "rev-parse", f"{observation_commit}:{repo_path}")
        blob_bytes = subprocess.run(
            ["git", "-C", str(self.root), "cat-file", "blob", observed_blob],
            capture_output=True,
            check=True,
        ).stdout
        with sqlite3.connect(self.module.STATE_DB) as conn:
            conn.execute(
                "CREATE TABLE memory_file_observations ("
                "path TEXT PRIMARY KEY, rel_path TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "actor TEXT NOT NULL, session_hash TEXT NOT NULL DEFAULT '', "
                "git_commit TEXT NOT NULL, git_blob_oid TEXT NOT NULL, "
                "git_blob_sha256 TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO memory_file_observations VALUES (?, ?, ?, 'codex', 'session', ?, ?, ?)",
                (
                    str(self.note),
                    "项目/note.md",
                    hashlib.sha256(stable_content).hexdigest(),
                    observation_commit,
                    observed_blob,
                    hashlib.sha256(blob_bytes).hexdigest(),
                ),
            )
        entry = self.module.GitEntry(status="M", repo_path=repo_path, path=self.note)

        self.assertEqual(self.module.unobserved_history_entries([entry]), [])

        self.note.write_text("# Different snapshot\n", encoding="utf-8")
        git(self.root, "add", repo_path)
        git(self.root, "commit", "-qm", "later different snapshot")
        self.note.write_bytes(stable_content)
        git(self.root, "add", repo_path)
        git(self.root, "commit", "-qm", "later same bytes")

        self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

    def _record_deleted_observation(
        self,
        path: Path,
        deletion_commit: str,
        prior_sha256: str,
        *,
        actor: str = "human",
        trash_sha256: str | None = None,
        evidence_ref_sha256: str | None = None,
    ) -> None:
        sentinel = f"deleted:{deletion_commit}:{prior_sha256}"
        parent_commit = git(self.root, "rev-parse", f"{deletion_commit}^")
        with contextlib.closing(sqlite3.connect(self.module.STATE_DB)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_file_observations "
                "(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, actor TEXT NOT NULL, "
                "session_hash TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_deletion_observations (
                  observation_id TEXT PRIMARY KEY, path TEXT NOT NULL, rel_path TEXT NOT NULL,
                  sentinel TEXT NOT NULL, actor TEXT NOT NULL, user_authorized INTEGER NOT NULL,
                  approval_trust TEXT NOT NULL DEFAULT 'self_attested',
                  can_authorize_action INTEGER NOT NULL DEFAULT 0,
                  approval_receipt_sha256 TEXT NOT NULL DEFAULT '',
                  deletion_commit TEXT NOT NULL, parent_commit TEXT NOT NULL,
                  prior_sha256 TEXT NOT NULL, trash_sha256 TEXT NOT NULL,
                  trash_path_sha256 TEXT NOT NULL, evidence_ref_sha256 TEXT NOT NULL,
                  evidence_ref_length INTEGER NOT NULL, observed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_file_observations"
                "(path, sha256, actor, session_hash) VALUES (?, ?, 'human', '')",
                (str(path), sentinel),
            )
            conn.execute(
                "INSERT INTO memory_deletion_observations "
                "(observation_id, path, rel_path, sentinel, actor, user_authorized, "
                "approval_trust, can_authorize_action, approval_receipt_sha256, "
                "deletion_commit, parent_commit, prior_sha256, trash_sha256, "
                "trash_path_sha256, evidence_ref_sha256, evidence_ref_length, observed_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'trusted_verifier', 1, ?, ?, ?, ?, ?, ?, ?, 12, ?)",
                (
                    hashlib.sha256((str(path) + actor).encode("utf-8")).hexdigest(),
                    str(path),
                    path.relative_to(self.vault).as_posix(),
                    sentinel,
                    actor,
                    "ab" * 32,
                    deletion_commit,
                    parent_commit,
                    prior_sha256,
                    trash_sha256 or prior_sha256,
                    "b" * 64,
                    evidence_ref_sha256 if evidence_ref_sha256 is not None else "e" * 64,
                    "2026-08-02T00:00:00+00:00",
                ),
            )

    def _committed_delete(self, filename: str) -> tuple[Path, bytes, str]:
        deleted = self.vault / "项目" / filename
        content = f"# {filename}\n".encode("utf-8")
        deleted.write_bytes(content)
        git(self.root, "add", f"AgentMemory/项目/{filename}")
        git(self.root, "commit", "-qm", "add note")
        deleted.unlink()
        git(self.root, "add", "-u", f"AgentMemory/项目/{filename}")
        git(self.root, "commit", "-qm", "authorized deletion")
        return deleted, content, git(self.root, "rev-parse", "HEAD")

    def test_history_accepts_only_exact_audited_latest_deletion(self) -> None:
        deleted, content, deletion_commit = self._committed_delete("deleted.md")
        self.module.STATE_DB = self.root / "state.sqlite"
        self._record_deleted_observation(
            deleted,
            deletion_commit,
            hashlib.sha256(content).hexdigest(),
        )
        entry = self.module.GitEntry(
            status="D",
            repo_path="AgentMemory/项目/deleted.md",
            path=deleted,
        )
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [])

            git(self.root, "commit", "--allow-empty", "-qm", "unrelated later commit")
            self.assertEqual(self.module.unobserved_history_entries([entry]), [])

        deleted.write_bytes(content)
        git(self.root, "add", "AgentMemory/项目/deleted.md")
        git(self.root, "commit", "-qm", "restore")
        deleted.unlink()
        git(self.root, "add", "-u", "AgentMemory/项目/deleted.md")
        git(self.root, "commit", "-qm", "later deletion")
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

    def test_history_rejects_self_attested_deletion_even_if_fields_look_valid(self) -> None:
        deleted, content, deletion_commit = self._committed_delete("self-attested.md")
        self.module.STATE_DB = self.root / "state.sqlite"
        self._record_deleted_observation(
            deleted,
            deletion_commit,
            hashlib.sha256(content).hexdigest(),
        )
        entry = self.module.GitEntry(
            status="D",
            repo_path="AgentMemory/项目/self-attested.md",
            path=deleted,
        )
        self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

    def test_history_rejects_invalid_deletion_audit_fields(self) -> None:
        deleted, content, deletion_commit = self._committed_delete("invalid.md")
        self.module.STATE_DB = self.root / "state.sqlite"
        self._record_deleted_observation(
            deleted,
            deletion_commit,
            hashlib.sha256(content).hexdigest(),
            actor="migration",
        )
        entry = self.module.GitEntry(
            status="D",
            repo_path="AgentMemory/项目/invalid.md",
            path=deleted,
        )
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

        with contextlib.closing(sqlite3.connect(self.module.STATE_DB)) as conn, conn:
            conn.execute(
                "UPDATE memory_deletion_observations SET actor='human', trash_sha256=?",
                ("c" * 64,),
            )
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

        with contextlib.closing(sqlite3.connect(self.module.STATE_DB)) as conn, conn:
            conn.execute(
                "UPDATE memory_deletion_observations "
                "SET trash_sha256=prior_sha256, evidence_ref_sha256=''"
            )
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

        with contextlib.closing(sqlite3.connect(self.module.STATE_DB)) as conn, conn:
            conn.execute(
                "UPDATE memory_deletion_observations SET evidence_ref_sha256=?, parent_commit=?",
                ("e" * 64, "a" * 40),
            )
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])


class CloseoutReconcileStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_closeout()
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.vault = Path(self.tempdir.name).resolve() / "AgentMemory"
        (self.vault / "项目").mkdir(parents=True)
        self.module.VAULT_ROOT = self.vault

    def tearDown(self) -> None:
        try:
            self.tempdir.cleanup()
        except PermissionError:
            pass

    def test_archived_history_does_not_block_active_fact_reconcile(self) -> None:
        archived = self.vault / "项目" / "history.md"
        archived.write_text(
            "---\nmemory_type: project_history\nstatus: archived\n---\n\n# History\n",
            encoding="utf-8",
        )
        entry = self.module.GitEntry(
            status="A",
            repo_path="AgentMemory/项目/history.md",
            path=archived,
        )
        args = Namespace(reconcile_all=False, limit=8, no_zvec=False)
        with mock.patch.object(
            self.module,
            "search_memory",
            side_effect=AssertionError("archived history must not enter duplicate search"),
        ):
            findings, warnings = self.module.postwrite_reconcile([entry], args)

        self.assertEqual(findings, [])
        self.assertEqual(warnings, [])

    def test_project_track_infers_current_project_for_postwrite_search(self) -> None:
        project_note = self.vault / "项目" / "project-a.md"
        project_note.write_text(
            "---\n"
            "memory_type: project\n"
            "track: project\n"
            "project_id: project-a\n"
            "status: active\n"
            "---\n\n"
            "# Project A\n\nA scoped deployment rule.\n",
            encoding="utf-8",
        )
        entry = self.module.GitEntry(
            status="A",
            repo_path="AgentMemory/项目/project-a.md",
            path=project_note,
        )
        args = Namespace(reconcile_all=False, limit=8, no_zvec=True, current_project="")
        with mock.patch.object(self.module, "search_memory", return_value=([], [])) as search:
            findings, warnings = self.module.postwrite_reconcile([entry], args)

        self.assertEqual(findings, [])
        self.assertEqual(warnings, [])
        self.assertEqual(search.call_args.kwargs["current_project"], "project-a")

    def test_postwrite_fails_closed_for_ambiguous_workflow_or_decision_scope(self) -> None:
        for directory, memory_type in (("工作流", "workflow"), ("决策", "decision")):
            with self.subTest(memory_type=memory_type):
                note = self.vault / directory / f"ambiguous-{memory_type}.md"
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(
                    "---\n"
                    f"memory_type: {memory_type}\n"
                    f"track: {memory_type}\n"
                    "project_id:\n  - project-a\n  - project-b\n"
                    "status: active\n---\n# Ambiguous\n",
                    encoding="utf-8",
                )
                entry = self.module.GitEntry(
                    status="A",
                    repo_path=f"AgentMemory/{directory}/{note.name}",
                    path=note,
                )
                args = Namespace(reconcile_all=False, limit=8, no_zvec=True, current_project="")
                with mock.patch.object(self.module, "search_memory") as search:
                    findings, warnings = self.module.postwrite_reconcile([entry], args)
                search.assert_not_called()
                self.assertEqual(findings[0]["action"], "ASK_USER")
                self.assertEqual(findings[0]["reason_code"], "AMBIGUOUS_PROJECT_SCOPE")
                self.assertIn("AMBIGUOUS_PROJECT_SCOPE", warnings)

    def test_frontmatter_boilerplate_is_not_used_as_fallback_summary(self) -> None:
        note = self.vault / "项目" / "new-project.md"
        note.write_text(
            "---\n"
            "memory_type: project\n"
            "track: project\n"
            "app_id: agent-memory\n"
            "agent_scope: shared\n"
            "status: active\n"
            "---\n\n"
            "# Unique Project\n\n"
            "unique_project_marker_20260712\n",
            encoding="utf-8",
        )

        query = self.module.reconcile_query_for_file(note)
        self.assertIn("unique_project_marker_20260712", query)
        self.assertNotIn("memory_type", query)
        self.assertNotIn("agent_scope", query)

    def test_postwrite_ignores_navigation_and_template_candidates(self) -> None:
        note = self.vault / "项目" / "new-project.md"
        note.write_text("# Unique Project\n\nunique_project_marker_20260712\n", encoding="utf-8")
        entry = self.module.GitEntry(
            status="A",
            repo_path="AgentMemory/项目/new-project.md",
            path=note,
        )
        rows = [
            {
                "path": str(self.vault / "INDEX.md"),
                "rel_path": "INDEX.md",
                "title": "Agent Memory Index",
                "memory_type": "directory_index",
                "summary": "Unique Project unique_project_marker_20260712",
                "hit": "Unique Project unique_project_marker_20260712",
                "sources": ["sqlite"],
            }
        ]
        args = Namespace(
            reconcile_all=False,
            limit=8,
            no_zvec=True,
            merge_threshold=0.42,
            merge_coverage_threshold=0.35,
            semantic_merge_threshold=0.32,
        )
        with mock.patch.object(self.module, "search_memory", return_value=(rows, [])):
            findings, warnings = self.module.postwrite_reconcile([entry], args)

        self.assertEqual(findings, [])
        self.assertEqual(warnings, [])

    def test_history_rejects_legacy_observation_without_commit_binding(self) -> None:
        note = self.vault / "项目" / "observed.md"
        note.write_text("# Observed\n", encoding="utf-8")
        entry = self.module.GitEntry(
            status="M",
            repo_path="AgentMemory/项目/observed.md",
            path=note,
        )
        self.module.STATE_DB = self.vault.parent / "state.sqlite"
        with sqlite3.connect(self.module.STATE_DB) as conn:
            conn.execute(
                "CREATE TABLE memory_file_observations (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                "actor TEXT NOT NULL, session_hash TEXT NOT NULL DEFAULT '')"
            )

        self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

        digest = hashlib.sha256(note.read_bytes()).hexdigest()
        with sqlite3.connect(self.module.STATE_DB) as conn:
            conn.execute(
                "INSERT INTO memory_file_observations(path, sha256, actor, session_hash) "
                "VALUES (?, ?, 'codex', '1')",
                (str(note), digest),
            )
        self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

        note.write_text("# Observed\n\nChanged.\n", encoding="utf-8")
        self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])

    def test_committed_recovery_requires_independent_stored_receipt_verifier(self) -> None:
        git_root = self.vault.parent
        git(git_root, "init", "-q")
        git(git_root, "config", "user.name", "Recovery Test")
        git(git_root, "config", "user.email", "recovery@example.invalid")
        note = self.vault / "项目" / "committed-recovery.md"
        note.write_text("# Committed recovery\n", encoding="utf-8")
        repo_path = "AgentMemory/项目/committed-recovery.md"
        git(git_root, "add", repo_path)
        git(git_root, "commit", "-qm", "committed recovery")
        proposal_commit = git(git_root, "rev-parse", "HEAD")
        blob_oid = git(git_root, "rev-parse", f"{proposal_commit}:{repo_path}")
        blob_bytes = subprocess.run(
            ["git", "-C", str(git_root), "cat-file", "blob", blob_oid],
            capture_output=True,
            check=True,
        ).stdout
        digest = hashlib.sha256(note.read_bytes()).hexdigest()
        entry = self.module.GitEntry(
            status="M",
            repo_path=repo_path,
            path=note,
        )
        self.module.REPO_ROOT = git_root
        self.module.STATE_DB = git_root / "state.sqlite"
        with sqlite3.connect(self.module.STATE_DB) as conn:
            conn.execute(
                "CREATE TABLE memory_file_observations ("
                "path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, actor TEXT NOT NULL, "
                "session_hash TEXT NOT NULL DEFAULT '', git_commit TEXT NOT NULL, "
                "git_blob_oid TEXT NOT NULL, git_blob_sha256 TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO memory_file_observations VALUES (?, ?, 'human', '', ?, ?, ?)",
                (
                    str(note),
                    digest,
                    proposal_commit,
                    blob_oid,
                    hashlib.sha256(blob_bytes).hexdigest(),
                ),
            )
            conn.execute(
                """
                CREATE TABLE memory_committed_observations (
                  observation_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                  actor TEXT NOT NULL, user_authorized INTEGER NOT NULL,
                  approval_trust TEXT NOT NULL, can_authorize_action INTEGER NOT NULL,
                  approval_receipt_sha256 TEXT NOT NULL, intent_id TEXT NOT NULL,
                  receipt_id TEXT NOT NULL, proposal_commit TEXT NOT NULL,
                  observed_git_head TEXT NOT NULL, audit_chain_sha256 TEXT NOT NULL,
                  evidence_ref_sha256 TEXT NOT NULL, evidence_ref_length INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO memory_committed_observations VALUES "
                "(?, ?, ?, 'human', 1, 'trusted_verifier', 1, ?, ?, ?, ?, ?, ?, ?, 12)",
                (
                    "a" * 64,
                    str(note),
                    digest,
                    "b" * 64,
                    "c" * 32,
                    "d" * 32,
                    proposal_commit,
                    proposal_commit,
                    "1" * 64,
                    "2" * 64,
                ),
            )

        self.assertEqual(self.module.unobserved_history_entries([entry]), [entry])
        with mock.patch.object(
            self.module,
            "stored_observation_has_trusted_approval",
            return_value=True,
        ):
            self.assertEqual(self.module.unobserved_history_entries([entry]), [])


class CloseoutCommitSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_closeout()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        self.vault = self.root / "Agent记忆"
        self.vault.mkdir()
        self.note = self.vault / "AGENTS.md"
        self.note.write_text("# Rules\n\nOriginal.\n", encoding="utf-8")
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Snapshot Test")
        git(self.root, "config", "user.email", "snapshot@example.invalid")
        git(self.root, "add", "Agent记忆/AGENTS.md")
        git(self.root, "commit", "-qm", "baseline")
        self.baseline = git(self.root, "rev-parse", "HEAD")
        self.module.REPO_ROOT = self.root
        self.module.VAULT_ROOT = self.vault
        self.module.CONFIG_ROOT = self.root / "runtime"
        self.args = Namespace(commit=True, dry_run=False, message="snapshot commit", actor="codex")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_validated_content_changed_before_snapshot_is_not_committed(self) -> None:
        expected_content = b"# Rules\n\nApproved.\n"
        expected_hash = hashlib.sha256(expected_content).hexdigest()
        self.note.write_bytes(b"# Rules\n\nRaced.\n")
        result = self.module.commit_files(
            [self.note],
            self.args,
            expected_raw_sha256={self.note: expected_hash},
            expected_head=self.baseline,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "validated_snapshot_verify")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.baseline)

    def test_worktree_race_after_snapshot_cannot_change_commit_blob(self) -> None:
        approved = b"# Rules\n\nApproved.\n"
        raced = "# Rules\n\nRaced after snapshot.\n"
        self.note.write_bytes(approved)
        expected_hash = hashlib.sha256(approved).hexdigest()
        original_builder = self.module._build_isolated_commit

        def race_then_commit(snapshots, *, expected_head, message):
            self.note.write_text(raced, encoding="utf-8")
            return original_builder(snapshots, expected_head=expected_head, message=message)

        with mock.patch.object(self.module, "_build_isolated_commit", side_effect=race_then_commit):
            result = self.module.commit_files(
                [self.note],
                self.args,
                expected_raw_sha256={self.note: expected_hash},
                expected_head=self.baseline,
            )
        self.assertTrue(result["ok"], result)
        committed = git(self.root, "show", f"{result['commit']}:Agent记忆/AGENTS.md")
        self.assertEqual(committed, approved.decode("utf-8").strip())
        self.assertEqual(self.note.read_text(encoding="utf-8"), raced)

    def test_isolated_commit_applies_repo_clean_filter_without_weakening_raw_cas(self) -> None:
        filter_script = self.root / "fixture_clean_filter.py"
        filter_script.write_text(
            "import sys\n"
            "payload = sys.stdin.buffer.read().replace(b'WORKTREE', b'FILTERED')\n"
            "sys.stdout.buffer.write(payload)\n",
            encoding="utf-8",
        )
        filter_command = f'"{sys.executable}" "{filter_script}"'
        git(self.root, "config", "core.autocrlf", "true")
        git(self.root, "config", "filter.fixture.clean", filter_command)
        (self.root / ".gitattributes").write_text(
            "Agent记忆/AGENTS.md filter=fixture text eol=lf\n",
            encoding="utf-8",
        )
        git(self.root, "add", ".gitattributes")
        git(self.root, "commit", "-qm", "configure clean filter")
        self.baseline = git(self.root, "rev-parse", "HEAD")

        approved = b"# WORKTREE\r\n\r\nApproved.\r\n"
        self.note.write_bytes(approved)
        expected_hash = hashlib.sha256(approved).hexdigest()
        result = self.module.commit_files(
            [self.note],
            self.args,
            expected_raw_sha256={self.note: expected_hash},
            expected_head=self.baseline,
        )
        self.assertTrue(result["ok"], result)
        completed = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{result['commit']}:Agent记忆/AGENTS.md"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(completed.stdout, b"# FILTERED\n\nApproved.\n")
        self.assertEqual(self.note.read_bytes(), approved)
        self.assertEqual(result["snapshot_sha256"]["Agent记忆/AGENTS.md"], expected_hash)

    def test_full_closeout_rejects_ordinary_file_changed_after_check(self) -> None:
        checked = "# Rules\n\nChecked ordinary content.\n"
        raced = "# Rules\n\nChanged after check.\n"
        self.note.write_text(checked, encoding="utf-8")
        self.module.STATE_DB = self.root / "runtime" / "state.sqlite"

        argv = [
            "agent_memory_closeout.py",
            "--commit",
            "--commit-warnings",
            "--skip-zvec",
            "--skip-audit",
            "--no-zvec",
            "--json",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = self.module.parse_args()

        def check_then_race(_files, _args):
            self.note.write_text(raced, encoding="utf-8")
            return {"ok": True, "advisories": [], "detail": "ok"}

        gate = {
            "ok": True,
            "enabled": True,
            "requested_mode": "enforce",
            "mode": "enforce",
            "blocking": False,
            "matched": [],
            "violations": [],
        }
        ok_step = {"ok": True, "skipped": True, "detail": "test"}
        with (
            mock.patch.object(self.module, "run_check", side_effect=check_then_race),
            mock.patch.object(self.module, "postwrite_reconcile", return_value=([], [])),
            mock.patch.object(self.module, "run_index", return_value=ok_step),
            mock.patch.object(self.module, "run_zvec", return_value=ok_step),
            mock.patch.object(self.module, "run_agent_evolution", return_value=ok_step),
            mock.patch.object(self.module, "run_audit_autorun", return_value=ok_step),
            mock.patch.object(self.module, "append_log"),
            mock.patch.object(
                self.module.write_intent,
                "enforce_protected_changes",
                return_value=gate,
            ),
        ):
            payload = self.module.run_closeout(args)

        self.assertEqual(payload["status"], "error")
        self.assertEqual(
            payload["steps"]["commit"]["detail"],
            "CONTENT_CHANGED_AFTER_CHECK",
        )
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.baseline)

if __name__ == "__main__":
    unittest.main()
