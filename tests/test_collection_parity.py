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

import agent_memory_index as index
import agent_memory_zvec_index as zvec_index


class FakeStore:
    """Stand-in for ZvecStore with a controllable on-disk truth."""

    def __init__(self, state: dict):
        self._state = state

    def inspect(self) -> dict:
        return dict(self._state)


def ledger_db(path: Path, indexed_paths: list[str], vault: Path) -> None:
    """Seed memory_docs (so paths are eligible) plus the vector ledger."""

    with sqlite3.connect(path) as conn:
        index.init_db(conn)
        zvec_index.init_db(conn)
        for raw in indexed_paths:
            rel = Path(raw).relative_to(vault).as_posix()
            conn.execute(
                "INSERT INTO memory_docs"
                "(path, rel_path, sha256, title, memory_type, track, status, sensitivity, "
                " verified_at_source, mtime, size_bytes, line_count, indexed_at) "
                "VALUES (?, ?, 'x', 'A', 'project', 'project', 'active', 'normal', "
                " 'frontmatter', 0, 20, 3, '2026-11-24T00:00:00+00:00')",
                (raw, rel),
            )
            conn.execute(
                "INSERT INTO memory_vector_index_state"
                "(path, rel_path, doc_sha256, status, last_error, updated_at) "
                "VALUES (?, ?, 'x', 'indexed', '', '2026-11-24T00:00:00+00:00')",
                (raw, rel),
            )
        conn.commit()


class CollectionParityTests(unittest.TestCase):
    """2026-11-24: --report / doctor must read the real collection.

    ``parity_report`` used to read only the SQLite ledger, so a collection that
    had been recreated or emptied while the ledger still said "indexed" reported
    a healthy 111/112. The vector store is now inspected too.
    """

    def _vault(self, root: Path) -> Path:
        vault = root / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("# A\n\nbody\n", encoding="utf-8")
        return vault

    def test_missing_collection_fails_parity(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            vault = self._vault(root)
            ledger_db(db, [str(vault / "a.md")], vault)
            store = FakeStore({"exists": False, "doc_count": None, "embedding_dim": None, "error": ""})
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                report = zvec_index.parity_report(conn, vault, store, 768)
            finally:
                conn.close()
            self.assertFalse(report["parity_ok"], report)
            self.assertIn("collection_missing_on_disk", report["collection_problems"])

    def test_doc_count_mismatch_fails_parity(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            vault = self._vault(root)
            ledger_db(db, [str(vault / "a.md")], vault)
            # Ledger says 1 indexed, the collection holds 0 vectors.
            store = FakeStore({"exists": True, "doc_count": 0, "embedding_dim": 768, "error": ""})
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                report = zvec_index.parity_report(conn, vault, store, 768)
            finally:
                conn.close()
            self.assertFalse(report["parity_ok"], report)
            joined = " ".join(report["collection_problems"])
            self.assertIn("collection_doc_count_mismatch", joined)
            self.assertIn("store=0", joined)

    def test_dimension_mismatch_fails_parity(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            vault = self._vault(root)
            ledger_db(db, [str(vault / "a.md")], vault)
            store = FakeStore({"exists": True, "doc_count": 1, "embedding_dim": 384, "error": ""})
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                report = zvec_index.parity_report(conn, vault, store, 768)
            finally:
                conn.close()
            self.assertFalse(report["parity_ok"], report)
            self.assertIn("collection_dim_mismatch", " ".join(report["collection_problems"]))

    def test_consistent_store_passes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            vault = self._vault(root)
            ledger_db(db, [str(vault / "a.md")], vault)
            store = FakeStore({"exists": True, "doc_count": 1, "embedding_dim": 768, "error": ""})
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                report = zvec_index.parity_report(conn, vault, store, 768)
            finally:
                conn.close()
            self.assertTrue(report["parity_ok"], report)
            self.assertEqual(report["collection_problems"], [])

    def test_report_without_store_keeps_ledger_only_behaviour(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw).resolve()
            db = root / "state.sqlite"
            vault = self._vault(root)
            ledger_db(db, [str(vault / "a.md")], vault)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                report = zvec_index.parity_report(conn, vault)
            finally:
                conn.close()
            # Backwards compatible: no store supplied, no collection claims made.
            self.assertEqual(report["collection"], {})
            self.assertEqual(report["collection_problems"], [])

    def test_doctor_helper_reports_store_problems(self) -> None:
        import agent_memory_doctor as doctor

        store = FakeStore({"exists": True, "doc_count": 0, "embedding_dim": 384, "error": ""})
        with (
            mock.patch.object(zvec_index, "ZvecStore", return_value=store),
            mock.patch.object(doctor, "EMBEDDING_DIM", "768"),
        ):
            state, problems = doctor.inspect_vector_store(5)
        self.assertEqual(state["doc_count"], 0)
        joined = " ".join(problems)
        self.assertIn("collection_doc_count_mismatch", joined)
        self.assertIn("collection_dim_mismatch", joined)


if __name__ == "__main__":
    unittest.main()
