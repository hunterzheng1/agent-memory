from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DocumentationContractTest(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return REPO_ROOT.joinpath(relative).read_text(encoding="utf-8")

    def test_public_docs_and_templates_have_no_conflict_markers(self) -> None:
        roots = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "config",
            REPO_ROOT / "docs",
            REPO_ROOT / "templates",
        ]
        offenders: list[str] = []
        marker = re.compile(r"^(?:<{7}|={7}|>{7})", re.MULTILINE)
        for root in roots:
            paths = [root] if root.is_file() else root.rglob("*")
            for path in paths:
                if path.is_file() and path.suffix.lower() in {".md", ".toml", ".json"}:
                    if marker.search(path.read_text(encoding="utf-8")):
                        offenders.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_local_document_links_resolve(self) -> None:
        missing: list[str] = []
        for path in (REPO_ROOT / "README.md", *(REPO_ROOT / "docs").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                target = target.strip().split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not path.parent.joinpath(target).resolve().exists():
                    missing.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {target}")
        self.assertEqual(missing, [])

    def test_readme_exposes_current_hosts_commands_and_windows_guide(self) -> None:
        readme = self._read("README.md")
        for value in (
            "codebuddy",
            "cursor",
            "decision-outcomes",
            "policy-benchmark",
            "observe-deletion",
            "observe-committed",
            "docs/windows.md",
        ):
            self.assertIn(value, readme)
        self.assertIn("enabled = false", readme)
        self.assertIn('enforcement = "off"', readme)
        self.assertIn("TRUSTED_APPROVAL_VERIFIER_REQUIRED", readme)
        for excluded in (
            "yichen-content-studio",
            "agent_memory_write.py",
            "agent_memory_retrieve.py",
        ):
            self.assertNotIn(excluded, readme)

    def test_windows_docs_state_verified_and_unverified_boundaries(self) -> None:
        guide = self._read("docs/windows.md")
        audit = self._read("docs/windows-compatibility-audit.md")
        for value in (
            "install-windows.ps1",
            "Windows PowerShell 5.1",
            "-OverwriteConfig",
            "windows_acl_unverified",
            "process_crash_recovery",
            "power_loss_durability",
        ):
            self.assertIn(value, guide)
        self.assertIn("Windows ACL", audit)
        self.assertIn("fail closed", audit)

    def test_vault_rules_use_one_session_scoped_protocol(self) -> None:
        agent_rules = self._read("templates/vault/AGENTS.md")
        claude_rules = self._read("templates/vault/CLAUDE.md")
        codebuddy_rules = self._read("templates/vault/CODEBUDDY.md")
        for rules in (agent_rules, claude_rules, codebuddy_rules):
            self.assertIn("memoryctl", rules)
            self.assertIn("prewrite", rules)
            self.assertIn("claim --file", rules)
            self.assertIn("closeout", rules)
            self.assertIn("source_class", rules)
            self.assertIn("knowledge_kind", rules)
            self.assertIn("agent_scope", rules)
        self.assertIn("--actor cursor", agent_rules)
        self.assertIn("CLAUDE_ENV_FILE", claude_rules)
        self.assertIn("CODEBUDDY_SESSION_ID", codebuddy_rules)

    def test_example_write_intents_remain_staged_off_by_default(self) -> None:
        config = self._read("config/agent-memory.example.toml")
        self.assertRegex(config, r"(?m)^enabled = false$")
        self.assertRegex(config, r'(?m)^enforcement = "off"')
        self.assertIn("independent trusted approval verifier", config)

    def test_vault_has_one_canonical_local_scripts_note(self) -> None:
        workflow = REPO_ROOT / "templates" / "vault" / "工作流"
        self.assertTrue(workflow.joinpath("Agent记忆本地脚本.md").is_file())
        self.assertFalse(workflow.joinpath("记忆本地脚本.md").exists())


if __name__ == "__main__":
    unittest.main()
