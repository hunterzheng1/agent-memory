from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import agent_memory_retrieval_benchmark as benchmark


class RetrievalBenchmarkLockTests(unittest.TestCase):
    def test_cli_accepts_vector_lock_timeout(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["agent_memory_retrieval_benchmark.py", "--no-vector", "--lock-timeout", "9.5"],
        ):
            args = benchmark.parse_args()

        self.assertEqual(args.lock_timeout, 9.5)

    def test_vector_phase_holds_the_exclusive_zvec_lock(self) -> None:
        args = argparse.Namespace(
            limit=5,
            json=True,
            no_vector=False,
            case_id=["field_rules"],
            benchmark_file="",
            lock_timeout=9.0,
        )
        lock_calls: list[tuple[bool, float]] = []

        @contextlib.contextmanager
        def recorded_lock(*, exclusive: bool, timeout: float):
            lock_calls.append((exclusive, timeout))
            yield

        connection = mock.Mock()
        store = mock.Mock()
        embedder = mock.Mock()
        zvec_module = mock.Mock(
            DEFAULT_COLLECTION_PATH=Path("/fake/zvec"),
            DEFAULT_EMBEDDING_DIM=768,
            DEFAULT_MODEL="fake-model",
            DEFAULT_DEVICE="cpu",
            zvec_lock=recorded_lock,
            connect=mock.Mock(return_value=connection),
            ZvecStore=mock.Mock(return_value=store),
            EmbeddingGemmaEmbedder=mock.Mock(return_value=embedder),
        )
        stdout = io.StringIO()
        expected_path = "工作流/Agent记忆字段规范.md"

        with mock.patch.object(
            benchmark,
            "parse_args",
            return_value=args,
        ), mock.patch.object(
            benchmark,
            "load_module",
            side_effect=[object(), zvec_module],
        ), mock.patch.object(
            benchmark,
            "run_sqlite",
            return_value=[expected_path],
        ), mock.patch.object(
            benchmark,
            "run_vector",
            return_value=[expected_path],
        ), redirect_stdout(stdout):
            self.assertEqual(benchmark.main(), 0)

        self.assertEqual(lock_calls, [(True, 9.0)])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
