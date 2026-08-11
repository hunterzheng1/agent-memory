from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.subprocess_env import isolated_subprocess_env


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts"


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


def run(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env if env is not None else isolated_subprocess_env(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


class BootstrapIntegrationTests(unittest.TestCase):
    def test_bootstrap_after_cas_replacement_restores_actual_object(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            vault = root / "vault"
            vault.mkdir()
            agents = vault / "AGENTS.md"
            agents.write_text("original vault object\n", encoding="utf-8")
            outside = root / "outside-agents.md"
            outside.write_text("external sentinel\n", encoding="utf-8")
            capability = root / "symlink-capability"
            try:
                capability.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            else:
                capability.unlink()
            probe = (
                "import pathlib,sys;"
                f"sys.path.insert(0,{str(SCRIPT_ROOT)!r});"
                "import bootstrap;"
                "vault=pathlib.Path(sys.argv[1]);outside=pathlib.Path(sys.argv[2]);"
                "fired=[False];"
                "exec(\"def race(target):\\n"
                "    if target.name == 'AGENTS.md' and not fired[0]:\\n"
                "        fired[0] = True\\n"
                "        target.unlink()\\n"
                "        target.symlink_to(outside)\");"
                "bootstrap.copy_template(vault,{},True,after_cas_before_replace=race)"
            )

            result = subprocess.run(
                [sys.executable, "-c", probe, str(vault), str(outside)],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("actual vault object was unsafe", result.stderr)
            self.assertTrue(agents.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "external sentinel\n")

    def test_bootstrap_rejects_vault_root_junction_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            outside = root / "outside-vault"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            vault = root / "vault-link"
            try:
                make_directory_reparse(vault, outside)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                bootstrap = run(
                    [
                        sys.executable,
                        str(SCRIPT_ROOT / "bootstrap.py"),
                        "--memory-root",
                        str(vault),
                        "--no-init-git",
                    ]
                )

                self.assertNotEqual(
                    bootstrap.returncode,
                    0,
                    bootstrap.stdout + bootstrap.stderr,
                )
                self.assertIn("BootstrapPathSecurityError", bootstrap.stderr)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
                self.assertEqual(
                    sorted(path.name for path in outside.iterdir()),
                    ["sentinel.txt"],
                )
            finally:
                remove_directory_reparse(vault)

    def test_bootstrap_rejects_agents_file_symlink_even_with_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            vault = root / "vault"
            vault.mkdir()
            outside = root / "outside-agents.md"
            outside.write_text("external sentinel\n", encoding="utf-8")
            try:
                (vault / "AGENTS.md").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            bootstrap = run(
                [
                    sys.executable,
                    str(SCRIPT_ROOT / "bootstrap.py"),
                    "--memory-root",
                    str(vault),
                    "--overwrite",
                    "--no-init-git",
                ]
            )

            self.assertNotEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("BootstrapPathSecurityError", bootstrap.stderr)
            self.assertEqual(outside.read_text(encoding="utf-8"), "external sentinel\n")

    def test_bootstrap_rejects_broken_template_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            vault = root / "vault"
            vault.mkdir()
            outside = root / "must-not-be-created.md"
            try:
                (vault / "AGENTS.md").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            bootstrap = run(
                [
                    sys.executable,
                    str(SCRIPT_ROOT / "bootstrap.py"),
                    "--memory-root",
                    str(vault),
                    "--no-init-git",
                ]
            )

            self.assertNotEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("BootstrapPathSecurityError", bootstrap.stderr)
            self.assertFalse(outside.exists())

    def test_bootstrap_rejects_template_parent_junction_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            vault = root / "vault"
            vault.mkdir()
            outside = root / "outside-agent"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            link = vault / "agent"
            try:
                make_directory_reparse(link, outside)
            except OSError as exc:
                self.skipTest(f"directory reparse points unavailable: {exc}")
            try:
                bootstrap = run(
                    [
                        sys.executable,
                        str(SCRIPT_ROOT / "bootstrap.py"),
                        "--memory-root",
                        str(vault),
                        "--overwrite",
                        "--no-init-git",
                    ]
                )

                self.assertNotEqual(
                    bootstrap.returncode,
                    0,
                    bootstrap.stdout + bootstrap.stderr,
                )
                self.assertIn("BootstrapPathSecurityError", bootstrap.stderr)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
                self.assertEqual(
                    sorted(path.name for path in outside.iterdir()),
                    ["sentinel.txt"],
                )
            finally:
                remove_directory_reparse(link)

    def test_new_namespace_bootstraps_indexes_and_checks(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            vault = root / "Agent记忆"
            config = root / "config"
            state_db = config / "state.sqlite"

            bootstrap = run(
                [
                    sys.executable,
                    str(SCRIPT_ROOT / "bootstrap.py"),
                    "--memory-root",
                    str(vault),
                ]
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            self.assertIn("git_baseline=created", bootstrap.stdout)
            self.assertTrue((vault / ".git" / "HEAD").is_file())
            head = run(["git", "-C", str(vault), "rev-parse", "--verify", "HEAD"])
            self.assertEqual(head.returncode, 0, head.stdout + head.stderr)
            (vault / ".obsidian").mkdir()
            (vault / ".obsidian" / "workspace.json").write_text("{}\n", encoding="utf-8")
            status = run(["git", "-C", str(vault), "status", "--porcelain"])
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertEqual(status.stdout, "")

            env = isolated_subprocess_env(
                {
                    "AGENT_MEMORY_ROOT": str(vault),
                    "AGENT_MEMORY_GIT_ROOT": str(vault),
                    "AGENT_MEMORY_CONFIG_ROOT": str(config),
                    "AGENT_MEMORY_STATE_DB": str(state_db),
                }
            )

            evolution = run(
                [sys.executable, str(SCRIPT_ROOT / "agent_memory_evolution.py"), "--init", "--scan", "--report"],
                env,
            )
            self.assertEqual(evolution.returncode, 0, evolution.stderr)

            index = run(
                [sys.executable, str(SCRIPT_ROOT / "agent_memory_index.py"), "--init", "--scan", "--report"],
                env,
            )
            self.assertEqual(index.returncode, 0, index.stderr)

            check = run(
                [sys.executable, str(SCRIPT_ROOT / "agent_memory_check.py")], env
            )
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertIn("agent_memory_check=ok", check.stdout)

    def test_no_init_git_leaves_new_vault_unmanaged(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            vault = Path(raw_root).resolve() / "vault"
            bootstrap = run(
                [
                    sys.executable,
                    str(SCRIPT_ROOT / "bootstrap.py"),
                    "--memory-root",
                    str(vault),
                    "--no-init-git",
                ]
            )

            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("git_baseline=skipped disabled", bootstrap.stdout)
            self.assertFalse((vault / ".git").exists())

    def test_existing_vault_is_not_silently_initialized_or_committed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            vault = Path(raw_root).resolve() / "existing vault"
            vault.mkdir()
            user_file = vault / "private-user-note.md"
            user_file.write_text("keep me\n", encoding="utf-8")

            bootstrap = run(
                [sys.executable, str(SCRIPT_ROOT / "bootstrap.py"), "--memory-root", str(vault)]
            )

            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("git_baseline=skipped preexisting_vault", bootstrap.stdout)
            self.assertFalse((vault / ".git").exists())
            self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me\n")

    def test_existing_headless_repo_is_left_unstaged(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            vault = Path(raw_root).resolve() / "headless"
            vault.mkdir()
            initialized = run(["git", "-C", str(vault), "init", "-q"])
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            user_file = vault / "before-bootstrap.md"
            user_file.write_text("untracked\n", encoding="utf-8")

            bootstrap = run(
                [sys.executable, str(SCRIPT_ROOT / "bootstrap.py"), "--memory-root", str(vault)]
            )

            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("git_baseline=skipped existing_repository_without_head", bootstrap.stdout)
            head = run(["git", "-C", str(vault), "rev-parse", "--verify", "HEAD"])
            self.assertNotEqual(head.returncode, 0)
            staged = run(["git", "-C", str(vault), "diff", "--cached", "--name-only"])
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self.assertEqual(staged.stdout, "")

    def test_parent_repository_is_not_staged_or_shadowed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            parent = Path(raw_root).resolve() / "parent"
            parent.mkdir()
            initialized = run(["git", "-C", str(parent), "init", "-q"])
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            unrelated = parent / "unrelated.txt"
            unrelated.write_text("outside vault\n", encoding="utf-8")
            vault = parent / "nested" / "vault"

            bootstrap = run(
                [sys.executable, str(SCRIPT_ROOT / "bootstrap.py"), "--memory-root", str(vault)]
            )

            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertIn("git_baseline=skipped external_git_root", bootstrap.stdout)
            self.assertFalse((vault / ".git").exists())
            staged = run(["git", "-C", str(parent), "diff", "--cached", "--name-only"])
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self.assertEqual(staged.stdout, "")

    def test_existing_repository_with_head_is_not_recommitted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            vault = Path(raw_root).resolve() / "vault"
            first = run(
                [sys.executable, str(SCRIPT_ROOT / "bootstrap.py"), "--memory-root", str(vault)]
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_head = run(["git", "-C", str(vault), "rev-parse", "HEAD"])
            self.assertEqual(first_head.returncode, 0, first_head.stdout + first_head.stderr)

            second = run(
                [sys.executable, str(SCRIPT_ROOT / "bootstrap.py"), "--memory-root", str(vault)]
            )

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("git_baseline=existing", second.stdout)
            second_head = run(["git", "-C", str(vault), "rev-parse", "HEAD"])
            self.assertEqual(second_head.stdout, first_head.stdout)


if __name__ == "__main__":
    unittest.main()
