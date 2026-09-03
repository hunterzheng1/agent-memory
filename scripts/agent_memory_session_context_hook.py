#!/usr/bin/env python3
"""SessionStart hook for ZCode: put the real session id into model context.

ZCode has no CLAUDE_ENV_FILE bridge (the Claude mechanism for persisting env
across a session), and the interactive shell never sees the session id. Manual
`memoryctl claim` / `closeout` calls therefore cannot attribute files to this
session unless the id is visible to the model. This hook surfaces it once at
session start via additionalContext, the same route Cursor takes with an
explicit --session-id.

Contract: **fail open, always**. Any error, timeout, or missing session id
exits 0 with no output; a bridge problem must never block session startup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inject the Agent Memory session id into the ZCode session context."
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="fallback id from the ${CLAUDE_SESSION_ID} template variable",
    )
    return parser.parse_args()


def read_payload() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_session_id(payload: dict[str, object], fallback: str) -> str:
    for key in ("session_id", "sessionId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return fallback.strip()


def heartbeat(session_id: str, outcome: str) -> None:
    """Record that the hook was invoked, mirroring the prompt hook's trail.

    Silent hooks are indistinguishable from absent ones; this line makes
    "is the ZCode wiring actually loaded?" answerable from evidence.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from agent_memory_env import env_value, resolve_config_path  # noqa: PLC0415

        root = resolve_config_path(env_value("CONFIG_ROOT", "$HOME/.config/agent-memory"))
        path = root / "logs" / "session-context-hook.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": int(time.time()),
            "event": "session-context-hook",
            "actor": "zcode",
            "outcome": outcome,
            "session_hash": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
            if session_id
            else "",
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Observability must never break the hook's fail-open contract.
        return


def main() -> int:
    args = parse_args()
    payload = read_payload()
    session_id = resolve_session_id(payload, args.session_id)
    if not session_id:
        heartbeat("", "no-session-id")
        return 0
    heartbeat(session_id, "emitted")

    context = (
        f"Agent Memory 会话桥接：本次会话 ID 为 `{session_id}`。"
        f"之后在本会话的终端里手动执行记忆库 claim/closeout 时，"
        f"请传 `--session-id {session_id}`（actor 用 zcode）。"
        "读取/写入规则见 E:\\Agent Memory\\AGENTS.md。"
    )
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
