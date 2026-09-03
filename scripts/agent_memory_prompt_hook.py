#!/usr/bin/env python3
"""UserPromptSubmit hook: surface relevant vault memories as prompt context.

The Stop hook closes the *write* half of the loop. This closes the *read* half:
without it, recall depends on the model remembering to run a search, which is
exactly the kind of instruction that gets skipped under load.

Contract: **fail open, always**. Any error, timeout, malformed payload, or empty
result exits 0 with no output. A memory problem must never block or delay the
user's prompt, so every failure path is silent by design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEARCH_SCRIPT = REPO_ROOT / "scripts" / "agent_memory_search.py"

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
from agent_memory_host import actor_names  # noqa: E402

# Prompts shorter than this are continuations ("继续", "ok", "go on") that carry
# no retrievable intent; searching on them returns noise.
MIN_PROMPT_CHARS = 8

# Slash commands and pure paste-throughs are handled by their own tooling.
SKIP_PREFIXES = ("/", "!", "#")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inject relevant Agent Memory entries into the Claude prompt context."
    )
    parser.add_argument("--actor", default="claude", choices=actor_names(hook_only=True))
    parser.add_argument("--limit", type=int, default=3, help="max memories injected per prompt")
    parser.add_argument(
        "--min-score",
        type=float,
        default=1.0,
        help="drop results below this relevance score",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=8,
        help="hard cap on the search subprocess; expiry injects nothing",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1800,
        help="cap on injected context so recall cannot crowd out the task",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="re-inject memories already surfaced earlier in this session",
    )
    return parser.parse_args()


def read_payload() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def should_skip(prompt: str) -> bool:
    text = prompt.strip()
    if len(text) < MIN_PROMPT_CHARS:
        return True
    return text.startswith(SKIP_PREFIXES)


def heartbeat(session_id: str, outcome: str, injected: int, prompt_chars: int, actor: str = "claude") -> None:
    """Record that the hook was invoked at all.

    Both memory hooks are silent when there is nothing to do, which makes
    "the hook never ran" and "the hook ran and had nothing to say"
    indistinguishable — the exact ambiguity that let a half-installed Claude
    setup look healthy for five days. A compact per-invocation line makes
    "is it actually loaded?" answerable from evidence instead of inference.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from agent_memory_env import env_value, resolve_config_path  # noqa: PLC0415

        root = resolve_config_path(
            env_value("CONFIG_ROOT", "$HOME/.config/agent-memory")
        )
        path = root / "logs" / "prompt-hook.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": int(time.time()),
            "event": "prompt-hook",
            "actor": actor,
            "outcome": outcome,
            "injected": injected,
            "promptChars": prompt_chars,
            # Hash, never the raw id or prompt text.
            "session_hash": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
            if session_id
            else "",
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Observability must never break the hook's fail-open contract.
        return


def _state_dir() -> Path | None:
    """Per-session dedupe stamps; None when no writable location resolves."""
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from agent_memory_env import env_value, resolve_config_path  # noqa: PLC0415

        root = resolve_config_path(
            env_value("CONFIG_ROOT", "$HOME/.config/agent-memory")
        )
        target = root / "hooks" / "prompt-recall"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        return None


def _seen_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    root = _state_dir()
    if root is None:
        return None
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return root / f"{digest}.json"


def load_seen(session_id: str) -> set[str]:
    path = _seen_path(session_id)
    if path is None or not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    # A stale stamp from a reused session id would suppress recall forever.
    if time.time() - float(payload.get("updated", 0)) > 86400:
        return set()
    values = payload.get("paths")
    return {str(v) for v in values} if isinstance(values, list) else set()


def save_seen(session_id: str, paths: set[str]) -> None:
    path = _seen_path(session_id)
    if path is None:
        return
    try:
        path.write_text(
            json.dumps({"updated": time.time(), "paths": sorted(paths)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        return


def search(query: str, args: argparse.Namespace) -> list[dict]:
    command = [
        sys.executable,
        str(SEARCH_SCRIPT),
        query,
        "--limit",
        str(max(args.limit * 2, args.limit)),
        "--json",
        # Read-only: a recall on every prompt must not migrate schema or write
        # a search-log row.
        "--no-log",
        "--agent-scope",
        args.actor,
    ]
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _caveats(entry: dict) -> str:
    """Surface the vault's own trust metadata so recall is not taken as fact."""
    notes: list[str] = []
    if entry.get("requires_live_verification"):
        notes.append("需现场核实")
    if entry.get("analogy_only"):
        notes.append("仅作类比")
    if str(entry.get("time_status") or "") == "expired":
        notes.append("已过期")
    status = str(entry.get("status") or "")
    if status and status != "active":
        notes.append(f"status={status}")
    return f"（{'，'.join(notes)}）" if notes else ""


def render(entries: list[dict], max_chars: int) -> str:
    lines = [
        "以下是长期记忆库中与本次输入相关的条目（自动召回，非用户输入）。",
        "它们是写入时点的观察，可能已过时；涉及文件/接口/命令时请先核实再据此行动。",
        "",
    ]
    for entry in entries:
        title = str(entry.get("title") or entry.get("rel_path") or "(untitled)").strip()
        kind = str(entry.get("memory_type") or "").strip()
        verified = str(entry.get("verified_at") or "").strip()
        path = str(entry.get("path") or "").strip()
        meta = " · ".join(x for x in (kind, f"verified {verified}" if verified else "") if x)
        lines.append(f"- **{title}**{_caveats(entry)}")
        if meta:
            lines.append(f"  {meta}")
        hit = " ".join(str(entry.get("hit") or "").split())
        if hit:
            lines.append(f"  {hit[:240]}")
        if path:
            lines.append(f"  {path}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（已截断）"
    return text


def main() -> int:
    args = parse_args()
    payload = read_payload()
    prompt = str(payload.get("prompt") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not prompt or should_skip(prompt):
        heartbeat(session_id, "skipped", 0, len(prompt), args.actor)
        return 0
    if not SEARCH_SCRIPT.is_file():
        heartbeat(session_id, "no-search-script", 0, len(prompt), args.actor)
        return 0

    entries = search(prompt, args)
    if not entries:
        heartbeat(session_id, "no-results", 0, len(prompt), args.actor)
        return 0
    seen = set() if args.no_dedupe else load_seen(session_id)

    selected: list[dict] = []
    for entry in entries:
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < args.min_score:
            continue
        path = str(entry.get("path") or entry.get("rel_path") or "")
        if path and path in seen:
            continue
        selected.append(entry)
        if path:
            seen.add(path)
        if len(selected) >= args.limit:
            break

    if not selected:
        heartbeat(session_id, "below-threshold-or-seen", 0, len(prompt), args.actor)
        return 0
    if not args.no_dedupe:
        save_seen(session_id, seen)
    heartbeat(session_id, "injected", len(selected), len(prompt), args.actor)

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": render(selected, args.max_chars),
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
        # Fail open: recall is an enhancement, never a gate on the user's prompt.
        raise SystemExit(0)
