from __future__ import annotations

import hashlib
import json
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

import agent_memory_search as search


class SearchLogRedactionTest(unittest.TestCase):
    def test_legacy_query_text_is_replaced_with_hash_metadata(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_tmp:
            tmp = Path(raw_tmp)
            state_db = tmp / "state.sqlite"
            config = tmp / "agent-memory.toml"
            config.write_text(
                f'memory_root = "{(REPO_ROOT / "templates" / "vault").as_posix()}"\n'
                f'state_db = "{state_db.as_posix()}"\n',
                encoding="utf-8",
            )
            env = isolated_subprocess_env(
                {
                    "AGENT_MEMORY_CONFIG_FILE": str(config),
                    "AGENT_MEMORY_ROOT": str(REPO_ROOT / "templates" / "vault"),
                    "AGENT_MEMORY_STATE_DB": str(state_db),
                }
            )
            initialized = subprocess.run(
                [sys.executable, str(SCRIPTS / "agent_memory_index.py"), "--init"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            with sqlite3.connect(state_db) as conn:
                conn.execute(
                    "INSERT INTO memory_search_log(query,result_count,used_paths,created_at) VALUES (?,?,?,?)",
                    (
                        "private legacy query",
                        1,
                        "projects/customer-acme/private-plan.md",
                        "2026-07-11T00:00:00+00:00",
                    ),
                )
            redacted = subprocess.run(
                [sys.executable, str(SCRIPTS / "agent_memory_search.py"), "--redact-legacy-logs", "--json"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(redacted.returncode, 0, redacted.stderr)
            self.assertEqual(json.loads(redacted.stdout), {"redacted": 1, "remaining_raw": 0})
            with sqlite3.connect(state_db) as conn:
                query, digest, length, used_paths, paths_digest, path_count = conn.execute(
                    "SELECT query, query_sha256, query_length, used_paths, "
                    "used_paths_sha256, used_path_count FROM memory_search_log"
                ).fetchone()
            self.assertTrue(query.startswith("[redacted:"))
            self.assertEqual(len(digest), 64)
            self.assertEqual(length, len("private legacy query"))
            self.assertNotIn("private legacy query", query)
            self.assertTrue(used_paths.startswith("[redacted:"))
            self.assertEqual(len(paths_digest), 64)
            self.assertEqual(path_count, 1)
            self.assertNotIn("customer-acme", used_paths)
            self.assertNotIn(b"customer-acme", state_db.read_bytes())

    def test_new_search_log_persists_only_path_digest_and_count(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_tmp:
            state_db = Path(raw_tmp) / "state.sqlite"
            private_path = "projects/customer-acme/private-plan.md"
            row = search.SearchResult(
                path=private_path,
                rel_path=private_path,
                sources={"sqlite"},
            )
            with mock.patch.object(search, "STATE_DB", state_db):
                search.log_search("project status", [row], 7)

            with sqlite3.connect(state_db) as conn:
                stored = conn.execute(
                    "SELECT used_paths, used_paths_sha256, used_path_count FROM memory_search_log"
                ).fetchone()
            self.assertIsNotNone(stored)
            used_paths, paths_digest, path_count = stored
            self.assertTrue(used_paths.startswith("[redacted:"))
            self.assertEqual(paths_digest, hashlib.sha256(private_path.encode("utf-8")).hexdigest())
            self.assertEqual(path_count, 1)
            self.assertNotIn("customer-acme", " ".join(str(value) for value in stored))
            self.assertNotIn(b"customer-acme", state_db.read_bytes())


if __name__ == "__main__":
    unittest.main()
