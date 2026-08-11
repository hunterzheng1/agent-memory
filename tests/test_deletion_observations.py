from __future__ import annotations

import hashlib
import contextlib
import json
import os
import shutil
import sqlite3
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

import agent_memory_claim as claim
from agent_memory_claim import parse_deleted_observation


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


class DeletionObservationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        self.git_root = self.root / "git"
        self.vault = self.git_root / "AgentMemory"
        self.target = self.vault / "项目" / "deleted-note.md"
        self.trash_dir = self.root / ".Trash"
        self.trash_file = self.trash_dir / "deleted-note.md"
        self.runtime = self.root / "runtime"
        self.state_db = self.runtime / "state.sqlite"
        self.content = b"# Deleted note\n\nRecoverable content.\n"

        self.target.parent.mkdir(parents=True)
        self.trash_dir.mkdir()
        self.runtime.mkdir()
        self.target.write_bytes(self.content)
        git(self.git_root, "init", "-q", "--initial-branch=main")
        git(self.git_root, "config", "user.name", "Deletion Observation Test")
        git(self.git_root, "config", "user.email", "test@example.invalid")
        git(self.git_root, "add", "AgentMemory")
        git(self.git_root, "commit", "-qm", "baseline")
        self.baseline_commit = git(self.git_root, "rev-parse", "HEAD")

        helper = self.git_root / "unrelated.txt"
        helper.write_text("unrelated\n", encoding="utf-8")
        git(self.git_root, "add", "unrelated.txt")
        git(self.git_root, "commit", "-qm", "unrelated ancestor")
        self.non_deletion_commit = git(self.git_root, "rev-parse", "HEAD")

        self.target.replace(self.trash_file)
        git(self.git_root, "add", "-u", "--", "AgentMemory/项目/deleted-note.md")
        git(self.git_root, "commit", "-qm", "authorized deletion")
        self.deletion_commit = git(self.git_root, "rev-parse", "HEAD")

        config_path = self.runtime / "agent-memory.toml"
        config_path.write_text(
            "\n".join(
                (
                    f"memory_root = {json.dumps(str(self.vault), ensure_ascii=False)}",
                    f"git_root = {json.dumps(str(self.git_root), ensure_ascii=False)}",
                    f"config_root = {json.dumps(str(self.runtime), ensure_ascii=False)}",
                    f"state_db = {json.dumps(str(self.state_db), ensure_ascii=False)}",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.env = isolated_subprocess_env(
            {
                "AGENT_MEMORY_CONFIG_FILE": str(config_path),
                "AGENT_MEMORY_TRASH_ROOT": str(self.trash_dir),
                "HOME": str(self.root),
                "USERPROFILE": str(self.root),
            }
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def observe(
        self,
        *,
        target_file: Path | None = None,
        commit: str | None = None,
        trash_file: Path | None = None,
        authorized: bool = True,
        apply: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPTS / "memoryctl"),
            "--actor",
            "human",
            "observe-deletion",
            "--file",
            str(target_file or self.target),
            "--trash-path",
            str(trash_file or self.trash_file),
            "--deletion-commit",
            commit or self.deletion_commit,
            "--evidence-ref",
            "current-user-turn-explicit-delete",
            "--json",
        ]
        if authorized:
            command.append("--confirm-user-authorized")
        if apply:
            command.append("--apply")
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_preview_is_self_attested_and_cli_apply_fails_closed_without_writes(self) -> None:
        preview = self.observe()
        self.assertEqual(preview.returncode, 0, preview.stderr + preview.stdout)
        preview_payload = json.loads(preview.stdout)
        self.assertTrue(preview_payload["preview"])
        self.assertEqual(preview_payload["applied"], 0)
        self.assertEqual(
            preview_payload["observation"]["approval_trust"],
            "self_attested",
        )
        self.assertFalse(preview_payload["observation"]["can_authorize_action"])
        self.assertFalse(self.state_db.exists(), "preview must not create or modify the state database")

        applied = self.observe(apply=True)
        self.assertEqual(applied.returncode, 2, applied.stderr + applied.stdout)
        self.assertEqual(
            json.loads(applied.stdout)["error"],
            "TRUSTED_APPROVAL_VERIFIER_REQUIRED",
        )
        self.assertFalse(self.target.exists())
        self.assertEqual(self.trash_file.read_bytes(), self.content)
        self.assertNotIn(str(self.trash_file), applied.stdout)
        self.assertNotIn("current-user-turn-explicit-delete", applied.stdout)
        self.assertFalse(self.state_db.exists())

        prior_sha256 = hashlib.sha256(self.content).hexdigest()
        expected_sentinel = f"deleted:{self.deletion_commit}:{prior_sha256}"
        self.assertEqual(parse_deleted_observation(expected_sentinel), (self.deletion_commit, prior_sha256))
        self.assertIsNone(parse_deleted_observation(f"deleted:{self.deletion_commit}:bad"))

    def test_injected_trusted_verifier_records_bound_audit(self) -> None:
        patches = (
            mock.patch.object(claim, "VAULT_ROOT", self.vault),
            mock.patch.object(claim, "GIT_ROOT", self.git_root),
            mock.patch.object(claim, "STATE_DB", self.state_db),
            mock.patch.object(claim, "CONFIG_ROOT", self.runtime),
            mock.patch.object(
                claim,
                "DELETION_OBSERVATION_LOCK",
                self.runtime / "locks" / "closeout.lock",
            ),
            mock.patch.dict(
                os.environ,
                {"AGENT_MEMORY_TRASH_ROOT": str(self.trash_dir)},
            ),
            mock.patch.object(claim, "_is_recognized_trash_path", return_value=True),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            observation = claim.validate_deletion_observation(
                actor="human",
                target_file=str(self.target),
                trash_file=str(self.trash_file),
                deletion_commit=self.deletion_commit,
                evidence_ref="current-user-turn-explicit-delete",
                user_authorized=True,
            )
            applied = claim.apply_deletion_observation(
                observation,
                actor="human",
                target_file=str(self.target),
                trash_file=str(self.trash_file),
                deletion_commit=self.deletion_commit,
                evidence_ref="current-user-turn-explicit-delete",
                user_authorized=True,
                approval_verifier=lambda _subject: {
                    "approval_trust": "trusted_verifier",
                    "can_authorize_action": True,
                    "receipt_sha256": "ab" * 32,
                },
            )
        self.assertEqual(applied, 1)
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            audit = conn.execute(
                "SELECT approval_trust, can_authorize_action, approval_receipt_sha256 "
                "FROM memory_deletion_observations"
            ).fetchone()
            observed = conn.execute(
                "SELECT sha256, actor, session_hash FROM memory_file_observations"
            ).fetchone()
        self.assertEqual(audit, ("trusted_verifier", 1, "ab" * 32))
        self.assertEqual(observed[1:], ("human", ""))

    def test_missing_explicit_authorization_is_rejected_without_writes(self) -> None:
        completed = self.observe(authorized=False, apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("explicit user authorization", json.loads(completed.stdout)["error"])
        self.assertFalse(self.state_db.exists())

    def test_trash_hash_mismatch_is_rejected_without_writes(self) -> None:
        self.trash_file.write_text("different content\n", encoding="utf-8")
        completed = self.observe(apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("does not match", json.loads(completed.stdout)["error"])
        self.assertFalse(self.state_db.exists())

    def test_trash_symlink_is_rejected_without_writes(self) -> None:
        trash_link = self.trash_dir / "deleted-note-link.md"
        try:
            trash_link.symlink_to(self.trash_file)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        completed = self.observe(trash_file=trash_link, apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("regular file", json.loads(completed.stdout)["error"])
        self.assertFalse(self.state_db.exists())

    def test_lookalike_trash_component_is_rejected_without_writes(self) -> None:
        lookalike = self.root / "ordinary" / ".Trash"
        lookalike.mkdir(parents=True)
        fake_trash = lookalike / "deleted-note.md"
        shutil.copy2(self.trash_file, fake_trash)
        completed = self.observe(trash_file=fake_trash, apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("recognized Trash", json.loads(completed.stdout)["error"])
        self.assertFalse(self.state_db.exists())

    def test_staged_readd_with_missing_worktree_file_is_rejected(self) -> None:
        self.target.write_bytes(self.content)
        git(self.git_root, "add", "AgentMemory/项目/deleted-note.md")
        self.target.replace(self.trash_dir / "staged-readd.md")
        completed = self.observe(apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("uncommitted Git index or worktree", json.loads(completed.stdout)["error"])
        self.assertFalse(self.state_db.exists())

    def test_apply_revalidates_trash_after_preview(self) -> None:
        patches = (
            mock.patch.object(claim, "VAULT_ROOT", self.vault),
            mock.patch.object(claim, "GIT_ROOT", self.git_root),
            mock.patch.object(claim, "STATE_DB", self.state_db),
            mock.patch.object(claim, "CONFIG_ROOT", self.runtime),
            mock.patch.object(
                claim,
                "DELETION_OBSERVATION_LOCK",
                self.runtime / "locks" / "closeout.lock",
            ),
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.root),
                    "USERPROFILE": str(self.root),
                    "AGENT_MEMORY_TRASH_ROOT": str(self.trash_dir),
                },
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            observation = claim.validate_deletion_observation(
                actor="human",
                target_file=str(self.target),
                trash_file=str(self.trash_file),
                deletion_commit=self.deletion_commit,
                evidence_ref="current-user-turn-explicit-delete",
                user_authorized=True,
            )
            self.trash_file.write_text("changed after preview\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                claim.apply_deletion_observation(
                    observation,
                    actor="human",
                    target_file=str(self.target),
                    trash_file=str(self.trash_file),
                    deletion_commit=self.deletion_commit,
                    evidence_ref="current-user-turn-explicit-delete",
                    user_authorized=True,
                )
        self.assertFalse(self.state_db.exists())

    def test_non_ancestor_deletion_commit_is_rejected(self) -> None:
        side_trash = self.trash_dir / "side-deleted-note.md"
        shutil.copy2(self.trash_file, side_trash)
        git(self.git_root, "checkout", "-q", "-b", "side", self.baseline_commit)
        self.target.replace(side_trash)
        git(self.git_root, "add", "-u", "--", "AgentMemory/项目/deleted-note.md")
        git(self.git_root, "commit", "-qm", "side deletion")
        side_commit = git(self.git_root, "rev-parse", "HEAD")
        git(self.git_root, "checkout", "-q", "main")

        completed = self.observe(commit=side_commit, trash_file=side_trash, apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("not an ancestor", json.loads(completed.stdout)["error"])
        self.assertFalse(self.state_db.exists())

    def test_ancestor_commit_that_did_not_delete_target_is_rejected(self) -> None:
        completed = self.observe(commit=self.non_deletion_commit, apply=True)
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        error = json.loads(completed.stdout)["error"]
        self.assertTrue("still contains" in error or "did not delete" in error)
        self.assertFalse(self.state_db.exists())

    def test_git_mv_is_rejected_as_rename_not_true_deletion(self) -> None:
        old_path = self.vault / "项目" / "rename-old.md"
        new_path = self.vault / "项目" / "rename-new.md"
        rename_trash = self.trash_dir / "rename-old.md"
        old_path.write_bytes(b"# Rename source\n")
        git(self.git_root, "add", "AgentMemory/项目/rename-old.md")
        git(self.git_root, "commit", "-qm", "add rename source")
        shutil.copy2(old_path, rename_trash)
        git(
            self.git_root,
            "mv",
            "AgentMemory/项目/rename-old.md",
            "AgentMemory/项目/rename-new.md",
        )
        git(self.git_root, "commit", "-qm", "rename memory")
        rename_commit = git(self.git_root, "rev-parse", "HEAD")

        completed = self.observe(
            target_file=old_path,
            commit=rename_commit,
            trash_file=rename_trash,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("rename", json.loads(completed.stdout)["error"].lower())
        self.assertTrue(new_path.is_file())

    def _commit_nonregular_then_delete(self, mode: str, repo_path: str) -> tuple[Path, str]:
        target = self.git_root / repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            hashed = subprocess.run(
                ["git", "-C", str(self.git_root), "hash-object", "-w", "--stdin"],
                input=b"destination.md",
                capture_output=True,
                check=True,
            ).stdout.decode("ascii").strip()
            trash_bytes = b"destination.md"
        else:
            hashed = git(self.git_root, "rev-parse", "HEAD")
            trash_bytes = b"gitlink evidence is intentionally not a blob\n"
        git(self.git_root, "update-index", "--add", "--cacheinfo", mode, hashed, repo_path)
        git(self.git_root, "commit", "-qm", f"add mode {mode}")
        git(self.git_root, "rm", "--cached", "-q", "--", repo_path)
        git(self.git_root, "commit", "-qm", f"delete mode {mode}")
        deletion_commit = git(self.git_root, "rev-parse", "HEAD")
        trash = self.trash_dir / Path(repo_path).name
        trash.write_bytes(trash_bytes)
        return target, deletion_commit

    def test_symlink_tree_mode_is_rejected_even_when_blob_bytes_match(self) -> None:
        target, deletion_commit = self._commit_nonregular_then_delete(
            "120000",
            "AgentMemory/项目/symlink.md",
        )
        completed = self.observe(
            target_file=target,
            commit=deletion_commit,
            trash_file=self.trash_dir / "symlink.md",
        )
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("regular blob mode", json.loads(completed.stdout)["error"])

    def test_submodule_tree_mode_is_rejected(self) -> None:
        target, deletion_commit = self._commit_nonregular_then_delete(
            "160000",
            "AgentMemory/项目/submodule.md",
        )
        completed = self.observe(
            target_file=target,
            commit=deletion_commit,
            trash_file=self.trash_dir / "submodule.md",
        )
        self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
        self.assertIn("regular blob mode", json.loads(completed.stdout)["error"])

    def test_unicode_path_uses_exact_nul_delimited_tree_entry(self) -> None:
        target = self.vault / "项目" / "删除 记忆-ß.md"
        trash = self.trash_dir / target.name
        target.write_bytes("# Unicode deletion\n".encode("utf-8"))
        git(self.git_root, "add", "--", "AgentMemory/项目/删除 记忆-ß.md")
        git(self.git_root, "commit", "-qm", "add unicode memory")
        target.replace(trash)
        git(self.git_root, "add", "-u", "--", "AgentMemory/项目/删除 记忆-ß.md")
        git(self.git_root, "commit", "-qm", "delete unicode memory")
        deletion_commit = git(self.git_root, "rev-parse", "HEAD")

        completed = self.observe(
            target_file=target,
            commit=deletion_commit,
            trash_file=trash,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout)["observation"]["rel_path"],
            "项目/删除 记忆-ß.md",
        )


class TrashPathClassificationTests(unittest.TestCase):
    def test_configured_trash_root_must_be_disjoint_from_git_and_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            git_root = root / "git"
            vault = git_root / "vault"
            nested = git_root / "trash"
            separate = root / "recovery"
            for directory in (vault, nested, separate):
                directory.mkdir(parents=True, exist_ok=True)
            patches = (
                mock.patch.object(claim, "GIT_ROOT", git_root),
                mock.patch.object(claim, "VAULT_ROOT", vault),
            )
            with patches[0], patches[1]:
                with mock.patch.dict(
                    os.environ,
                    {"AGENT_MEMORY_TRASH_ROOT": str(nested)},
                ):
                    self.assertIsNone(claim._configured_trash_root())
                with mock.patch.dict(
                    os.environ,
                    {"AGENT_MEMORY_TRASH_ROOT": str(root)},
                ):
                    self.assertIsNone(claim._configured_trash_root())
                with mock.patch.dict(
                    os.environ,
                    {"AGENT_MEMORY_TRASH_ROOT": str(separate)},
                ):
                    self.assertEqual(claim._configured_trash_root(), separate)

    def test_windows_requires_recycle_bin_sid_or_explicit_root(self) -> None:
        self.assertTrue(
            claim._is_recognized_trash_path(
                Path(r"C:\$Recycle.Bin\S-1-5-21-1000\$RABC.md"),
                platform="win32",
            )
        )
        self.assertTrue(
            claim._is_recognized_trash_path(
                Path(r"c:\$recycle.bin\s-1-5-18\$rabc.md"),
                platform="win32",
            )
        )
        self.assertFalse(
            claim._is_recognized_trash_path(
                Path(r"C:\Users\user\.Trash\note.md"),
                platform="win32",
                home=Path(r"C:\Users\user"),
            )
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            configured = Path(raw_tmp).resolve()
            evidence = configured / "note.md"
            evidence.write_text("evidence\n", encoding="utf-8")
            self.assertTrue(
                claim._is_recognized_trash_path(
                    evidence,
                    platform="win32",
                    configured_root=configured,
                )
            )

    def test_macos_and_linux_use_their_native_trash_layouts(self) -> None:
        self.assertTrue(
            claim._is_recognized_trash_path(
                Path("/Users/alice/.Trash/note.md"),
                platform="darwin",
                home=Path("/Users/alice"),
                uid=501,
            )
        )
        self.assertTrue(
            claim._is_recognized_trash_path(
                Path("/home/alice/.local/share/Trash/files/note.md"),
                platform="linux",
                home=Path("/home/alice"),
                xdg_data_home=Path("/home/alice/.local/share"),
                uid=1000,
            )
        )
        self.assertFalse(
            claim._is_recognized_trash_path(
                Path("/home/alice/.Trash/note.md"),
                platform="linux",
                home=Path("/home/alice"),
                xdg_data_home=Path("/home/alice/.local/share"),
                uid=1000,
            )
        )


class Sha256RepositoryObservationTests(unittest.TestCase):
    def test_sha256_repository_commit_and_sentinel_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            git_root = root / "git"
            initialized = subprocess.run(
                [
                    "git",
                    "init",
                    "-q",
                    "--object-format=sha256",
                    "--initial-branch=main",
                    str(git_root),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if initialized.returncode != 0:
                commit = "a" * 64
                prior = "b" * 64
                self.assertEqual(
                    parse_deleted_observation(f"deleted:{commit}:{prior}"),
                    (commit, prior),
                    initialized.stderr,
                )
                return
            vault = git_root / "AgentMemory"
            target = vault / "项目" / "sha256.md"
            trash = root / "explicit-trash"
            trash_file = trash / "sha256.md"
            runtime = root / "runtime"
            target.parent.mkdir(parents=True)
            trash.mkdir()
            runtime.mkdir()
            target.write_bytes(b"# SHA-256 repository\n")
            git(git_root, "config", "user.name", "SHA256 Test")
            git(git_root, "config", "user.email", "test@example.invalid")
            git(git_root, "add", "AgentMemory")
            git(git_root, "commit", "-qm", "baseline")
            target.replace(trash_file)
            git(git_root, "add", "-u", "--", "AgentMemory/项目/sha256.md")
            git(git_root, "commit", "-qm", "delete sha256 memory")
            deletion_commit = git(git_root, "rev-parse", "HEAD")
            self.assertEqual(len(deletion_commit), 64)
            config_path = runtime / "agent-memory.toml"
            config_path.write_text(
                "\n".join(
                    (
                        f"memory_root = {json.dumps(str(vault))}",
                        f"git_root = {json.dumps(str(git_root))}",
                        f"config_root = {json.dumps(str(runtime))}",
                        f"state_db = {json.dumps(str(runtime / 'state.sqlite'))}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env = isolated_subprocess_env(
                {
                    "AGENT_MEMORY_CONFIG_FILE": str(config_path),
                    "AGENT_MEMORY_TRASH_ROOT": str(trash),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "memoryctl"),
                    "--actor",
                    "human",
                    "observe-deletion",
                    "--file",
                    str(target),
                    "--trash-path",
                    str(trash_file),
                    "--deletion-commit",
                    deletion_commit,
                    "--evidence-ref",
                    "sha256-repo-preview",
                    "--confirm-user-authorized",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertTrue(
                json.loads(completed.stdout)["observation"]["sentinel"].startswith(
                    f"deleted:{deletion_commit}:"
                )
            )


if __name__ == "__main__":
    unittest.main()
