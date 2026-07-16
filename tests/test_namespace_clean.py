from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts"

# Fork policy: keep thin codex_* / agent_evolution delegates for compatibility.
ALLOWED_COMPAT_ENTRYPOINTS = {
    "agent_evolution.py",
    "codex_agent_evolution.py",
    "codex_memory_audit.py",
    "codex_memory_audit_autorun.py",
    "codex_memory_check.py",
    "codex_memory_closeout.py",
    "codex_memory_doctor.py",
    "codex_memory_index.py",
    "codex_memory_retrieval_benchmark.py",
    "codex_memory_search.py",
    "codex_memory_stop_hook.py",
    "codex_memory_zvec_index.py",
}


class NamespaceCleanTests(unittest.TestCase):
    def test_runtime_has_no_unexpected_compatibility_entrypoints(self) -> None:
        forbidden_prefix = "codex" + "_"
        legacy_dispatcher = "legacy" + "_entrypoint.py"
        forbidden_files = [
            path.name
            for path in SCRIPT_ROOT.iterdir()
            if path.is_file()
            and path.name not in ALLOWED_COMPAT_ENTRYPOINTS
            and (path.name.startswith(forbidden_prefix) or path.name == legacy_dispatcher)
        ]
        self.assertEqual(forbidden_files, [])

    def test_allowed_compat_wrappers_are_thin_delegates(self) -> None:
        for name in sorted(ALLOWED_COMPAT_ENTRYPOINTS):
            path = SCRIPT_ROOT / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding="utf-8")
            self.assertIn("Compatibility wrapper", text, name)
            self.assertIn("runpy.run_path", text, name)
            self.assertIn("agent_memory_", text, name)

    def test_canonical_scripts_use_only_agent_memory_environment_namespace(self) -> None:
        forbidden_namespace = "CODEX" + "_MEMORY_"
        offenders = []
        for path in sorted(SCRIPT_ROOT.glob("agent_memory_*.py")):
            if forbidden_namespace in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn(forbidden_namespace, env_example)


if __name__ == "__main__":
    unittest.main()
