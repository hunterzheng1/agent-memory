#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_memory_env import env_value, resolve_config_path
from agent_memory_host import actor_names, resolve
import agent_memory_intent as write_intent
from agent_memory_state import absolute_path, secure_sqlite_connect


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = resolve_config_path(env_value("ROOT", str(RUNTIME_ROOT / "templates" / "vault")))
STATE_DB = absolute_path(
    resolve_config_path(env_value("STATE_DB", "$HOME/.config/agent-memory/state.sqlite"))
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def session_value(explicit: str = "", actor: str = "codex") -> str:
    return resolve(actor, explicit_session_id=explicit).session_id


def session_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    conn = secure_sqlite_connect(
        STATE_DB,
        timeout=10,
        create=not read_only,
        read_only=read_only,
        row_factory=sqlite3.Row,
        pragmas=("PRAGMA busy_timeout=10000",),
    )
    if not read_only:
        ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_session_claims (
          session_hash TEXT NOT NULL,
          actor TEXT NOT NULL,
          path TEXT NOT NULL,
          rel_path TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          claimed_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          intent_id TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (session_hash, path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_file_observations (
          path TEXT PRIMARY KEY,
          rel_path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          actor TEXT NOT NULL,
          session_hash TEXT NOT NULL DEFAULT '',
          observed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_session_claims_active "
        "ON memory_session_claims(status, actor, session_hash)"
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_session_claims)")}
    if "intent_id" not in columns:
        conn.execute("ALTER TABLE memory_session_claims ADD COLUMN intent_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_session_claims_active_intent "
        "ON memory_session_claims(intent_id) WHERE intent_id<>'' AND status='active'"
    )
    write_intent.ensure_schema(conn)
    conn.commit()


def record_file_observations(raw_session_id: str, actor: str, paths: list[Path]) -> int:
    rows: list[tuple[str, str, str]] = []
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        try:
            rel_path = path.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            continue
        rows.append((str(path), rel_path, file_sha256(path)))
    if not rows:
        return 0
    now = utc_now()
    hashed = session_hash(raw_session_id)
    with connect() as conn:
        for path, rel_path, digest in rows:
            conn.execute(
                """
                INSERT INTO memory_file_observations (
                  path, rel_path, sha256, actor, session_hash, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  rel_path=excluded.rel_path,
                  sha256=excluded.sha256,
                  actor=excluded.actor,
                  session_hash=excluded.session_hash,
                  observed_at=excluded.observed_at
                """,
                (path, rel_path, digest, actor, hashed, now),
            )
        conn.commit()
    return len(rows)


def normalize_claim_path(raw: str, *, allow_missing: bool = False) -> tuple[Path, str]:
    if allow_missing:
        target = write_intent.canonical_target(raw)
        if target.path.exists() and not target.path.is_file():
            raise ValueError(f"claim path is not a regular file: {target.path}")
        if not target.path.parent.is_dir():
            raise ValueError(f"claim parent directory does not exist: {target.path.parent}")
        return target.path, target.rel_path
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    try:
        rel_path = path.relative_to(VAULT_ROOT).as_posix()
    except ValueError as exc:
        raise ValueError(f"claim path is outside the memory vault: {path}") from exc
    if path.suffix.lower() != ".md":
        raise ValueError(f"claim path is not Markdown: {path}")
    if not path.exists():
        raise ValueError(f"claim path does not exist: {path}")
    return path, rel_path


def claim_paths(actor: str, raw_session_id: str, paths: list[str], intent_id: str = "") -> list[dict[str, str]]:
    hashed = session_hash(raw_session_id)
    if not hashed:
        raise ValueError("session id is required; pass --session-id or use a supported host session environment")
    normalized = [normalize_claim_path(raw, allow_missing=bool(intent_id)) for raw in paths]
    if intent_id and len(normalized) != 1:
        raise ValueError("one write intent can bind exactly one claimed file")
    for path, rel_path in normalized:
        if (
            write_intent.PROTECTED_PATHS
            and write_intent.ENFORCEMENT_MODE == "enforce"
            and write_intent.is_protected_target(path)
            and not intent_id
        ):
            raise ValueError(f"protected memory requires a bound write intent before editing: {rel_path}")
    now = utc_now()
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if intent_id:
                bound = write_intent.bind_claim(
                    intent_id,
                    actor=actor,
                    raw_session_id=raw_session_id,
                    claim_path=normalized[0][0],
                    claim_ref=f"{actor}:{hashed}:{normalized[0][1]}",
                    connection=conn,
                )
                if str(bound.get("target_key", "")) != write_intent.canonical_target(normalized[0][0]).target_key:
                    raise ValueError("write intent target does not match claimed file")
            for path, rel_path in normalized:
                existing = conn.execute(
                    "SELECT status, intent_id FROM memory_session_claims WHERE session_hash=? AND path=?",
                    (hashed, str(path)),
                ).fetchone()
                if (
                    existing is not None
                    and str(existing[0]) == "active"
                    and str(existing[1] or "")
                    and str(existing[1]) != intent_id
                ):
                    raise ValueError(f"active claim already has a different write intent: {rel_path}")
                conn.execute(
                    """
                    INSERT INTO memory_session_claims (
                      session_hash, actor, path, rel_path, status, claimed_at, updated_at, completed_at, intent_id
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, ?)
                    ON CONFLICT(session_hash, path) DO UPDATE SET
                      actor=excluded.actor,
                      rel_path=excluded.rel_path,
                      status='active',
                      updated_at=excluded.updated_at,
                      completed_at=NULL,
                      intent_id=CASE
                        WHEN memory_session_claims.status='active'
                             AND memory_session_claims.intent_id<>''
                             AND excluded.intent_id=''
                        THEN memory_session_claims.intent_id
                        ELSE excluded.intent_id
                      END
                    """,
                    (hashed, actor, str(path), rel_path, now, now, intent_id),
                )
            conn.commit()
    except write_intent.IntentError as exc:
        if intent_id and exc.reason_code in {"STALE_BASE", "INTENT_EXPIRED"}:
            try:
                write_intent.finalize_receipt(
                    intent_id,
                    actor=actor,
                    raw_session_id=raw_session_id,
                    outcome="expired" if exc.reason_code == "INTENT_EXPIRED" else "failed",
                    reason_code=exc.reason_code,
                    detail_code="CLAIM_BINDING_REJECTED",
                )
            except (write_intent.IntentError, OSError, sqlite3.Error):
                pass
        raise
    return [{"path": str(path), "rel_path": rel_path, "intent_id": intent_id} for path, rel_path in normalized]


def active_claim_rows(
    raw_session_id: str,
    actor: str = "",
    *,
    read_only: bool = False,
) -> list[dict[str, str]]:
    hashed = session_hash(raw_session_id)
    if not hashed:
        return []
    params: list[str] = [hashed]
    try:
        with connect(read_only=read_only) as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_session_claims)")}
            if not columns:
                return []
            intent_expression = "intent_id" if "intent_id" in columns else "'' AS intent_id"
            query = (
                "SELECT session_hash, actor, path, rel_path, status, claimed_at, updated_at, "
                f"{intent_expression} FROM memory_session_claims "
                "WHERE session_hash=? AND status='active'"
            )
            if actor:
                query += " AND actor=?"
                params.append(actor)
            query += " ORDER BY rel_path"
            rows = conn.execute(query, params).fetchall()
    except (OSError, sqlite3.Error):
        if read_only and not STATE_DB.exists():
            return []
        raise
    return [{key: str(row[key] or "") for key in row.keys()} for row in rows]


def parsed_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def all_active_claim_rows(max_age_hours: float | None = None) -> list[dict[str, str]]:
    if max_age_hours is not None and max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT session_hash, actor, path, rel_path, status, claimed_at, updated_at, intent_id
            FROM memory_session_claims
            WHERE status='active'
            ORDER BY actor, session_hash, rel_path
            """
        ).fetchall()
    payloads = [{key: str(row[key] or "") for key in row.keys()} for row in rows]
    if max_age_hours is None:
        return payloads
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    return [row for row in payloads if (parsed_time(row["updated_at"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= cutoff]


def stale_active_claim_rows(max_age_hours: float = 24) -> list[dict[str, str]]:
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    rows = all_active_claim_rows()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    return [row for row in rows if (parsed_time(row["updated_at"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) < cutoff]


def expire_stale_claims(max_age_hours: float = 24, apply: bool = False) -> tuple[list[dict[str, str]], int]:
    rows = stale_active_claim_rows(max_age_hours)
    if not apply or not rows:
        return rows, 0
    now = utc_now()
    changed = 0
    with connect() as conn:
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE memory_session_claims
                SET status='expired', completed_at=?, updated_at=?
                WHERE session_hash=? AND path=? AND status='active' AND updated_at=?
                """,
                (now, now, row["session_hash"], row["path"], row["updated_at"]),
            )
            changed += int(cursor.rowcount)
        conn.commit()
    return rows, changed


def complete_claim_paths(raw_session_id: str, actor: str, paths: list[Path]) -> int:
    hashed = session_hash(raw_session_id)
    if not hashed or not paths:
        return 0
    now = utc_now()
    with connect() as conn:
        placeholders = ",".join("?" for _ in paths)
        params: list[str] = [now, now, hashed, actor, *(str(path.resolve()) for path in paths)]
        cursor = conn.execute(
            f"""
            UPDATE memory_session_claims
            SET status='completed', completed_at=?, updated_at=?
            WHERE session_hash=? AND actor=? AND status='active'
              AND path IN ({placeholders})
            """,
            params,
        )
        conn.commit()
        return int(cursor.rowcount)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track per-session ownership of shared memory files.")
    parser.add_argument(
        "--actor",
        choices=actor_names(),
        default="codex",
    )
    parser.add_argument("--session-id", default="")
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    claim_parser = subparsers.add_parser("claim", help="Claim one or more Markdown files for this session.")
    claim_parser.add_argument("--file", action="append", required=True)
    claim_parser.add_argument("--intent-id", default="", help="Bind this single-file claim to a prepared write intent.")
    subparsers.add_parser("list", help="List active claims for this session.")
    subparsers.add_parser("list-all", help="List all active claims.")
    expire_parser = subparsers.add_parser("expire-stale", help="Preview or expire abandoned active claims.")
    expire_parser.add_argument("--older-than-hours", type=float, default=24)
    expire_parser.add_argument("--apply", action="store_true", help="Mark matching claims expired; default is preview only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_session_id = session_value(args.session_id, args.actor)
    applied = 0
    try:
        if args.action == "claim":
            rows = claim_paths(args.actor, raw_session_id, args.file, args.intent_id)
        elif args.action == "list-all":
            rows = all_active_claim_rows()
        elif args.action == "expire-stale":
            rows, applied = expire_stale_claims(args.older_than_hours, args.apply)
        else:
            if not raw_session_id:
                raise ValueError("session id is required; pass --session-id or use a supported host session environment")
            rows = active_claim_rows(raw_session_id, args.actor)
    except (ValueError, sqlite3.Error) as exc:
        payload: dict[str, Any] = {"ok": False, "error": str(exc), "action": args.action}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"claim_error={exc}")
        return 2
    payload = {
        "ok": True,
        "action": args.action,
        "actor": args.actor,
        "session_hash": session_hash(raw_session_id),
        "count": len(rows),
        "claims": rows,
        "applied": applied,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"claims={len(rows)} applied={applied} actor={args.actor} session={payload['session_hash']}")
        for row in rows:
            print(row.get("rel_path", row.get("path", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
