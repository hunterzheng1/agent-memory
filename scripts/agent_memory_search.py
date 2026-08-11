#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_memory_env import env_value, resolve_config_path
from agent_memory_host import scope_names
from agent_memory_state import secure_sqlite_connect


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts"
DEFAULT_VAULT_ROOT = REPO_ROOT / "templates" / "vault"
VAULT_ROOT = resolve_config_path(env_value("ROOT", str(DEFAULT_VAULT_ROOT)))
STATE_DB = resolve_config_path(env_value("STATE_DB", "$HOME/.config/agent-memory/state.sqlite"))
ZVEC_SCRIPT = SCRIPT_ROOT / "agent_memory_zvec_index.py"
ZVEC_PYTHON = env_value("ZVEC_PYTHON", sys.executable)

if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import agent_memory_index as memory_index  # noqa: E402


@dataclass
class SearchResult:
    path: str
    rel_path: str
    title: str = ""
    memory_type: str = ""
    track: str = ""
    project_id: str = ""
    status: str = ""
    verified_at: str = ""
    verified_at_source: str = ""
    valid_until: str = ""
    user_id: str = ""
    agent_id: str = ""
    agent_scope: str = "shared"
    app_id: str = ""
    session_id: str = ""
    has_open_loop: int = 0
    summary: str = ""
    hit: str = ""
    score: float = 0.0
    sources: set[str] = field(default_factory=set)
    source_details: dict[str, Any] = field(default_factory=dict)
    time_status: str = "unknown"
    scope_status: str = "unspecified"
    policy_warnings: list[str] = field(default_factory=list)
    can_authorize_action: bool = False
    requires_live_verification: bool = False
    analogy_only: bool = False
    current_project_context: str = ""

    def merge(self, other: "SearchResult") -> None:
        self.sources.update(other.sources)
        self.score += other.score
        self.source_details.update(other.source_details)
        for attr in (
            "title", "memory_type", "track", "project_id", "status", "verified_at",
            "verified_at_source", "valid_until", "user_id", "agent_id", "agent_scope", "app_id", "session_id", "summary", "hit",
        ):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "title": self.title,
            "memory_type": self.memory_type,
            "track": self.track,
            "project_id": self.project_id,
            "status": self.status,
            "verified_at": self.verified_at,
            "verified_at_source": self.verified_at_source,
            "valid_until": self.valid_until,
            "time_status": self.time_status,
            "scope_status": self.scope_status,
            "policy_warnings": self.policy_warnings,
            "can_authorize_action": self.can_authorize_action,
            "requires_live_verification": self.requires_live_verification,
            "analogy_only": self.analogy_only,
            "current_project_context": self.current_project_context,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "agent_scope": self.agent_scope or "shared",
            "app_id": self.app_id,
            "session_id": self.session_id,
            "summary": self.summary,
            "hit": self.hit,
            "sources": sorted(self.sources),
            "score": round(self.score, 4),
            "path": self.path,
            "source_details": self.source_details,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9_]{2,}", text.lower()):
        tokens.add(word)
    for seq in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if len(seq) <= 6:
            tokens.add(seq)
        for index in range(max(len(seq) - 1, 0)):
            tokens.add(seq[index : index + 2])
    return tokens


def coverage(query: str, text: str) -> float:
    query_tokens = tokenize(query)
    text_tokens = tokenize(text)
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def compact_match_text(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]+", "", text).lower()


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    return secure_sqlite_connect(
        STATE_DB,
        create=not read_only,
        read_only=read_only,
        row_factory=sqlite3.Row,
        pragmas=("PRAGMA busy_timeout=10000",),
    )


def row_to_result(row: sqlite3.Row, rank: int, query: str) -> SearchResult:
    searchable = " ".join(str(row[key] or "") for key in ("title", "rel_path", "summary", "hit"))
    term_coverage = coverage(query, searchable)
    compact_query = compact_match_text(query)
    compact_title = compact_match_text(str(row["title"] or ""))
    exact_bonus = 4.0 if compact_query and compact_query in compact_title else 0.0
    return SearchResult(
        path=str(row["path"]),
        rel_path=str(row["rel_path"]),
        title=str(row["title"] or ""),
        memory_type=str(row["memory_type"] or ""),
        track=str(row["track"] or ""),
        project_id=str(row["project_id"] or ""),
        status=str(row["status"] or ""),
        verified_at=str(row["verified_at"] or ""),
        verified_at_source=str(row["verified_at_source"] or ""),
        valid_until=str(row["valid_until"] or ""),
        user_id=str(row["user_id"] or ""),
        agent_id=str(row["agent_id"] or ""),
        agent_scope=str(row["agent_scope"] or "shared"),
        app_id=str(row["app_id"] or ""),
        session_id=str(row["session_id"] or ""),
        has_open_loop=int(row["has_open_loop"] or 0),
        summary=str(row["summary"] or ""),
        hit=str(row["hit"] or "").replace("\n", " "),
        score=(1.0 / max(rank, 1)) + (term_coverage * 3.0) + exact_bonus,
        sources={"sqlite"},
        source_details={"sqlite_rank": rank, "term_coverage": round(term_coverage, 4), "exact_title_bonus": exact_bonus},
    )


def enrich_from_db(result: SearchResult, conn: sqlite3.Connection) -> SearchResult:
    row = conn.execute(
        """
        SELECT path, rel_path, title, memory_type, track, project_id, status,
               verified_at, verified_at_source, valid_until, user_id, agent_id, agent_scope, app_id,
               session_id, has_open_loop, summary
        FROM memory_docs
        WHERE path=? OR rel_path=?
        LIMIT 1
        """,
        (result.path, result.rel_path),
    ).fetchone()
    if not row:
        return result
    result.path = str(row["path"])
    result.rel_path = str(row["rel_path"])
    result.title = result.title or str(row["title"] or "")
    result.memory_type = result.memory_type or str(row["memory_type"] or "")
    result.track = result.track or str(row["track"] or "")
    result.project_id = result.project_id or str(row["project_id"] or "")
    result.status = result.status or str(row["status"] or "")
    result.verified_at = result.verified_at or str(row["verified_at"] or "")
    result.verified_at_source = result.verified_at_source or str(row["verified_at_source"] or "")
    result.valid_until = result.valid_until or str(row["valid_until"] or "")
    result.user_id = result.user_id or str(row["user_id"] or "")
    result.agent_id = result.agent_id or str(row["agent_id"] or "")
    result.agent_scope = str(row["agent_scope"] or "shared")
    result.app_id = result.app_id or str(row["app_id"] or "")
    result.session_id = result.session_id or str(row["session_id"] or "")
    result.has_open_loop = int(row["has_open_loop"] or 0)
    result.summary = result.summary or str(row["summary"] or "")
    return result


def readonly_index_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    track: str = "",
    memory_type: str = "",
    user_id: str = "",
    agent_id: str = "",
    app_id: str = "",
    session_id: str = "",
    status: str = "",
    has_open_loop: bool = False,
) -> list[sqlite3.Row]:
    """Search an already initialized index without running schema setup.

    ``agent_memory_index.search`` intentionally initializes and migrates the
    database for normal interactive searches.  A closeout dry-run must be
    observably read-only, so this path performs the same SELECT/ranking work
    against the schema that is already present and never calls ``init_db``.
    """
    conn.row_factory = sqlite3.Row
    rows: list[sqlite3.Row] = []
    try:
        rows = list(
            conn.execute(
                """
                SELECT d.*, memory_fts.search_text AS search_text,
                       snippet(memory_fts, 6, '[', ']', '...', 12) AS hit
                FROM memory_fts
                JOIN memory_docs d ON d.path = memory_fts.path
                WHERE memory_fts MATCH ?
                ORDER BY bm25(memory_fts)
                LIMIT ?
                """,
                (memory_index.fts_query(query), max(limit * 12, 50)),
            )
        )
    except sqlite3.Error:
        rows = []

    seen = {str(row["path"]) for row in rows}
    terms = memory_index.lexical_terms(query)
    if terms:
        like_parts: list[str] = []
        params: list[object] = []
        for term in terms[:6]:
            like = f"%{term}%"
            like_parts.append(
                "(memory_fts.title LIKE ? OR memory_fts.rel_path LIKE ? "
                "OR memory_fts.summary LIKE ? OR memory_fts.search_text LIKE ?)"
            )
            params.extend([like, like, like, like])
        fallback = list(
            conn.execute(
                f"""
                SELECT d.*, memory_fts.search_text AS search_text,
                       substr(memory_fts.summary, 1, 160) AS hit
                FROM memory_fts
                JOIN memory_docs d ON d.path = memory_fts.path
                WHERE {' OR '.join(like_parts)}
                ORDER BY d.has_open_loop DESC, d.mtime DESC
                LIMIT ?
                """,
                [*params, max(limit * 20, 80)],
            )
        )
        for row in fallback:
            if str(row["path"]) not in seen:
                rows.append(row)
                seen.add(str(row["path"]))

    rows = [
        row
        for row in rows
        if memory_index.row_matches_filters(
            row,
            track,
            memory_type,
            "",
            user_id,
            agent_id,
            app_id,
            session_id,
            status,
            has_open_loop,
        )
    ]
    return memory_index.dedupe_and_rank(rows, query, limit)


def sqlite_search(args: argparse.Namespace) -> tuple[list[SearchResult], list[str]]:
    if not STATE_DB.exists():
        return [], [f"sqlite index missing: {STATE_DB}"]
    try:
        if bool(getattr(args, "no_log", False)):
            with connect(read_only=True) as conn:
                rows = readonly_index_search(
                    conn,
                    args.query,
                    backend_candidate_limit(args),
                    args.track,
                    args.memory_type,
                    args.user_id,
                    args.agent_id if not args.agent_scope else "",
                    args.app_id,
                    args.session_id,
                    args.status,
                    args.has_open_loop,
                )
        else:
            with memory_index.connect() as conn:
                rows = memory_index.search(
                    conn,
                    args.query,
                    backend_candidate_limit(args),
                    args.track,
                    args.memory_type,
                    # Exact project filtering happens after all backends are
                    # merged.  Passing this to the legacy index filter would
                    # apply substring matching and can hide the exact result.
                    "",
                    args.user_id,
                    args.agent_id if not args.agent_scope else "",
                    args.app_id,
                    args.session_id,
                    args.status,
                    args.has_open_loop,
                )
        return [row_to_result(row, rank, args.query) for rank, row in enumerate(rows, 1)], []
    except Exception as exc:  # pragma: no cover
        return [], [f"sqlite search failed: {exc}"]


def command_env_offline() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    return env


def zvec_search(args: argparse.Namespace) -> tuple[list[SearchResult], list[str]]:
    if args.no_zvec:
        return [], []
    if not ZVEC_SCRIPT.exists():
        return [], [f"zvec script missing: {ZVEC_SCRIPT}"]
    command = [
        ZVEC_PYTHON,
        str(ZVEC_SCRIPT),
        "--search",
        args.query,
        "--limit",
        str(backend_candidate_limit(args)),
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=args.zvec_timeout,
            env=command_env_offline(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], [f"zvec search timed out after {args.zvec_timeout}s"]
    except OSError as exc:
        return [], [f"zvec search failed to start: {exc}"]
    if completed.returncode != 0 and not completed.stdout.strip():
        detail = completed.stderr.strip() or f"returncode={completed.returncode}"
        return [], [f"zvec search failed: {detail}"]
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = completed.stderr.strip() or completed.stdout.strip()[:300]
        return [], [f"zvec returned non-json output: {detail}"]
    if payload.get("error"):
        return [], [f"zvec search failed: {payload['error']}"]
    rows = payload.get("results", [])
    if not isinstance(rows, list):
        return [], ["zvec returned invalid result shape"]
    if not rows:
        return [], []
    results: list[SearchResult] = []
    with connect(read_only=bool(getattr(args, "no_log", False))) as conn:
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            try:
                raw_value = row.get("raw_distance", row.get("vector_score"))
                rank_value = row.get("rank_distance", row.get("score", raw_value))
                raw_distance = float(raw_value) if raw_value is not None else None
                rank_distance = float(rank_value)
            except (TypeError, ValueError):
                continue
            filter_distance = raw_distance if raw_distance is not None else rank_distance
            if filter_distance > args.zvec_max_distance:
                continue
            semantic_quality = max(0.0, 1.0 - (filter_distance / args.zvec_max_distance))
            source_details: dict[str, Any] = {
                "zvec_rank": rank,
                # Compatibility: zvec_score historically exposed the
                # lexically adjusted distance used for ordering.  Keep
                # that meaning while callers migrate to the explicit rank
                # field.  Reconciliation must only use zvec_raw_distance.
                "zvec_score": rank_distance,
                "zvec_rank_distance": rank_distance,
                "zvec_rank_score": rank_distance,
                "zvec_score_semantics": "deprecated_rank_distance",
            }
            # Never relabel a legacy adjusted-only score as raw evidence.
            if raw_distance is not None:
                source_details["zvec_raw_distance"] = raw_distance
            result = SearchResult(
                path=str(row.get("path") or ""),
                rel_path=str(row.get("rel_path") or ""),
                title=str(row.get("title") or ""),
                memory_type=str(row.get("memory_type") or ""),
                track=str(row.get("track") or ""),
                project_id=str(row.get("project_id") or ""),
                verified_at=str(row.get("verified_at") or ""),
                summary=str(row.get("summary") or ""),
                hit=str(row.get("summary") or ""),
                score=(0.8 / max(rank, 1)) + (semantic_quality * 2.0)
                + coverage(args.query, " ".join(str(row.get(key) or "") for key in ("title", "rel_path", "summary")))
                * 2.0,
                sources={"zvec"},
                source_details=source_details,
            )
            results.append(enrich_from_db(result, conn))
    return results, []


def rg_search(args: argparse.Namespace) -> tuple[list[SearchResult], list[str]]:
    if not args.force_rg:
        return [], []
    command = ["rg", "--line-number", "--ignore-case", "--fixed-strings", "--", args.query, str(VAULT_ROOT)]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=args.rg_timeout, check=False)
    except FileNotFoundError:
        return [], ["rg not found"]
    except subprocess.TimeoutExpired:
        return [], [f"rg timed out after {args.rg_timeout}s"]
    if completed.returncode not in {0, 1}:
        return [], [completed.stderr.strip() or f"rg failed: {completed.returncode}"]
    results: list[SearchResult] = []
    seen: set[str] = set()
    with connect(read_only=bool(getattr(args, "no_log", False))) as conn:
        for line in completed.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            path = str(Path(parts[0]).resolve())
            if path in seen:
                continue
            seen.add(path)
            try:
                rel_path = Path(path).relative_to(VAULT_ROOT).as_posix()
            except ValueError:
                rel_path = path
            result = SearchResult(
                path=path,
                rel_path=rel_path,
                hit=parts[2].strip(),
                score=0.35 / max(len(seen), 1),
                sources={"rg"},
                source_details={"rg_line": parts[1]},
            )
            results.append(enrich_from_db(result, conn))
            if len(results) >= backend_candidate_limit(args):
                break
    return results, []


def merge_results(result_groups: list[list[SearchResult]]) -> list[SearchResult]:
    merged: dict[str, SearchResult] = {}
    for group in result_groups:
        for item in group:
            key = item.path or item.rel_path
            if not key:
                continue
            if key in merged:
                merged[key].merge(item)
            else:
                merged[key] = item
    rows = list(merged.values())
    rows.sort(key=lambda item: (item.score, item.verified_at), reverse=True)
    return rows


def backend_candidate_limit(args: argparse.Namespace) -> int:
    has_project_filter = bool(
        str(getattr(args, "current_project", "") or "").strip()
        or str(getattr(args, "project_id", "") or "").strip()
    )
    factor = 8 if has_project_filter else 2
    return max(int(args.limit) * factor, int(args.limit))


def result_matches_filters(result: SearchResult, args: argparse.Namespace) -> bool:
    if args.agent_scope and (result.agent_scope or "shared") not in {"shared", args.agent_scope}:
        return False
    for value, actual in (
        (args.track, result.track),
        (args.memory_type, result.memory_type),
        (args.user_id, result.user_id),
        (args.agent_id, result.agent_id),
        (args.app_id, result.app_id),
        (args.session_id, result.session_id),
    ):
        if value and value != actual:
            return False
    if args.project_id and not project_matches(args.project_id, result.project_id):
        return False
    current_project = str(getattr(args, "current_project", "") or "").strip()
    if current_project and project_scope(result, current_project) == "cross_project_reference":
        if not bool(getattr(args, "cross_project", False)):
            return False
    if args.status and result.status != args.status:
        return False
    if not args.status and not args.include_inactive and result.status != "active":
        return False
    if args.has_open_loop and result.has_open_loop != 1:
        return False
    if not args.memory_type and not args.include_supporting and result.memory_type in {"template", "directory_index"}:
        return False
    return bool(result.path and result.rel_path)


def project_matches(current_project: str, result_project: str) -> bool:
    current = unicodedata.normalize("NFKC", current_project.strip()).casefold()
    candidates = {
        unicodedata.normalize("NFKC", item.strip()).casefold()
        for item in result_project.split(",")
        if item.strip()
    }
    return bool(current) and current in candidates


def project_scope(result: SearchResult, current_project: str) -> str:
    project_values = {
        unicodedata.normalize("NFKC", item.strip()).casefold()
        for item in result.project_id.split(",")
        if item.strip()
    }
    if not project_values:
        return "unscoped_shared_reference"
    # A value is globally reusable only when every declared project id says so.
    # Mixed values such as ``global, project-a`` remain project-bound.
    if project_values <= {"global", "shared"}:
        return "global_shared"
    if project_matches(current_project, result.project_id):
        return "current_project"
    return "cross_project_reference"


def parsed_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except (AttributeError, ValueError):
        return None


def annotate_result_policy(result: SearchResult, args: argparse.Namespace) -> None:
    """Attach warnings without changing retrieval score or granting authority."""
    warnings: list[str] = []
    as_of = parsed_date(str(getattr(args, "as_of", "") or "")) or dt.datetime.now().date()
    if result.valid_until:
        boundary = parsed_date(result.valid_until)
        if boundary is None:
            result.time_status = "invalid"
            result.requires_live_verification = True
            warnings.append("invalid_valid_until")
        elif boundary < as_of:
            result.time_status = "expired"
            result.requires_live_verification = True
            warnings.append("expired_memory_reference_only")
        elif boundary == as_of:
            result.time_status = "expires_today"
            result.requires_live_verification = True
            warnings.append("memory_expires_today")
        else:
            result.time_status = "current"
    else:
        result.time_status = "unspecified"

    current_project = str(getattr(args, "current_project", "") or "").strip()
    result.current_project_context = current_project
    if not current_project:
        result.scope_status = "unspecified"
    else:
        result.scope_status = project_scope(result, current_project)
    if result.scope_status == "cross_project_reference":
        result.analogy_only = True
        warnings.append("cross_project_reference_only")

    if result.status and result.status != "active":
        warnings.append("inactive_or_historical_memory")
    result.policy_warnings = warnings
    result.can_authorize_action = False


def log_search(query: str, rows: list[SearchResult], duration_ms: int) -> None:
    try:
        with connect() as conn:
            memory_index.init_db(conn)
            digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
            sources = sorted({source for row in rows for source in row.sources})
            used_paths, used_paths_digest, used_path_count = memory_index.redacted_path_metadata(
                [row.rel_path for row in rows]
            )
            conn.execute(
                """
                INSERT INTO memory_search_log(
                  query, result_count, used_paths, used_paths_sha256, used_path_count,
                  query_sha256, query_length, sources, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"[redacted:{digest[:12]}]", len(rows), used_paths,
                    used_paths_digest, used_path_count, digest, len(query),
                    ",".join(sources), duration_ms, utc_now(),
                ),
            )
    except sqlite3.Error:
        return


def redact_legacy_search_logs() -> dict[str, int]:
    """Irreversibly remove legacy query text while retaining useful metadata."""
    with connect() as conn:
        memory_index.init_db(conn)
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, query, query_sha256, query_length,
                   used_paths, used_paths_sha256, used_path_count
            FROM memory_search_log
            WHERE query NOT LIKE '[redacted:%'
               OR (
                    COALESCE(used_paths, '') <> ''
                    AND used_paths NOT LIKE '[redacted:%'
                  )
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            query = str(row["query"] or "")
            query_is_redacted = query.startswith("[redacted:")
            digest = str(row["query_sha256"] or "")
            if not digest and not query_is_redacted:
                digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
            length = int(row["query_length"] or (0 if query_is_redacted else len(query)))
            redacted_query = query if query_is_redacted else f"[redacted:{digest[:12]}]"

            used_paths = str(row["used_paths"] or "")
            paths_are_redacted = not used_paths or used_paths.startswith("[redacted:")
            paths_digest = str(row["used_paths_sha256"] or "")
            path_count = int(row["used_path_count"] or 0)
            redacted_paths = used_paths
            if not paths_are_redacted:
                redacted_paths, paths_digest, path_count = memory_index.redacted_path_metadata(
                    used_paths.split(",")
                )
            conn.execute(
                """
                UPDATE memory_search_log
                SET query=?, query_sha256=?, query_length=?, used_paths=?,
                    used_paths_sha256=?, used_path_count=?
                WHERE id=?
                """,
                (
                    redacted_query, digest, length, redacted_paths,
                    paths_digest, path_count, int(row["id"]),
                ),
            )
        conn.commit()
        remaining = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM memory_search_log
                WHERE query NOT LIKE '[redacted:%'
                   OR (
                        COALESCE(used_paths, '') <> ''
                        AND used_paths NOT LIKE '[redacted:%'
                      )
                """
            ).fetchone()[0]
        )
    return {"redacted": len(rows), "remaining_raw": remaining}


def run_search(args: argparse.Namespace) -> tuple[list[SearchResult], list[str], bool]:
    started = time.monotonic()
    warnings: list[str] = []
    if STATE_DB.exists() and not bool(getattr(args, "no_log", False)):
        try:
            with memory_index.connect() as conn:
                memory_index.init_db(conn)
        except sqlite3.Error as exc:
            warnings.append(f"sqlite schema migration failed: {exc}")
    result_groups: list[list[SearchResult]] = []
    successful_backends: set[str] = set()
    with ThreadPoolExecutor(max_workers=3) as executor:
        tasks = {executor.submit(sqlite_search, args): "sqlite"}
        if not bool(getattr(args, "no_zvec", False)):
            tasks[executor.submit(zvec_search, args)] = "zvec"
        if args.force_rg:
            tasks[executor.submit(rg_search, args)] = "rg"
        for future in as_completed(tasks):
            backend = tasks[future]
            try:
                rows, task_warnings = future.result()
            except Exception as exc:  # pragma: no cover
                rows, task_warnings = [], [f"{backend} search task failed: {exc}"]
            if rows or not task_warnings:
                successful_backends.add(backend)
            warnings.extend(task_warnings)
            result_groups.append(rows)
    rows = merge_results(result_groups)
    rows = [row for row in rows if result_matches_filters(row, args)]
    for row in rows:
        annotate_result_policy(row, args)
    rows = rows[: args.limit]
    if not bool(getattr(args, "no_log", False)):
        log_search(args.query, rows, round((time.monotonic() - started) * 1000))
    return rows, warnings, not successful_backends


def print_human(query: str, rows: list[SearchResult], warnings: list[str]) -> None:
    print(f"query={query}")
    print(f"results={len(rows)}")
    for warning in warnings:
        print(f"warning: {warning}")
    for index, row in enumerate(rows, 1):
        print(f"{index}. {row.rel_path}")
        print(f"   title: {row.title}")
        print(f"   type: {row.memory_type} track={row.track} project_id={row.project_id} status={row.status}")
        print(f"   verified_at: {row.verified_at} source={row.verified_at_source}")
        print(f"   valid_until: {row.valid_until or '-'} time_status={row.time_status} scope={row.scope_status}")
        for warning in row.policy_warnings:
            print(f"   policy_warning: {warning}")
        print(f"   sources: {','.join(sorted(row.sources))} score={round(row.score, 4)}")
        if row.summary:
            print(f"   summary: {row.summary[:240]}")
        if row.hit:
            print(f"   hit: {row.hit[:240]}")
        print(f"   path: {row.path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified Agent Memory search: SQLite FTS plus optional Zvec semantic results.")
    parser.add_argument("query", nargs="?", help="Search query.")
    parser.add_argument("--search", dest="search", help="Search query, alternative to positional query.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum merged results.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--no-zvec", action="store_true", help="Skip semantic Zvec search.")
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Read-only search: do not migrate the SQLite schema or insert a search-log row.",
    )
    parser.add_argument("--force-rg", action="store_true", help="Also run rg as a manual fallback.")
    parser.add_argument("--zvec-timeout", type=int, default=45, help="Seconds before Zvec search times out.")
    parser.add_argument("--zvec-max-distance", type=float, default=0.72, help="Discard farther semantic results.")
    parser.add_argument("--rg-timeout", type=int, default=15, help="Seconds before rg fallback times out.")
    parser.add_argument("--track", default="", help="Filter all results by track.")
    parser.add_argument("--memory-type", default="", help="Filter all results by memory_type.")
    parser.add_argument("--project-id", default="", help="Filter all results by an exact normalized project_id value.")
    parser.add_argument("--current-project", default="", help="Current project id used to contain all project-scoped retrieval.")
    parser.add_argument(
        "--cross-project",
        action="store_true",
        help="Include other project-scoped memories as labeled references; never as authorization.",
    )
    parser.add_argument("--as-of", default="", help="Date used for valid_until checks (YYYY-MM-DD; defaults to today).")
    parser.add_argument("--user-id", default="", help="Filter all results by user_id.")
    parser.add_argument("--agent-id", default="", help="Filter all results by agent_id.")
    parser.add_argument(
        "--agent-scope",
        choices=scope_names(),
        default="",
        help="Return shared memories plus memories scoped to this Agent.",
    )
    parser.add_argument("--app-id", default="", help="Filter all results by app_id.")
    parser.add_argument("--session-id", default="", help="Filter all results by session_id.")
    parser.add_argument("--status", default="", help="Filter all results by status.")
    parser.add_argument("--has-open-loop", action="store_true", help="Only return docs with open loops.")
    parser.add_argument("--include-inactive", action="store_true", help="Include candidate/outdated statuses.")
    parser.add_argument("--include-supporting", action="store_true", help="Include templates and directory indexes.")
    parser.add_argument(
        "--redact-legacy-logs",
        action="store_true",
        help="Irreversibly redact legacy raw search queries while retaining hashes and lengths.",
    )
    args = parser.parse_args()
    args.query = args.search or args.query
    if not args.query and not args.redact_legacy_logs:
        parser.error("query is required")
    if args.cross_project and not args.current_project:
        parser.error("--cross-project requires --current-project")
    if args.no_log and args.redact_legacy_logs:
        parser.error("--no-log cannot be combined with --redact-legacy-logs")
    if args.as_of and parsed_date(args.as_of) is None:
        parser.error("--as-of must be YYYY-MM-DD")
    args.limit = max(args.limit, 1)
    return args


def main() -> int:
    args = parse_args()
    if args.redact_legacy_logs:
        payload = redact_legacy_search_logs()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"redacted={payload['redacted']} remaining_raw={payload['remaining_raw']}")
        return 0
    rows, warnings, all_enabled_backends_failed = run_search(args)
    if args.json:
        print(json.dumps({"query": args.query, "results": [row.to_dict() for row in rows], "warnings": warnings}, ensure_ascii=False, indent=2))
    else:
        print_human(args.query, rows, warnings)
    return 2 if all_enabled_backends_failed and not rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
