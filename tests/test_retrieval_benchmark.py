from __future__ import annotations

import argparse
import contextlib
import json
import io
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.subprocess_env import isolated_subprocess_env


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory_retrieval_benchmark as benchmark


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "agent_memory_retrieval_benchmark.py"


def run_benchmark(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args, "--json"],
        cwd=REPO_ROOT,
        env=isolated_subprocess_env(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


class RetrievalBenchmarkDatasetTests(unittest.TestCase):
    def test_public_sample_is_explicit_and_safe(self) -> None:
        metadata, cases = benchmark.load_dataset("")
        self.assertEqual(metadata["privacy"], "public_sample")
        self.assertGreaterEqual(len(cases), 5)
        self.assertTrue(all("fake" in case["tags"] for case in cases))
        self.assertTrue(all(int(case["required_at"]) > 0 for case in cases))

    def test_legacy_private_array_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "private.json"
            path.write_text(
                json.dumps([{"id": "one", "query": "private words", "expected": ["项目/private.md"]}]),
                encoding="utf-8",
            )
            metadata, cases = benchmark.load_dataset(str(path))
        self.assertEqual(metadata["privacy"], "private_local")
        self.assertEqual(cases[0]["required_at"], 0)

    def test_unsafe_expected_path_and_duplicate_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "bad.json"
            path.write_text(
                json.dumps({
                    "privacy": "private_local",
                    "cases": [
                        {"id": "same", "query": "a", "expected": ["../secret.md"]},
                        {"id": "same", "query": "b", "expected": ["项目/b.md"]},
                    ],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(benchmark.DatasetError):
                benchmark.load_dataset(str(path))

    def test_explicit_file_cannot_self_declare_public(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "claims-public.json"
            path.write_text(
                json.dumps({
                    "privacy": "public_sample",
                    "cases": [{"id": "one", "query": "private words", "expected": ["项目/private.md"]}],
                }),
                encoding="utf-8",
            )
            metadata, _ = benchmark.load_dataset(str(path))
        self.assertEqual(metadata["declared_privacy"], "public_sample")
        self.assertEqual(metadata["privacy"], "private_local")

    def test_private_stdout_redacts_query_expected_and_dataset_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            private_case_id = "PRIVATE_CASE_ID_MUST_NOT_APPEAR"
            private_query = "private query alpha bravo"
            expected_path = "项目/private-memory.md"
            path = Path(raw_tmp) / "private-fixture.json"
            path.write_text(
                json.dumps({
                    "privacy": "public_sample",
                    "name": "must-not-be-trusted",
                    "cases": [{"id": private_case_id, "query": private_query, "expected": [expected_path]}],
                }),
                encoding="utf-8",
            )
            parsed = Namespace(
                limit=5,
                json=True,
                no_vector=True,
                case_id=[],
                benchmark_file=str(path),
                show_private_details=False,
            )
            stream = io.StringIO()
            with mock.patch.object(benchmark, "parse_args", return_value=parsed), \
                 mock.patch.object(benchmark, "load_module", return_value=object()), \
                 mock.patch.object(benchmark, "run_sqlite", return_value=[expected_path]), \
                 redirect_stdout(stream):
                self.assertEqual(benchmark.main(), 0)
            stdout = stream.getvalue()
        self.assertNotIn(private_query, stdout)
        self.assertNotIn(private_case_id, stdout)
        self.assertNotIn(expected_path, stdout)
        self.assertNotIn(str(path), stdout)
        payload = json.loads(stdout)
        self.assertTrue(payload["private_details_redacted"])
        self.assertNotIn("path", payload["dataset"])
        self.assertEqual(
            set(payload["records"][0]),
            {"case_ref", "query_sha256", "query_length", "required_at", "sqlite_rank", "vector_rank"},
        )
        self.assertRegex(payload["records"][0]["case_ref"], r"^case-001-[0-9a-f]{12}$")

    def test_private_human_output_redacts_vector_exception(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            private_case_id = "PRIVATE_VECTOR_CASE_ID_MUST_NOT_APPEAR"
            private_query = "private query charlie delta"
            expected_path = "项目/private-vector.md"
            path = Path(raw_tmp) / "private-vector-fixture.json"
            path.write_text(
                json.dumps([{"id": private_case_id, "query": private_query, "expected": [expected_path]}]),
                encoding="utf-8",
            )
            parsed = Namespace(
                limit=5,
                json=False,
                no_vector=False,
                case_id=[],
                benchmark_file=str(path),
                show_private_details=False,
            )
            stream = io.StringIO()
            error = RuntimeError(f"failed for {private_query} at {path}")
            with mock.patch.object(benchmark, "parse_args", return_value=parsed), \
                 mock.patch.object(benchmark, "load_module", side_effect=[object(), error]), \
                 mock.patch.object(benchmark, "run_sqlite", return_value=[expected_path]), \
                 redirect_stdout(stream):
                self.assertEqual(benchmark.main(), 2)
            stdout = stream.getvalue()
        self.assertNotIn(private_query, stdout)
        self.assertNotIn(private_case_id, stdout)
        self.assertNotIn(expected_path, stdout)
        self.assertNotIn(str(path), stdout)
        self.assertIn("vector error=[redacted:", stdout)

    def test_private_gate_failure_uses_case_ref_not_raw_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            private_case_id = "PRIVATE_GATE_CASE_ID_MUST_NOT_APPEAR"
            path = Path(raw_tmp) / "fixture.json"
            path.write_text(
                json.dumps(
                    {
                        "privacy": "private_local",
                        "cases": [
                            {
                                "id": private_case_id,
                                "query": "private gate query",
                                "expected": ["项目/private-gate.md"],
                                "required_at": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            parsed = Namespace(
                limit=5,
                json=True,
                no_vector=True,
                case_id=[],
                benchmark_file=str(path),
                show_private_details=False,
            )
            stream = io.StringIO()
            with mock.patch.object(benchmark, "parse_args", return_value=parsed), \
                 mock.patch.object(benchmark, "load_module", return_value=object()), \
                 mock.patch.object(benchmark, "run_sqlite", return_value=[]), \
                 redirect_stdout(stream):
                self.assertEqual(benchmark.main(), 3)
            stdout = stream.getvalue()
        self.assertNotIn(private_case_id, stdout)
        failure = json.loads(stdout)["gate_failures"][0]
        self.assertEqual(set(failure), {"case_ref", "backend", "required_at", "rank"})
        self.assertRegex(failure["case_ref"], r"^case-001-[0-9a-f]{12}$")

    def test_public_output_keeps_diagnostic_case_details(self) -> None:
        parsed = Namespace(
            limit=5,
            json=True,
            no_vector=True,
            case_id=["sample-field-rules"],
            benchmark_file="",
            show_private_details=False,
        )
        stream = io.StringIO()
        with mock.patch.object(benchmark, "parse_args", return_value=parsed), \
             mock.patch.object(benchmark, "load_module", return_value=object()), \
             mock.patch.object(
                 benchmark,
                 "run_sqlite",
                 return_value=["工作流/Agent记忆字段规范.md"],
             ), \
             redirect_stdout(stream):
            self.assertEqual(benchmark.main(), 0)
        stdout = stream.getvalue()
        self.assertIn("sample-field-rules", stdout)
        self.assertIn("Agent记忆字段规范", stdout)
        self.assertIn("工作流/Agent记忆字段规范.md", stdout)

    def test_explicit_input_errors_are_content_free_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="PRIVATE_RETRIEVAL_DIR_") as raw_tmp:
            root = Path(raw_tmp)
            invalid_json = root / "PRIVATE_INVALID_JSON_FILENAME.json"
            invalid_json.write_text("PRIVATE_INVALID_JSON_CONTENT{", encoding="utf-8")
            invalid_fixture = root / "PRIVATE_INVALID_FIXTURE_FILENAME.json"
            invalid_fixture.write_text(
                json.dumps(
                    {
                        "privacy": "private_local",
                        "cases": [
                            {
                                "id": "PRIVATE_INVALID_FIXTURE_CASE_ID",
                                "query": "PRIVATE_INVALID_FIXTURE_QUERY",
                                "expected": ["../PRIVATE_INVALID_EXPECTED.md"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            missing = root / "PRIVATE_MISSING_FIXTURE_FILENAME.json"
            scenarios = [
                (missing, "dataset_unreadable"),
                (invalid_json, "dataset_invalid_json"),
                (invalid_fixture, "case_expected_must_be_safe_relative_markdown_path"),
            ]
            for path, expected_error in scenarios:
                with self.subTest(expected_error=expected_error):
                    completed = run_benchmark(
                        "--benchmark-file", str(path), "--no-vector"
                    )
                    self.assertEqual(completed.returncode, 2)
                    combined = completed.stdout + completed.stderr
                    self.assertEqual(json.loads(completed.stdout)["error"], expected_error)
                    self.assertNotIn(str(root), combined)
                    self.assertNotIn(path.name, combined)
                    self.assertNotIn("PRIVATE_INVALID_JSON_CONTENT", combined)
                    self.assertNotIn("PRIVATE_INVALID_FIXTURE_CASE_ID", combined)
                    self.assertNotIn("PRIVATE_INVALID_FIXTURE_QUERY", combined)
                    self.assertNotIn("PRIVATE_INVALID_EXPECTED", combined)
                    self.assertNotIn("Traceback", combined)


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
            case_id=["sample-field-rules"],
            benchmark_file="",
            show_private_details=False,
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

    def test_vector_failures_close_connection_before_releasing_lock(self) -> None:
        for failure_stage in ("init", "preflight", "query"):
            with self.subTest(failure_stage=failure_stage):
                args = argparse.Namespace(
                    limit=5,
                    json=True,
                    no_vector=False,
                    case_id=["sample-field-rules"],
                    benchmark_file="",
                    show_private_details=False,
                    lock_timeout=4.0,
                )
                lock_events: list[str] = []

                @contextlib.contextmanager
                def recorded_lock(*, exclusive: bool, timeout: float):
                    self.assertTrue(exclusive)
                    self.assertEqual(timeout, 4.0)
                    lock_events.append("enter")
                    try:
                        yield
                    finally:
                        lock_events.append("exit")

                connection = mock.Mock()
                embedder = mock.Mock()
                if failure_stage == "preflight":
                    embedder.embed_query.side_effect = RuntimeError("preflight failed")
                zvec_module = mock.Mock(
                    DEFAULT_COLLECTION_PATH=Path("/fake/zvec"),
                    DEFAULT_EMBEDDING_DIM=768,
                    DEFAULT_MODEL="fake-model",
                    DEFAULT_DEVICE="cpu",
                    zvec_lock=recorded_lock,
                    connect=mock.Mock(return_value=connection),
                    ZvecStore=mock.Mock(return_value=mock.Mock()),
                    EmbeddingGemmaEmbedder=mock.Mock(return_value=embedder),
                )
                if failure_stage == "init":
                    zvec_module.init_db.side_effect = RuntimeError("init failed")
                vector_effect = (
                    RuntimeError("query failed")
                    if failure_stage == "query"
                    else ["工作流/Agent记忆字段规范.md"]
                )

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
                    return_value=["工作流/Agent记忆字段规范.md"],
                ), mock.patch.object(
                    benchmark,
                    "run_vector",
                    side_effect=vector_effect,
                ), redirect_stdout(io.StringIO()):
                    self.assertEqual(benchmark.main(), 2)

                connection.close.assert_called_once_with()
                self.assertEqual(lock_events, ["enter", "exit"])



if __name__ == "__main__":
    unittest.main()
