from __future__ import annotations

import json
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
import agent_memory_redact_search_log as redact


def make_db(path: Path, rows: list[tuple[str, str]]) -> None:
    """Create a state DB holding legacy (raw) search-log rows."""

    with sqlite3.connect(path) as conn:
        index.init_db(conn)
        conn.executemany(
            """
            INSERT INTO memory_search_log(
              query, result_count, used_paths, query_sha256, query_length,
              sources, duration_ms, created_at
            ) VALUES (?, ?, ?, '', NULL, 'sqlite', 12, '2026-07-11T14:10:21+00:00')
            """,
            [(query, 3, paths) for query, paths in rows],
        )
        conn.commit()


class SearchLogRedactionCommandTests(unittest.TestCase):
    """2026-11-24: one-off redaction of legacy raw query/path rows.

    memory_search_log is derived telemetry, not a fact source, so redacting it
    loses no memory content; it only removes query/path text that should not
    persist. The live writer already redacts new rows.
    """

    def _count_legacy(self, db: Path) -> int:
        conn = sqlite3.connect(db)
        try:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM memory_search_log WHERE {redact.LEGACY_PREDICATE}"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def test_plan_masks_query_and_paths_but_keeps_statistics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            db = Path(raw).resolve() / "state.sqlite"
            make_db(db, [("secret question", "项目/a.md,项目/b.md")])
            conn = sqlite3.connect(db)
            try:
                plan = redact.plan_redaction(conn, 10)
            finally:
                conn.close()
            self.assertEqual(len(plan), 1)
            item = plan[0]
            self.assertTrue(item["query"].startswith("[redacted:"), item)
            self.assertTrue(item["used_paths"].startswith("[redacted:"), item)
            dumped = json.dumps(item, ensure_ascii=False)
            self.assertNotIn("secret question", dumped)
            self.assertNotIn("项目/a.md", dumped)
            # Statistical columns stay analysable.
            self.assertEqual(item["query_length"], len("secret question"))
            self.assertEqual(item["used_path_count"], 2)
            self.assertEqual(len(item["query_sha256"]), 64)
            self.assertEqual(len(item["used_paths_sha256"]), 64)

    def test_apply_redacts_everything_and_doctor_then_passes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            backup = root / "backup"
            make_db(
                db,
                [
                    ("secret question", "项目/a.md,项目/b.md"),
                    ("another secret", "工作流/c.md"),
                ],
            )
            self.assertEqual(self._count_legacy(db), 2)

            with mock.patch.object(redact, "state_db_path", return_value=db):
                code = redact.main(
                    ["--apply", "--verify", "--json", "--backup-dir", str(backup)]
                )
            self.assertEqual(code, 0)
            self.assertEqual(self._count_legacy(db), 0)

            conn = sqlite3.connect(db)
            try:
                total = int(
                    conn.execute("SELECT COUNT(*) FROM memory_search_log").fetchone()[0]
                )
                durations = [
                    row[0]
                    for row in conn.execute(
                        "SELECT duration_ms FROM memory_search_log ORDER BY id"
                    )
                ]
                self.assertEqual(total, 2)
                self.assertEqual(durations, [12, 12])
                self.assertTrue(
                    any(p.name.startswith("state.sqlite") for p in backup.iterdir())
                )
                health = doctor.search_log_privacy_health(conn)
            finally:
                conn.close()
            self.assertTrue(health[0], health)
            self.assertEqual(health[1]["legacy_raw_rows"], 0)

    def test_apply_refuses_without_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            db = Path(raw).resolve() / "state.sqlite"
            make_db(db, [("secret question", "项目/a.md")])
            with mock.patch.object(redact, "state_db_path", return_value=db):
                code = redact.main(["--apply", "--json"])
            self.assertEqual(code, 2)
            # Irreversible redaction must not run without a copy.
            self.assertEqual(self._count_legacy(db), 1)

    def test_second_run_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            make_db(db, [("secret question", "项目/a.md")])
            with mock.patch.object(redact, "state_db_path", return_value=db):
                redact.main(["--apply", "--json", "--backup-dir", str(root / "b1")])
                conn = sqlite3.connect(db)
                try:
                    plan_again = redact.plan_redaction(conn, 10)
                finally:
                    conn.close()
            self.assertEqual(plan_again, [])


if __name__ == "__main__":
    unittest.main()
