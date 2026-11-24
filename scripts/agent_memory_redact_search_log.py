#!/usr/bin/env python3
"""Irreversibly redact legacy raw rows in ``memory_search_log``.

Why this exists
---------------
``agent_memory_index.py`` has written redacted search-log rows for a while:
``query`` becomes ``[redacted:<sha256[:12]>]`` and ``used_paths`` becomes a single
``[redacted:<digest>]`` token with the digest, count and length kept in dedicated
columns. Rows written before that change still hold the raw query text or the
list of memory paths a search touched.

``memory_search_log`` is derived telemetry, not a fact source: the Markdown vault
is the source of truth and the log only records "a search happened, how many
results, how long". Redacting it therefore loses no memory content — only the
literal query/path text, which is exactly what should not persist.

What this script does
---------------------
For every row whose ``query`` or ``used_paths`` is not already a
``[redacted:...]`` token, replace the raw value with the same redaction format
the live writer uses, and fill the statistical columns (``query_sha256``,
``query_length``, ``used_paths_sha256``, ``used_path_count``) when they are empty
so counts/durations remain analysable. ``result_count``, ``sources``,
``duration_ms`` and ``created_at`` are never touched.

Safety
------
- Dry run by default; ``--apply`` performs the update.
- ``--backup-dir`` copies the database file (plus ``-wal``/``-shm``) before any
  write. Nothing is deleted.
- Runs inside one transaction; ``--verify`` re-counts afterwards.
- Idempotent: a second run finds no legacy rows.

Usage
-----
    python scripts/agent_memory_redact_search_log.py --json
    python scripts/agent_memory_redact_search_log.py --apply --backup-dir <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import agent_memory_env as env  # noqa: E402
import agent_memory_state as state  # noqa: E402

LEGACY_PREDICATE = """
    query NOT LIKE '[redacted:%'
    OR (
        COALESCE(used_paths, '') <> ''
        AND used_paths NOT LIKE '[redacted:%'
       )
"""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def state_db_path() -> Path:
    return state.absolute_path(env.env_value("STATE_DB", str(Path.home() / ".config" / "agent-memory" / "state.sqlite")))


def count_legacy(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM memory_search_log WHERE {LEGACY_PREDICATE}").fetchone()[0])


def plan_redaction(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT id, query, used_paths, query_sha256, query_length,
               used_paths_sha256, used_path_count
        FROM memory_search_log
        WHERE {LEGACY_PREDICATE}
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    plan: list[dict[str, Any]] = []
    for row in rows:
        row_id, query, used_paths, q_sha, q_len, p_sha, p_count = row
        query_text = "" if query is None else str(query)
        paths_text = "" if used_paths is None else str(used_paths)
        new_query = query_text
        new_q_sha = q_sha or ""
        new_q_len = q_len
        if not query_text.startswith("[redacted:"):
            digest = _digest(query_text)
            new_query = f"[redacted:{digest[:12]}]"
            new_q_sha = new_q_sha or digest
            if new_q_len is None:
                new_q_len = len(query_text)
        new_paths = paths_text
        new_p_sha = p_sha or ""
        new_p_count = p_count
        if paths_text and not paths_text.startswith("[redacted:"):
            # Paths were stored comma-separated; hash the same normalised set the
            # live writer hashes so old and new rows stay comparable.
            items = sorted({part.strip() for part in paths_text.split(",") if part.strip()})
            digest = _digest("\n".join(items))
            new_paths = f"[redacted:{digest[:12]}]"
            new_p_sha = new_p_sha or digest
            if new_p_count is None:
                new_p_count = len(items)
        plan.append(
            {
                "id": row_id,
                "query": new_query,
                "query_sha256": new_q_sha,
                "query_length": new_q_len,
                "used_paths": new_paths,
                "used_paths_sha256": new_p_sha,
                "used_path_count": new_p_count,
            }
        )
    return plan


def backup_database(db: Path, backup_dir: Path) -> list[str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{db}{suffix}")
        if not source.exists():
            continue
        target = backup_dir / source.name
        shutil.copy2(source, target)
        saved.append(str(target))
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Redact legacy raw rows in memory_search_log.")
    parser.add_argument("--apply", action="store_true", help="Write the redaction (default: dry run).")
    parser.add_argument("--limit", type=int, default=100000, help="Maximum rows to process.")
    parser.add_argument("--backup-dir", default="", help="Directory for a pre-write database copy.")
    parser.add_argument("--verify", action="store_true", help="Re-count legacy rows after applying.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    db = state_db_path()
    if not db.exists():
        payload = {"ok": False, "error": "state_db_missing", "path": str(db)}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload["error"])
        return 2

    conn = state.secure_sqlite_connect(db, repair_permissions=False, row_factory=sqlite3.Row)
    try:
        before = count_legacy(conn)
        plan = plan_redaction(conn, max(1, args.limit))
        payload: dict[str, Any] = {
            "ok": True,
            "database": str(db),
            "mode": "apply" if args.apply else "dry_run",
            "legacy_rows_before": before,
            "planned_rows": len(plan),
            "backups": [],
            "legacy_rows_after": None,
        }
        if args.apply and plan:
            if args.backup_dir:
                payload["backups"] = backup_database(db, Path(args.backup_dir).resolve())
            else:
                # Never write without a copy: the redaction is irreversible.
                payload["ok"] = False
                payload["error"] = "backup_dir_required_for_apply"
                print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload["error"])
                return 2
            # NOTE: PrivateSQLiteConnection.__exit__ closes the handle, so commit
            # explicitly instead of using "with conn:" (the post-apply verify and
            # the finally-close below still need a live connection).
            conn.executemany(
                """
                UPDATE memory_search_log
                   SET query = ?, query_sha256 = ?, query_length = ?,
                       used_paths = ?, used_paths_sha256 = ?, used_path_count = ?
                 WHERE id = ?
                """,
                [
                    (
                        item["query"],
                        item["query_sha256"],
                        item["query_length"],
                        item["used_paths"],
                        item["used_paths_sha256"],
                        item["used_path_count"],
                        item["id"],
                    )
                    for item in plan
                ],
            )
            conn.commit()
            if args.verify:
                payload["legacy_rows_after"] = count_legacy(conn)
        elif not args.apply:
            payload["legacy_rows_after"] = before
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"mode={payload['mode']} legacy_before={payload['legacy_rows_before']} "
                  f"planned={payload['planned_rows']} legacy_after={payload['legacy_rows_after']}")
            for item in payload["backups"]:
                print(f"backup: {item}")
        return 0 if payload["ok"] else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
