#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[0]
RUNTIME_ROOT = SCRIPT_ROOT.parent
DEFAULT_LIMIT = 5
DEFAULT_BENCHMARK_FILE = RUNTIME_ROOT / "benchmarks" / "public-sample.json"
MAX_DATASET_BYTES = 2 * 1024 * 1024
MAX_CASES = 200
MAX_QUERY_CHARS = 20_000


class DatasetError(ValueError):
    """A stable, content-free benchmark input error."""


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot_load_module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_expected_path(raw: object) -> str:
    if not isinstance(raw, str):
        raise DatasetError("case_expected_paths_must_be_strings")
    value = str(raw).strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".md":
        raise DatasetError("case_expected_must_be_safe_relative_markdown_path")
    return path.as_posix()


def load_dataset(path: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        selected = Path(path).expanduser().resolve() if path else DEFAULT_BENCHMARK_FILE.resolve()
        raw_data = selected.read_bytes()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DatasetError("dataset_unreadable") from exc
    if len(raw_data) > MAX_DATASET_BYTES:
        raise DatasetError("dataset_too_large")
    try:
        data = json.loads(raw_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError("dataset_invalid_json") from exc
    if isinstance(data, list):
        metadata: dict[str, object] = {
            "schema_version": 0,
            "name": selected.stem,
            "privacy": "private_local" if path else "public_sample",
            "legacy_array": True,
        }
        raw_cases = data
    elif isinstance(data, dict) and isinstance(data.get("cases"), list):
        declared_privacy = str(data.get("privacy") or "private_local")
        if declared_privacy not in {"public_sample", "private_local"}:
            raise DatasetError("dataset_privacy_is_unsupported")
        try:
            schema_version = int(data.get("schema_version") or 1)
        except (TypeError, ValueError) as exc:
            raise DatasetError("dataset_schema_version_is_invalid") from exc
        metadata = {
            "schema_version": schema_version,
            "name": str(data.get("name") or selected.stem),
            # An explicitly supplied file is outside the bundled trust
            # boundary.  It cannot opt itself into public output merely by
            # declaring `privacy: public_sample` inside its own contents.
            "privacy": "private_local" if path else declared_privacy,
            "declared_privacy": declared_privacy,
            "legacy_array": False,
        }
        raw_cases = data["cases"]
    else:
        raise DatasetError("dataset_requires_array_or_object_with_cases")
    if metadata["privacy"] not in {"public_sample", "private_local"}:
        raise DatasetError("dataset_privacy_is_unsupported")
    if not raw_cases or len(raw_cases) > MAX_CASES:
        raise DatasetError("dataset_case_count_is_invalid")
    cases: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for item in raw_cases:
        if not isinstance(item, dict) or "id" not in item or "query" not in item or "expected" not in item:
            raise DatasetError("case_requires_id_query_expected")
        if not isinstance(item["id"], str) or not isinstance(item["query"], str):
            raise DatasetError("case_id_and_query_must_be_strings")
        case_id = item["id"].strip()
        query = item["query"].strip()
        expected_raw = item["expected"]
        if not case_id or case_id in seen_ids:
            raise DatasetError("case_ids_must_be_unique_and_nonempty")
        if not query or not isinstance(expected_raw, list) or not expected_raw:
            raise DatasetError("case_query_and_expected_must_be_nonempty")
        if len(query) > MAX_QUERY_CHARS:
            raise DatasetError("case_query_too_large")
        required_at_raw = item.get("required_at")
        try:
            required_at = int(required_at_raw) if required_at_raw not in (None, "") else 0
        except (TypeError, ValueError) as exc:
            raise DatasetError("case_required_at_is_invalid") from exc
        if required_at < 0 or required_at > 100:
            raise DatasetError("case_required_at_must_be_between_0_and_100")
        seen_ids.add(case_id)
        cases.append(
            {
                "id": case_id,
                "query": query,
                "expected": [validate_expected_path(value) for value in expected_raw],
                "required_at": required_at,
                "tags": [str(value) for value in item.get("tags", [])] if isinstance(item.get("tags", []), list) else [],
            }
        )
    metadata["path"] = (
        str(selected)
        if path
        else selected.relative_to(RUNTIME_ROOT.resolve()).as_posix()
    )
    metadata["sha256"] = hashlib.sha256(raw_data).hexdigest()
    return metadata, cases


def output_dataset_metadata(dataset: dict[str, object], *, private_redacted: bool) -> dict[str, object]:
    if not private_redacted:
        return dict(dataset)
    # Do not emit a user-selected absolute path or self-declared dataset name.
    # A content hash is sufficient to identify the exact private fixture run.
    return {
        "schema_version": dataset.get("schema_version", 0),
        "privacy": "private_local",
        "legacy_array": bool(dataset.get("legacy_array", False)),
        "sha256": dataset.get("sha256", ""),
    }


def redacted_case_ref(case_id: object, ordinal: int) -> str:
    digest = hashlib.sha256(str(case_id).encode("utf-8")).hexdigest()[:12]
    return f"case-{ordinal:03d}-{digest}"


def first_hit_rank(results: list[str], expected: list[str]) -> int | None:
    expected_set = set(expected)
    for index, rel_path in enumerate(results, 1):
        if rel_path in expected_set:
            return index
    return None


def metrics(ranks: list[int | None]) -> dict[str, float]:
    total = len(ranks) or 1
    return {
        "cases": len(ranks),
        "hit@1": sum(1 for rank in ranks if rank is not None and rank <= 1) / total,
        "hit@3": sum(1 for rank in ranks if rank is not None and rank <= 3) / total,
        "hit@5": sum(1 for rank in ranks if rank is not None and rank <= 5) / total,
        "mrr": sum((1 / rank) for rank in ranks if rank is not None) / total,
    }


def format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def run_sqlite(sqlite_index: Any, query: str, limit: int) -> list[str]:
    with sqlite_index.connect() as conn:
        rows = sqlite_index.search(conn, query, limit)
    return [str(row["rel_path"]) for row in rows]


def run_vector(zvec_index: Any, vector_conn: Any, store: Any, embedder: Any, query: str, limit: int) -> list[str]:
    query_embedding = embedder.embed_query(query)
    scored_ids = store.search(query_embedding, max(limit * 12, limit))
    rows = zvec_index.vector_rows(vector_conn, scored_ids, query)[:limit]
    return [str(row["rel_path"]) for row in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare SQLite/FTS retrieval with optional Zvec semantic retrieval.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Top K used for hit@K and result display.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--no-vector", action="store_true", help="Only run SQLite baseline.")
    parser.add_argument("--case-id", action="append", default=[], help="Run only this benchmark case id. Repeatable.")
    parser.add_argument("--benchmark-file", default="", help="Optional JSON array of benchmark cases.")
    parser.add_argument(
        "--show-private-details",
        action="store_true",
        help="Explicitly show private query text and paths. Private datasets are redacted by default.",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for the serialized Zvec collection lock.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    limit = max(args.limit, 1)
    try:
        dataset, loaded_cases = load_dataset(args.benchmark_file)
    except DatasetError as exc:
        payload = {"status": "error", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"retrieval-benchmark=error {exc}")
        return 2
    private_redacted = bool(args.benchmark_file) and not args.show_private_details
    if dataset.get("privacy") == "private_local" and not args.show_private_details:
        private_redacted = True
    case_ids = set(args.case_id)
    cases = [case for case in loaded_cases if not case_ids or str(case["id"]) in case_ids]
    if not cases:
        print("no_cases_selected", file=sys.stderr)
        return 1

    sqlite_index = load_module("agent_memory_index_module", SCRIPT_ROOT / "agent_memory_index.py")
    records: list[dict[str, object]] = []
    sqlite_ranks: list[int | None] = []
    vector_ranks: list[int | None] = []

    for ordinal, case in enumerate(cases, 1):
        query = str(case["query"])
        expected = [str(item) for item in case["expected"]]  # type: ignore[index]
        sqlite_results = run_sqlite(sqlite_index, query, limit)
        sqlite_rank = first_hit_rank(sqlite_results, expected)
        sqlite_ranks.append(sqlite_rank)
        records.append({
            "id": case["id"],
            "case_ref": redacted_case_ref(case["id"], ordinal),
            "query": query,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "query_length": len(query),
            "expected": expected,
            "required_at": int(case.get("required_at") or 0),
            "sqlite_rank": sqlite_rank,
            "sqlite_results": sqlite_results,
            "vector_rank": None,
            "vector_results": [],
        })

    vector_error = ""
    if not args.no_vector:
        try:
            zvec_index = load_module("agent_memory_zvec_index_module", SCRIPT_ROOT / "agent_memory_zvec_index.py")
            with zvec_index.zvec_lock(exclusive=True, timeout=max(float(args.lock_timeout), 0.0)):
                vector_conn = zvec_index.connect()
                try:
                    zvec_index.init_db(vector_conn)
                    store = zvec_index.ZvecStore(
                        zvec_index.DEFAULT_COLLECTION_PATH,
                        zvec_index.DEFAULT_EMBEDDING_DIM,
                    )
                    store.init()
                    embedder = zvec_index.EmbeddingGemmaEmbedder(
                        zvec_index.DEFAULT_MODEL,
                        zvec_index.DEFAULT_EMBEDDING_DIM,
                        zvec_index.DEFAULT_DEVICE,
                        "",
                    )
                    embedder.embed_query("benchmark preflight")
                    for record in records:
                        vector_results = run_vector(
                            zvec_index,
                            vector_conn,
                            store,
                            embedder,
                            str(record["query"]),
                            limit,
                        )
                        vector_rank = first_hit_rank(vector_results, list(record["expected"]))
                        vector_ranks.append(vector_rank)
                        record["vector_rank"] = vector_rank
                        record["vector_results"] = vector_results
                finally:
                    vector_conn.close()
        except Exception as exc:
            vector_error = str(exc)

    gate_failures: list[dict[str, object]] = []
    for record in records:
        required_at = int(record["required_at"] or 0)
        if not required_at:
            continue
        sqlite_rank = record["sqlite_rank"]
        if sqlite_rank is None or int(sqlite_rank) > required_at:
            gate_failures.append({
                ("case_ref" if private_redacted else "id"): (
                    record["case_ref"] if private_redacted else record["id"]
                ),
                "backend": "sqlite",
                "required_at": required_at,
                "rank": sqlite_rank,
            })
        vector_rank = record["vector_rank"]
        if not args.no_vector and not vector_error and (vector_rank is None or int(vector_rank) > required_at):
            gate_failures.append({
                ("case_ref" if private_redacted else "id"): (
                    record["case_ref"] if private_redacted else record["id"]
                ),
                "backend": "vector",
                "required_at": required_at,
                "rank": vector_rank,
            })

    output_records: list[dict[str, object]] = []
    for record in records:
        if private_redacted:
            output_records.append({
                "case_ref": record["case_ref"],
                "query_sha256": record["query_sha256"],
                "query_length": record["query_length"],
                "required_at": record["required_at"],
                "sqlite_rank": record["sqlite_rank"],
                "vector_rank": record["vector_rank"],
            })
        else:
            output_records.append(record)

    output: dict[str, object] = {
        "status": "failed_gate" if gate_failures else "ok",
        "dataset": output_dataset_metadata(dataset, private_redacted=private_redacted),
        "limit": limit,
        "case_count": len(cases),
        "sqlite": metrics(sqlite_ranks),
        "vector": metrics(vector_ranks) if vector_ranks else None,
        "vector_error": (
            {
                "redacted": True,
                "sha256": hashlib.sha256(vector_error.encode("utf-8")).hexdigest(),
                "length": len(vector_error),
            }
            if private_redacted and vector_error
            else vector_error
        ),
        "gate_failures": gate_failures,
        "private_details_redacted": private_redacted,
        "records": output_records,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if not vector_error and not gate_failures else (2 if vector_error else 3)

    sqlite_metrics = output["sqlite"]
    vector_metrics = output["vector"]
    print(f"cases={len(cases)} limit={limit}")
    print(
        "sqlite "
        f"hit@1={format_pct(sqlite_metrics['hit@1'])} "
        f"hit@3={format_pct(sqlite_metrics['hit@3'])} "
        f"hit@5={format_pct(sqlite_metrics['hit@5'])} "
        f"mrr={sqlite_metrics['mrr']:.3f}"
    )
    if vector_metrics:
        print(
            "vector "
            f"hit@1={format_pct(vector_metrics['hit@1'])} "
            f"hit@3={format_pct(vector_metrics['hit@3'])} "
            f"hit@5={format_pct(vector_metrics['hit@5'])} "
            f"mrr={vector_metrics['mrr']:.3f}"
        )
    else:
        safe_vector_error = output["vector_error"]
        if isinstance(safe_vector_error, dict):
            print(
                "vector error="
                f"[redacted:{str(safe_vector_error.get('sha256', ''))[:12]} "
                f"len={safe_vector_error.get('length', 0)}]"
            )
        else:
            print(f"vector error={safe_vector_error or 'not_run'}")
    print("")
    for record in output_records:
        query_label = record.get("query") or f"[redacted:{str(record['query_sha256'])[:12]} len={record['query_length']}]"
        identity = record.get("id") or record.get("case_ref")
        print(f"[{identity}] {query_label}")
        if record.get("expected"):
            print(f"  expected: {', '.join(record['expected'])}")
        print(f"  sqlite_rank={record['sqlite_rank']} top={record.get('sqlite_results', [])[:3]}")
        if vector_metrics:
            print(f"  vector_rank={record['vector_rank']} top={record['vector_results'][:3]}")
    return 0 if not vector_error and not gate_failures else (2 if vector_error else 3)


if __name__ == "__main__":
    raise SystemExit(main())
