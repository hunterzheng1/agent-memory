from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory_doctor as doctor
import agent_memory_index as index


def build_vault(root: Path, index_body: str) -> Path:
    vault = root / "vault"
    (vault / "项目").mkdir(parents=True)
    (vault / "决策").mkdir(parents=True)
    (vault / "用户记忆").mkdir(parents=True)
    (vault / "项目" / "alpha.md").write_text("# Alpha\n\nBody.\n", encoding="utf-8")
    (vault / "决策" / "beta.md").write_text("# Beta\n\nBody.\n", encoding="utf-8")
    (vault / "用户记忆" / "gamma.md").write_text("# Gamma\n\nBody.\n", encoding="utf-8")
    (vault / "INDEX.md").write_text(index_body, encoding="utf-8")
    return vault


def index_check(vault: Path, root: Path) -> dict:
    state_db = root / "state.sqlite"
    with sqlite3.connect(state_db) as conn:
        index.init_db(conn)
    with mock.patch.multiple(
        doctor,
        STATE_DB=state_db,
        VAULT_ROOT=vault,
        GIT_ROOT=root,
        CONFIG_ROOT=root / "runtime",
        AUDIT_LOG=root / "audit.jsonl",
        CLOSEOUT_LOG=root / "closeout.jsonl",
        HOST_CONFIG={},
        SEMANTIC_ENABLED=False,
    ):
        checks = doctor.collect_checks(allow_dirty_memory=True)
    return next(item for item in checks if item["name"] == "index_navigation_parity")


class IndexNavigationTests(unittest.TestCase):
    """2026-11-24: INDEX.md is a routing index, not a full catalogue.

    Requiring one entry per document produced a permanent warning on every real
    vault (134 docs vs ~16 routes). The contract is now: no broken link, and
    every top-level area has at least one route.
    """

    def test_routing_index_passes_without_listing_every_document(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            vault = build_vault(
                root,
                "# Index\n\n- 项目：`项目/alpha.md`\n- 决策：`决策/beta.md`\n"
                "- 用户：`用户记忆/gamma.md`\n",
            )
            check = index_check(vault, root)
            self.assertEqual(check["status"], "pass", check)
            self.assertEqual(check["detail"]["documents_total"], 4)
            # 3 routed documents + INDEX.md itself are not required to be listed.
            self.assertGreaterEqual(check["detail"]["routed_entries"], 3)

    def test_broken_link_fails_routing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            vault = build_vault(
                root,
                "# Index\n\n- 项目：`项目/alpha.md`\n- 决策：`决策/beta.md`\n"
                "- 用户：`用户记忆/gamma.md`\n- 缺失：`项目/missing.md`\n",
            )
            check = index_check(vault, root)
            self.assertEqual(check["status"], "warn", check)
            self.assertIn("项目/missing.md", check["detail"]["broken"])

    def test_unrouted_top_level_area_warns(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            vault = build_vault(
                root,
                "# Index\n\n- 项目：`项目/alpha.md`\n- 决策：`决策/beta.md`\n",
            )
            check = index_check(vault, root)
            self.assertEqual(check["status"], "warn", check)
            self.assertIn("用户记忆", check["detail"]["unrouted_areas"])

    def test_external_host_entry_files_are_not_broken_links(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            vault = build_vault(
                root,
                "# Index\n\n- 项目：`项目/alpha.md`\n- 决策：`决策/beta.md`\n"
                "- 用户：`用户记忆/gamma.md`\n- Pi：`~/.pi/agent/AGENTS.md`\n",
            )
            check = index_check(vault, root)
            self.assertEqual(check["status"], "pass", check)
            self.assertEqual(check["detail"]["broken"], [])


if __name__ == "__main__":
    unittest.main()
