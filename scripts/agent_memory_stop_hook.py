#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_memory_env import env_value, resolve_config_path
from agent_memory_claim import active_claim_rows, all_active_claim_rows
from agent_memory_host import actor_names, resolve
from agent_memory_state import secure_append_text


REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = resolve_config_path(env_value("ROOT", str(REPO_ROOT / "templates" / "vault")))
CONFIG_ROOT = resolve_config_path(env_value("CONFIG_ROOT", "$HOME/.config/agent-memory"))
STATE_DB = resolve_config_path(env_value("STATE_DB", str(CONFIG_ROOT / "state.sqlite")))
LOG_PATH = resolve_config_path(env_value("CLOSEOUT_LOG", str(CONFIG_ROOT / "logs" / "closeout.jsonl")))
CLOSEOUT_SCRIPT = REPO_ROOT / "scripts" / "agent_memory_closeout.py"
AUDIT_AUTORUN = REPO_ROOT / "scripts" / "agent_memory_audit_autorun.py"
STAMP_ROOT = CONFIG_ROOT / "hooks"
HOOK_AUDIT_LOG = CONFIG_ROOT / "logs" / "stop-hook.jsonl"


def default_git_root() -> Path:
    for candidate in (VAULT_ROOT, *VAULT_ROOT.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return VAULT_ROOT.parent.resolve()


GIT_ROOT = resolve_config_path(env_value("GIT_ROOT", str(default_git_root())))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stop hook for Agent Memory shared by Claude Code, Codex, and CodeBuddy."
    )
    parser.add_argument("--actor", choices=actor_names(hook_only=True), default="codex")
    parser.add_argument("--protocol", choices=("codex", "claude"), default=None)
    parser.add_argument(
        "--event",
        choices=("stop-hook", "session-end"),
        default="stop-hook",
        help="Host lifecycle event used for closeout attribution and failure behavior.",
    )
    parser.add_argument(
        "--non-blocking",
        action="store_true",
        help="Report failures by notification only; required for lifecycle events that cannot block.",
    )
    parser.add_argument("--auto-closeout", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    expected_protocol = resolve(args.actor, env={}).hook_protocol
    if args.protocol is None:
        args.protocol = expected_protocol
    elif args.protocol != expected_protocol:
        parser.error(
            f"--protocol {args.protocol!r} conflicts with --actor {args.actor!r}; "
            f"expected {expected_protocol!r}"
        )
    return args


def read_payload() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "CREDENTIAL"))
        and "PROXY" not in key.upper()
    }


def session_key(payload: dict[str, object], actor: str) -> str:
    return resolve(actor, payload=payload).session_id


def run_git(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(GIT_ROOT), "-c", "core.quotepath=false", *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def vault_target() -> str:
    try:
        return str(VAULT_ROOT.relative_to(GIT_ROOT))
    except ValueError:
        return str(VAULT_ROOT)


def lexical_absolute(raw_path: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """Normalize dot segments without resolving symlinks or junction targets."""

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (base or GIT_ROOT) / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def lexical_path_key(raw_path: str | os.PathLike[str]) -> str:
    """Return a platform-aware identity key without filesystem traversal."""

    return os.path.normcase(os.path.normpath(os.fspath(lexical_absolute(raw_path))))


def lexical_path_within(path: Path, root: Path) -> bool:
    path_key = lexical_path_key(path)
    root_key = lexical_path_key(root)
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def lexical_repo_path(path: Path) -> str | None:
    candidate = lexical_absolute(path)
    git_root = lexical_absolute(GIT_ROOT)
    if not lexical_path_within(candidate, git_root):
        return None
    relative = os.path.relpath(os.fspath(candidate), os.fspath(git_root))
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return Path(relative).as_posix()


def normalize_path(repo_path: str) -> Path | None:
    path = lexical_absolute(repo_path, base=GIT_ROOT)
    if not lexical_path_within(path, lexical_absolute(VAULT_ROOT)):
        return None
    if path.suffix.lower() != ".md":
        return None
    return path


def dedupe_paths(paths: list[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        unique.setdefault(lexical_path_key(path), lexical_absolute(path))
    return list(unique.values())


def parse_porcelain_z(output: str) -> list[str]:
    """Parse porcelain-v1 -z, including the second rename/copy path."""

    items = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if not item:
            continue
        if len(item) < 4 or item[2] != " ":
            continue
        status = item[:2]
        destination = item[3:]
        paths.append(destination)
        change_kind = next((value for value in status if value in {"R", "C"}), "")
        if change_kind and index < len(items):
            source = items[index]
            index += 1
            if change_kind == "R" and source:
                paths.append(source)
    return paths


def dirty_paths() -> list[Path]:
    result = run_git(["status", "--porcelain=v1", "-z", "--", vault_target()])
    if not result or result.returncode != 0:
        return []
    paths: list[Path] = []
    for repo_path in parse_porcelain_z(result.stdout):
        path = normalize_path(repo_path)
        if path:
            paths.append(path)
    return dedupe_paths(paths)


def last_observed_head() -> str:
    if not LOG_PATH.exists():
        return ""
    for line in reversed(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("git_observed_through"):
            return str(payload["git_observed_through"])
    return ""


def historical_paths() -> list[Path]:
    baseline = last_observed_head()
    if not baseline:
        return []
    head_result = run_git(["rev-parse", "HEAD"])
    if not head_result or head_result.returncode != 0:
        return []
    head = head_result.stdout.strip()
    if not head or head == baseline:
        return []
    ancestor = run_git(["merge-base", "--is-ancestor", baseline, head])
    if not ancestor or ancestor.returncode != 0:
        return []
    diff = run_git(
        [
            "diff",
            "--find-renames",
            "--find-copies",
            "--name-status",
            "-z",
            f"{baseline}..{head}",
            "--",
            vault_target(),
        ]
    )
    if not diff or diff.returncode != 0:
        return []
    items = diff.stdout.split("\0")
    paths: list[Path] = []
    index = 0
    while index < len(items):
        status = items[index]
        index += 1
        if not status:
            continue
        path_count = 2 if status[0] in {"R", "C"} else 1
        changed = items[index : index + path_count]
        index += path_count
        selected = changed if status[0] == "R" else changed[-1:]
        for repo_path in selected:
            path = normalize_path(repo_path)
            if path is not None:
                paths.append(path)
    return dedupe_paths(paths)


def path_is_safe_regular(path: Path) -> bool:
    """Reject missing, non-regular, and symlinked path components lexically."""

    candidate = lexical_absolute(path)
    vault = lexical_absolute(VAULT_ROOT)
    if not lexical_path_within(candidate, vault):
        return False
    relative = os.path.relpath(os.fspath(candidate), os.fspath(vault))
    components = () if relative == os.curdir else Path(relative).parts
    current = vault
    chain = [vault]
    for component in components:
        current = current / component
        chain.append(current)
    for index, component_path in enumerate(chain):
        try:
            metadata = os.lstat(component_path)
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or os.path.islink(component_path):
            return False
        if index < len(chain) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return False
        if index == len(chain) - 1 and not stat.S_ISREG(metadata.st_mode):
            return False
    return bool(components)


def run_git_bytes(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(GIT_ROOT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_oid_length() -> int | None:
    result = run_git(["rev-parse", "--show-object-format"])
    if not result or result.returncode != 0:
        return None
    object_format = result.stdout.strip().lower()
    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    return None


def git_oid_is_valid(value: str) -> bool:
    length = git_oid_length()
    return bool(length and len(value) == length and all(character in "0123456789abcdef" for character in value))


def git_tree_entry(revision: str, repo_path: str) -> tuple[str, str, str] | None:
    result = run_git_bytes(["ls-tree", "-z", revision, "--", repo_path])
    if not result or result.returncode != 0:
        return None
    entries = [item for item in result.stdout.split(b"\0") if item]
    if len(entries) != 1:
        return None
    metadata, separator, raw_path = entries[0].partition(b"\t")
    fields = metadata.split()
    try:
        mode = fields[0].decode("ascii", errors="strict")
        object_type = fields[1].decode("ascii", errors="strict")
        oid = fields[2].decode("ascii", errors="strict").lower()
        decoded_path = raw_path.decode("utf-8", errors="strict")
    except (IndexError, UnicodeDecodeError):
        return None
    if separator != b"\t" or decoded_path != repo_path or not git_oid_is_valid(oid):
        return None
    return mode, object_type, oid


def git_head() -> str:
    result = run_git(["rev-parse", "--verify", "HEAD^{commit}"])
    if not result or result.returncode != 0:
        return ""
    value = result.stdout.strip().lower()
    return value if git_oid_is_valid(value) else ""


def latest_path_commit(revision: str, repo_path: str) -> str:
    result = run_git(["log", "-1", "--format=%H", revision, "--", repo_path])
    if not result or result.returncode != 0:
        return ""
    value = result.stdout.strip().lower()
    return value if git_oid_is_valid(value) else ""


def observation_matches_git(path: Path, observation: dict[str, str]) -> bool:
    repo_path = lexical_repo_path(path)
    if repo_path is None:
        return False
    head = git_head()
    current_entry = git_tree_entry(head, repo_path) if head else None
    if current_entry is None or current_entry[:2] not in {
        ("100644", "blob"),
        ("100755", "blob"),
    }:
        return False

    commit = observation.get("git_commit", "").strip().lower()
    blob_oid = observation.get("git_blob_oid", "").strip().lower()
    blob_sha256 = observation.get("git_blob_sha256", "").strip().lower()
    if (
        not git_oid_is_valid(commit)
        or not git_oid_is_valid(blob_oid)
        or len(blob_sha256) != 64
        or any(character not in "0123456789abcdef" for character in blob_sha256)
    ):
        return False
    ancestry = run_git(["merge-base", "--is-ancestor", commit, head])
    if not ancestry or ancestry.returncode != 0:
        return False
    observed_entry = git_tree_entry(commit, repo_path)
    if (
        observed_entry is None
        or observed_entry[:2] not in {("100644", "blob"), ("100755", "blob")}
        or observed_entry[2] != blob_oid
        or current_entry[2] != blob_oid
    ):
        return False
    blob = run_git_bytes(["cat-file", "blob", blob_oid])
    if (
        not blob
        or blob.returncode != 0
        or hashlib.sha256(blob.stdout).hexdigest() != blob_sha256
    ):
        return False
    observed_latest = latest_path_commit(commit, repo_path)
    return bool(
        observed_latest
        and observed_latest == latest_path_commit(head, repo_path)
    )


def file_sha256(path: Path) -> str:
    candidate = lexical_absolute(path)
    if not path_is_safe_regular(candidate):
        raise OSError(f"unsafe memory path: {candidate}")
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not path_is_safe_regular(candidate):
            raise OSError(f"unsafe memory path: {candidate}")
        current = os.stat(candidate, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"memory path identity changed: {candidate}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        if not path_is_safe_regular(candidate):
            raise OSError(f"memory path identity changed: {candidate}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def safe_path_mtime(path: Path) -> float | None:
    candidate = lexical_absolute(path)
    if not path_is_safe_regular(candidate):
        return None
    try:
        metadata = os.lstat(candidate)
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or not path_is_safe_regular(candidate):
        return None
    return metadata.st_mtime


def unobserved_paths(paths: list[Path]) -> list[Path]:
    if not paths or not STATE_DB.exists():
        return paths
    try:
        with sqlite3.connect(STATE_DB, timeout=5) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(memory_file_observations)")
            }
            selected_columns = [
                name if name in columns else f"'' AS {name}"
                for name in (
                    "actor",
                    "git_commit",
                    "git_blob_oid",
                    "git_blob_sha256",
                )
            ]
            rows = conn.execute(
                "SELECT path, sha256, "
                + ", ".join(selected_columns)
                + " FROM memory_file_observations"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return paths
    indexed: dict[str, dict[str, str] | None] = {}
    for row in rows:
        key = lexical_path_key(str(row[0]))
        record = {
            "sha256": str(row[1]),
            "actor": str(row[2]),
            "git_commit": str(row[3]),
            "git_blob_oid": str(row[4]),
            "git_blob_sha256": str(row[5]),
        }
        indexed[key] = None if key in indexed else record
    stale: list[Path] = []
    for raw_path in paths:
        path = lexical_absolute(raw_path)
        observation = indexed.get(lexical_path_key(path))
        if (
            not isinstance(observation, dict)
            or observation.get("actor") == "human"
            or not path_is_safe_regular(path)
            or not observation_matches_git(path, observation)
        ):
            stale.append(path)
            continue
        try:
            current = file_sha256(path)
        except OSError:
            stale.append(path)
            continue
        if observation.get("sha256") != current:
            stale.append(path)
    return dedupe_paths(stale)


def pending_paths() -> list[Path]:
    # A prior committed observation can explain Git history, never a currently
    # dirty worktree. Keeping dirty paths unconditional closes the post-commit
    # race where stale observation state otherwise silenced a new edit.
    dirty = dedupe_paths(dirty_paths())
    dirty_set = {lexical_path_key(path) for path in dirty}
    historical = [
        path for path in historical_paths() if lexical_path_key(path) not in dirty_set
    ]
    return dedupe_paths([*dirty, *unobserved_paths(historical)])


def notify(message: str) -> None:
    if sys.platform != "darwin":
        return
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "Agent memory"'], timeout=5, check=False)


def run_closeout(
    payload: dict[str, object],
    actor: str,
    timeout: int,
    trigger: str = "stop-hook",
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(CLOSEOUT_SCRIPT),
        "--commit",
        "--json",
        "--actor",
        actor,
        "--trigger",
        trigger,
        "--session-id",
        session_key(payload, actor),
        "--claimed-only",
        "--lock-timeout",
        "60",
    ]
    if trigger == "session-end":
        command.append("--skip-audit")
    try:
        # closeout replies with UTF-8 JSON that names Chinese vault paths;
        # locale decoding (cp936) would corrupt it before json.loads below.
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(timeout, 30),
            env=clean_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"closeout timed out after {timeout}s"}
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": (completed.stderr.strip() or "closeout returned no JSON")[:500]}
    return result if isinstance(result, dict) else {"status": "error", "error": "invalid closeout payload"}


def failure_reason(result: dict[str, Any]) -> str:
    parts = [str(result["error"])] if result.get("error") else []
    if result.get("ownership_error"):
        parts.append(str(result["ownership_error"]))
    findings = result.get("reconcile_findings")
    if isinstance(findings, list) and findings:
        parts.append(f"reconcile_findings={len(findings)}")
    warnings = result.get("warnings")
    if isinstance(warnings, list):
        parts.extend(str(item) for item in warnings[:3])
    return "; ".join(parts)[:1000] or f"closeout status={result.get('status', 'unknown')}"


def report_failure(protocol: str, result: dict[str, Any], *, non_blocking: bool = False) -> int:
    reason = failure_reason(result)
    notify(reason[:180])
    if non_blocking:
        return 0
    if protocol == "claude":
        print(json.dumps({"decision": "block", "reason": "Memory closeout failed: " + reason}, ensure_ascii=False))
        return 0
    print(
        "Shared memory closeout did not finish. Continue this turn, resolve the issue below, "
        "and run closeout again: " + reason,
        file=sys.stderr,
    )
    return 2


def stop_hook_reentry(payload: dict[str, object], event: str) -> bool:
    """Claude-compatible hosts mark a repeated Stop after continuation was requested."""

    return event == "stop-hook" and payload.get("stop_hook_active") is True


def handle_failure(
    protocol: str,
    result: dict[str, Any],
    *,
    payload: dict[str, object],
    event: str,
    non_blocking: bool,
) -> int:
    audit_lifecycle_failure(
        protocol,
        result,
        payload=payload,
        event=event,
    )
    return report_failure(
        protocol,
        result,
        non_blocking=non_blocking or event == "session-end" or stop_hook_reentry(payload, event),
    )


def audit_lifecycle_failure(
    protocol: str,
    result: dict[str, Any],
    *,
    payload: dict[str, object],
    event: str,
) -> None:
    """Persist a privacy-safe lifecycle failure, including non-blocking SessionEnd."""

    actor = str(result.get("actor", ""))
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    record = {
        "time": int(time.time()),
        "event": event,
        "protocol": protocol,
        "actor": actor,
        "status": str(result.get("status", "error")),
        "reason": failure_reason(result),
        "session_present": bool(session_id),
        "session_hash": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        if session_id
        else "",
    }
    try:
        secure_append_text(
            HOOK_AUDIT_LOG,
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        )
    except OSError:
        return


def record_invocation(
    *,
    actor: str,
    protocol: str,
    event: str,
    session_id: str,
    pending: int,
    claims: int,
    auto_closeout: bool,
) -> None:
    """Record that the hook was invoked, regardless of outcome.

    Until now this log only captured failures, so a hook that ran and had
    nothing to do was indistinguishable from a hook that was never wired up.
    That ambiguity is what let a half-installed Claude setup look healthy while
    writing nothing to the vault for five days. docs/automation.md tells people
    to verify here — this makes that advice actually actionable.
    """
    record = {
        "time": int(time.time()),
        "event": event,
        "protocol": protocol,
        "actor": actor,
        "status": "invoked",
        "pending": pending,
        "claims": claims,
        "autoCloseout": bool(auto_closeout),
        "session_present": bool(session_id),
        "session_hash": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        if session_id
        else "",
    }
    try:
        secure_append_text(
            HOOK_AUDIT_LOG,
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        )
    except OSError:
        return


def run_due_audit() -> None:
    if not AUDIT_AUTORUN.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(AUDIT_AUTORUN), "--reason", "hook", "--min-interval-days", "7", "--notify", "--json"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
            env=clean_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def main() -> int:
    args = parse_args()
    payload = read_payload()
    paths = pending_paths()
    raw_session_id = session_key(payload, args.actor)
    current_claims = active_claim_rows(
        raw_session_id,
        args.actor,
        max_age_hours=24,
    )
    record_invocation(
        actor=args.actor,
        protocol=args.protocol,
        event=args.event,
        session_id=raw_session_id,
        pending=len(paths),
        claims=len(current_claims),
        auto_closeout=bool(args.auto_closeout),
    )
    if args.auto_closeout and current_claims:
        result = run_closeout(payload, args.actor, args.timeout, args.event)
        return 0 if result.get("status") == "ok" else handle_failure(
            args.protocol,
            result,
            payload=payload,
            event=args.event,
            non_blocking=args.non_blocking,
        )
    if args.auto_closeout and paths:
        active_rows = all_active_claim_rows(max_age_hours=24, read_only=True)
        if not raw_session_id:
            ambiguous_paths: list[Path] = []
            for path in paths:
                path_key = lexical_path_key(path)
                covering = [
                    row
                    for row in active_rows
                    if lexical_path_key(str(row.get("path", ""))) == path_key
                ]
                proven_other_actor = bool(covering) and all(
                    str(row.get("actor", ""))
                    and str(row.get("actor", "")) != args.actor
                    for row in covering
                )
                if not proven_other_actor:
                    ambiguous_paths.append(path)
            if ambiguous_paths:
                result = {
                    "status": "error",
                    "actor": args.actor,
                    "ownership_error": (
                        "MISSING_HOST_SESSION_ID: host hook payload has no session id; "
                        "same-actor or unclaimed memory changes cannot be attributed safely"
                    ),
                    "ambiguous_path_count": len(ambiguous_paths),
                }
                return handle_failure(
                    args.protocol,
                    result,
                    payload=payload,
                    event=args.event,
                    non_blocking=args.non_blocking,
                )
            if args.event != "session-end":
                run_due_audit()
            return 0
        all_claimed_paths = {
            lexical_path_key(str(row["path"])) for row in active_rows
        }
        unclaimed = [
            path for path in paths if lexical_path_key(path) not in all_claimed_paths
        ]
        if not unclaimed:
            if args.event != "session-end":
                run_due_audit()
            return 0
        if unclaimed:
            result = {
                "status": "error",
                "ownership_error": (
                    f"{len(unclaimed)} changed memory file(s) are not claimed by any session; "
                    f"run memoryctl --actor {args.actor} claim --file <path> for files owned by this session"
                ),
                "unclaimed_files": [str(path) for path in unclaimed],
            }
            return handle_failure(
                args.protocol,
                result,
                payload=payload,
                event=args.event,
                non_blocking=args.non_blocking,
            )
    if not args.auto_closeout and paths:
        state_mtime = STATE_DB.stat().st_mtime if STATE_DB.exists() else 0
        path_mtimes: list[float] = []
        for path in paths:
            modified_at = safe_path_mtime(path)
            if modified_at is not None:
                path_mtimes.append(modified_at)
        if historical_paths() or len(path_mtimes) < len(paths) or max(path_mtimes, default=0) > state_mtime:
            STAMP_ROOT.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(session_key(payload, args.actor).encode("utf-8")).hexdigest()[:16]
            stamp = STAMP_ROOT / f"stop-memory-reminded-{args.actor}-{digest}.stamp"
            if not stamp.exists():
                stamp.write_text(str(int(time.time())), encoding="utf-8")
                notify(f"{len(paths)} memory files still need closeout.")
    if args.event != "session-end":
        run_due_audit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
