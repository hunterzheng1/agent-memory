#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_memory_env import env_value, load_config, resolve_config_path
from agent_memory_state import absolute_path, secure_sqlite_connect, sqlite_permission_report
from install_runtime import CORE_FILES, managed_path_issue, verify as verify_runtime_install


VERSION = "2.4"
LEXICAL_RUNTIME_ROOT = absolute_path(Path(__file__).parent.parent)
REPO_ROOT = LEXICAL_RUNTIME_ROOT


def _lexical_config_path(raw: str) -> Path:
    expanded = raw
    home = str(Path.home())
    expanded = re.sub(r"^\$\{HOME\}(?=[/\\]|$)", lambda _match: home, expanded)
    expanded = re.sub(r"^\$HOME(?=[/\\]|$)", lambda _match: home, expanded)
    expanded = os.path.expandvars(expanded)
    expanded = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        expanded,
    )
    return absolute_path(Path(expanded).expanduser())


def _configured_file_lexical_path() -> Path:
    explicit = os.environ.get("AGENT_MEMORY_CONFIG_FILE", "").strip()
    if explicit:
        return _lexical_config_path(explicit)
    return LEXICAL_RUNTIME_ROOT / "config" / "agent-memory.toml"


def _initial_config_root() -> Path:
    explicit_root = os.environ.get("AGENT_MEMORY_CONFIG_ROOT", "").strip()
    if explicit_root:
        return _lexical_config_path(explicit_root)
    explicit_file = os.environ.get("AGENT_MEMORY_CONFIG_FILE", "").strip()
    if explicit_file:
        config_file = _lexical_config_path(explicit_file)
        return config_file.parent.parent if config_file.parent.name == "config" else config_file.parent
    manifest = LEXICAL_RUNTIME_ROOT / "config" / "runtime-manifest.json"
    if managed_path_issue(LEXICAL_RUNTIME_ROOT, manifest, expected_kind="file") is None:
        try:
            if stat.S_ISREG(os.lstat(manifest).st_mode):
                return LEXICAL_RUNTIME_ROOT
        except FileNotFoundError:
            pass
    return _lexical_config_path("$HOME/.config/agent-memory")


def _config_path_issues(config_root: Path, config_file: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    targets = (
        (config_root, config_root, "directory"),
        (config_root, config_root / "config", "directory"),
        (config_file.parent, config_file.parent, "directory"),
        (config_file.parent, config_file, "file"),
    )
    for root, target, expected_kind in targets:
        issue = managed_path_issue(root, target, expected_kind=expected_kind)
        if issue is not None and issue not in issues:
            issues.append(issue)
    return issues


def _runtime_path_issues() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    targets = (
        (LEXICAL_RUNTIME_ROOT, "directory"),
        (LEXICAL_RUNTIME_ROOT / "scripts", "directory"),
        (absolute_path(__file__), "file"),
    )
    for target, expected_kind in targets:
        issue = managed_path_issue(
            LEXICAL_RUNTIME_ROOT,
            target,
            expected_kind=expected_kind,
        )
        if issue is not None and issue not in issues:
            issues.append(issue)
    return issues


CONFIG_FILE = _configured_file_lexical_path()
CONFIG_ROOT = _initial_config_root()
PRELOAD_PATH_ISSUES = _runtime_path_issues()
for _issue in _config_path_issues(CONFIG_ROOT, CONFIG_FILE):
    if _issue not in PRELOAD_PATH_ISSUES:
        PRELOAD_PATH_ISSUES.append(_issue)
RUNTIME_CONFIG: dict[str, Any] = {}
if not PRELOAD_PATH_ISSUES:
    RUNTIME_CONFIG = load_config()
    configured_root = _lexical_config_path(
        str(os.environ.get("AGENT_MEMORY_CONFIG_ROOT") or RUNTIME_CONFIG.get("config_root") or CONFIG_ROOT)
    )
    PRELOAD_PATH_ISSUES.extend(_config_path_issues(configured_root, CONFIG_FILE))
    CONFIG_ROOT = configured_root

SCRIPT_ROOT = REPO_ROOT / "scripts"
RUNTIME_MANIFEST = CONFIG_ROOT / "config" / "runtime-manifest.json"
RUNTIME_FILES = CORE_FILES
HOST_CONFIG = RUNTIME_CONFIG.get("host", {})
if not isinstance(HOST_CONFIG, dict):
    HOST_CONFIG = {}
SEMANTIC_CONFIG = RUNTIME_CONFIG.get("semantic_retrieval", {})
if not isinstance(SEMANTIC_CONFIG, dict):
    SEMANTIC_CONFIG = {}
WRITE_INTENT_CONFIG = RUNTIME_CONFIG.get("write_intents", {})
if not isinstance(WRITE_INTENT_CONFIG, dict):
    WRITE_INTENT_CONFIG = {}
SEMANTIC_ENABLED = bool(SEMANTIC_CONFIG.get("enabled", False))
if PRELOAD_PATH_ISSUES:
    VAULT_ROOT = CONFIG_ROOT / "unsafe-unresolved-vault"
    GIT_ROOT = CONFIG_ROOT
    STATE_DB = CONFIG_ROOT / "state.sqlite"
    AUDIT_LOG = CONFIG_ROOT / "logs" / "audit_runs.jsonl"
    CLOSEOUT_LOG = CONFIG_ROOT / "logs" / "closeout.jsonl"
    ZVEC_PYTHON = CONFIG_ROOT / ".venv" / "bin" / "python"
    EMBEDDING_MODEL = Path()
    MODEL_MANIFEST = CONFIG_ROOT / "models" / "embeddinggemma-300m" / "model-manifest.json"
    MODEL_REVISION = ""
    DEPENDENCY_LOCK = CONFIG_ROOT / "requirements-vector.lock"
    REQUIRE_LOCAL_MODEL = False
else:
    VAULT_ROOT = resolve_config_path(env_value("ROOT", str(REPO_ROOT / "templates" / "vault")))
    GIT_ROOT = resolve_config_path(env_value("GIT_ROOT", str(REPO_ROOT)))
    STATE_DB = absolute_path(
        resolve_config_path(env_value("STATE_DB", str(CONFIG_ROOT / "state.sqlite")))
    )
    AUDIT_LOG = resolve_config_path(
        env_value("AUDIT_RUN_LOG", str(CONFIG_ROOT / "logs" / "audit_runs.jsonl"))
    )
    CLOSEOUT_LOG = resolve_config_path(
        env_value("CLOSEOUT_LOG", str(CONFIG_ROOT / "logs" / "closeout.jsonl"))
    )
    ZVEC_PYTHON = resolve_config_path(
        env_value("ZVEC_PYTHON", str(CONFIG_ROOT / ".venv" / "bin" / "python"))
    )
    _EMBEDDING_MODEL_RAW = env_value("EMBEDDING_MODEL", "")
    EMBEDDING_MODEL = (
        resolve_config_path(_EMBEDDING_MODEL_RAW) if _EMBEDDING_MODEL_RAW else Path()
    )
    MODEL_MANIFEST = resolve_config_path(
        env_value(
            "MODEL_MANIFEST",
            str(CONFIG_ROOT / "models" / "embeddinggemma-300m" / "model-manifest.json"),
        )
    )
    MODEL_REVISION = env_value("MODEL_REVISION", "")
    DEPENDENCY_LOCK = resolve_config_path(
        env_value("DEPENDENCY_LOCK", str(CONFIG_ROOT / "requirements-vector.lock"))
    )
    REQUIRE_LOCAL_MODEL = env_value("REQUIRE_LOCAL_MODEL", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
EXCLUDED_VECTOR_TYPES = {"routing", "directory_index", "template", "agent_case_candidate", "skill_candidate"}
EXCLUDED_VECTOR_STATUS = {"archived", "deleted", "obsolete", "outdated", "deprecated", "stale"}
STALE_CLAIM_HOURS = 24
REMOTE_BACKUP_MAX_UNPUSHED_COMMITS = 10
REMOTE_BACKUP_MAX_AGE_DAYS = 3


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def run(command: list[str], timeout: int = 300, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": 127, "detail": type(exc).__name__}
    return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout, "detail": (completed.stderr or completed.stdout).strip()[:500]}


def add(checks: list[dict[str, Any]], name: str, status: str, message: str, detail: dict[str, Any] | None = None) -> None:
    checks.append({"name": name, "status": status, "message": message, "detail": detail or {}})


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text_sha256(path: Path) -> str:
    # Must match agent_memory_index.py, which hashes the decoded text after
    # universal-newline translation (CRLF -> LF), not the raw bytes.
    text = path.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def closeout_observation_health() -> tuple[bool, dict[str, Any]]:
    """Report formal-memory Git history that lacks a completed observation."""

    try:
        import agent_memory_closeout as closeout

        baseline = closeout.last_observed_git_head()
        head, head_warnings = closeout.current_git_head()
        entries, history_warnings = closeout.git_history_entries(baseline, head)
        pending = closeout.unobserved_history_entries(entries)
    except (
        AttributeError,
        ImportError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        return False, {"error": type(exc).__name__}
    warnings = [*head_warnings, *history_warnings]
    detail = {
        "baseline": baseline,
        "head": head,
        "history_paths": len(entries),
        "pending_count": len(pending),
        "pending_existing": sorted(
            closeout.relative_to_vault(entry.path) for entry in pending if not entry.is_deleted
        ),
        "pending_deleted": sorted(
            closeout.relative_to_vault(entry.path) for entry in pending if entry.is_deleted
        ),
        "warnings": warnings,
    }
    return bool(baseline and head and not warnings and not pending), detail


def search_log_privacy_health(conn: sqlite3.Connection) -> tuple[bool, dict[str, int]]:
    """Use the same legacy-row predicate as the irreversible redaction command."""

    raw_rows = int(
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
    return raw_rows == 0, {"legacy_raw_rows": raw_rows}


def offline_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def verify_model_manifest() -> tuple[bool, dict[str, Any]]:
    manifest = read_json_object(MODEL_MANIFEST)
    root = resolve_config_path(str(manifest.get("root", ""))) if manifest else Path()
    files = manifest.get("files") if isinstance(manifest, dict) else None
    missing: list[str] = []
    size_mismatch: list[str] = []
    hash_mismatch: list[str] = []
    symlinks: list[str] = []
    if not manifest or not root.is_dir() or not isinstance(files, dict):
        return False, {"manifest": str(MODEL_MANIFEST), "root": str(root), "error": "manifest_or_root_missing"}
    for rel_path, expected in files.items():
        path = root / str(rel_path)
        if path.is_symlink():
            symlinks.append(str(rel_path))
        if not path.is_file():
            missing.append(str(rel_path))
            continue
        expected_size = expected.get("size") if isinstance(expected, dict) else None
        expected_hash = expected.get("sha256") if isinstance(expected, dict) else None
        if expected_size is not None and path.stat().st_size != int(expected_size):
            size_mismatch.append(str(rel_path))
            continue
        if expected_hash and file_sha256(path) != str(expected_hash):
            hash_mismatch.append(str(rel_path))
    revision = str(manifest.get("revision", ""))
    revision_ok = not MODEL_REVISION or MODEL_REVISION == revision
    ok = not missing and not size_mismatch and not hash_mismatch and not symlinks and revision_ok
    return ok, {
        "manifest": str(MODEL_MANIFEST),
        "root": str(root),
        "revision": revision,
        "expected_revision": MODEL_REVISION,
        "checked_files": len(files),
        "missing": missing,
        "size_mismatch": size_mismatch,
        "hash_mismatch": hash_mismatch,
        "symlinks": symlinks,
    }


def verify_dependency_lock() -> tuple[bool, dict[str, Any]]:
    if not DEPENDENCY_LOCK.is_file() or not ZVEC_PYTHON.is_file():
        return False, {"lock": str(DEPENDENCY_LOCK), "python": str(ZVEC_PYTHON), "error": "lock_or_python_missing"}
    code = """
import importlib.metadata as metadata
import json
import re
import sys
expected = {}
for raw in open(sys.argv[1], encoding='utf-8'):
    line = raw.strip()
    if not line or line.startswith('#') or '==' not in line:
        continue
    name, version = line.split('==', 1)
    expected[name] = version
missing = []
mismatched = []
for name, version in expected.items():
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError:
        missing.append(name)
        continue
    if actual != version:
        mismatched.append({'name': name, 'expected': version, 'actual': actual})
print(json.dumps({'expected': len(expected), 'missing': missing, 'mismatched': mismatched}))
raise SystemExit(0 if not missing and not mismatched else 2)
"""
    result = run([str(ZVEC_PYTHON), "-c", code, str(DEPENDENCY_LOCK)], 60, offline_env())
    try:
        detail = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError:
        detail = {"error": result.get("detail", "invalid_dependency_check_output")}
    detail.update({"lock": str(DEPENDENCY_LOCK), "python": str(ZVEC_PYTHON)})
    return bool(result["ok"]), detail


def verify_semantic_python_runtime() -> tuple[bool, dict[str, Any]]:
    if not ZVEC_PYTHON.is_file():
        return False, {"python": str(ZVEC_PYTHON), "error": "python_missing_or_broken_symlink"}
    code = """
import json
import os
import sys
base = getattr(sys, '_base_executable', '') or sys.executable
print(json.dumps({
    'executable': sys.executable,
    'base_executable': base,
    'base_exists': os.path.isfile(base),
    'version': '.'.join(str(part) for part in sys.version_info[:3]),
}))
"""
    result = run([str(ZVEC_PYTHON), "-c", code], 30, offline_env())
    try:
        detail = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError:
        detail = {"error": result.get("detail", "invalid_python_runtime_output")}
    detail.update({"python": str(ZVEC_PYTHON), "returncode": result.get("returncode")})
    ok = bool(result["ok"] and detail.get("base_exists"))
    if result["ok"] and not detail.get("base_exists"):
        detail["error"] = "base_interpreter_missing"
    return ok, detail


def offline_semantic_probe() -> tuple[bool, dict[str, Any]]:
    command = [
        str(ZVEC_PYTHON),
        str(SCRIPT_ROOT / "agent_memory_zvec_index.py"),
        "--search",
        "Agent Memory offline healthcheck",
        "--limit",
        "1",
        "--json",
    ]
    result = run(command, 240, offline_env())
    try:
        payload = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError:
        return False, {"error": result.get("detail", "non_json_probe"), "returncode": result.get("returncode")}
    rows = payload.get("results") if isinstance(payload, dict) else None
    ok = bool(result["ok"] and isinstance(rows, list) and rows)
    return ok, {
        "returncode": result.get("returncode"),
        "result_count": len(rows) if isinstance(rows, list) else 0,
        "model": str(EMBEDDING_MODEL),
        "offline": True,
        "error": payload.get("error", "") if isinstance(payload, dict) else "",
    }


def parse_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def latest_jsonl(path: Path, predicate: Any = None) -> dict[str, Any] | None:
    if not path.exists():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and (not predicate or predicate(item)):
            latest = item
    return latest


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def claude_compatible_hook_semantics(
    hooks: dict[str, Any],
    actor: str,
) -> tuple[bool, dict[str, Any]]:
    """Validate lifecycle semantics for Claude-protocol hosts without actor aliasing."""

    if actor not in {"claude", "codebuddy"}:
        return False, {"error": "unsupported_actor"}

    def command_entries(event: str) -> list[dict[str, Any]]:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            return []
        entries: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            entries.extend(item for item in group["hooks"] if isinstance(item, dict))
        return entries

    def has_value(command: str, flag: str, value: str) -> bool:
        return re.search(
            rf"(?<!\S){re.escape(flag)}\s+{re.escape(value)}(?=\s|$)",
            command,
        ) is not None

    def has_switch(command: str, flag: str) -> bool:
        return re.search(rf"(?<!\S){re.escape(flag)}(?=\s|$)", command) is not None

    stop_ok = any(
        "agent_memory_stop_hook.py" in (command := str(item.get("command", "")))
        and has_value(command, "--actor", actor)
        and has_value(command, "--protocol", "claude")
        and has_value(command, "--event", "stop-hook")
        and not has_switch(command, "--non-blocking")
        and has_switch(command, "--auto-closeout")
        for item in command_entries("Stop")
    )
    session_end_ok = any(
        "agent_memory_stop_hook.py" in (command := str(item.get("command", "")))
        and has_value(command, "--actor", actor)
        and has_value(command, "--protocol", "claude")
        and has_value(command, "--event", "session-end")
        and has_switch(command, "--non-blocking")
        and has_switch(command, "--auto-closeout")
        and isinstance(item.get("timeout"), (int, float))
        and 0 < float(item["timeout"]) <= 60
        for item in command_entries("SessionEnd")
    )
    session_start_ok = any(
        "agent_memory_session_hook.py" in (command := str(item.get("command", "")))
        and has_value(command, "--actor", actor)
        and isinstance(item.get("timeout"), (int, float))
        and 0 < float(item["timeout"]) <= 10
        for item in command_entries("SessionStart")
    )
    return stop_ok and session_end_ok and session_start_ok, {
        "stop_scoped_and_blocking": stop_ok,
        "session_end_non_blocking": session_end_ok,
        "session_start_bridge": session_start_ok,
    }


def claude_hook_semantics(hooks: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    return claude_compatible_hook_semantics(hooks, "claude")


def configured_path(name: str) -> Path | None:
    raw = HOST_CONFIG.get(name)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return resolve_config_path(raw)


def local_endpoint_reachable(raw_url: str) -> tuple[bool, dict[str, Any]]:
    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return True, {"url_type": "remote_or_unset"}
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True, {"host": host, "port": port, "listening": True}
    except OSError:
        return False, {"host": host, "port": port, "listening": False}


def cc_switch_hooks_match(db_path: Path, expected_hooks: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    if not db_path.exists():
        return True, {"installed": False}
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        common = conn.execute("SELECT value FROM settings WHERE key = 'common_config_claude'").fetchone()
        backups = conn.execute("SELECT original_config FROM proxy_live_backup WHERE app_type = 'claude'").fetchall()
        conn.close()
        common_payload = json.loads(str(common[0])) if common else {}
        backup_payloads = [json.loads(str(row[0])) for row in backups]
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        return False, {"installed": True, "error": type(exc).__name__}
    common_ok = isinstance(common_payload, dict) and common_payload.get("hooks") == expected_hooks
    backups_ok = all(isinstance(payload, dict) and payload.get("hooks") == expected_hooks for payload in backup_payloads)
    return common_ok and backups_ok, {
        "installed": True,
        "common_config_ok": common_ok,
        "backup_count": len(backup_payloads),
        "backups_ok": backups_ok,
    }


def git_remote_has_credential() -> bool:
    result = run(["git", "-C", str(GIT_ROOT), "config", "--get-regexp", r"^remote\..*\.url$"], 15)
    if result["returncode"] not in {0, 1}:
        return False
    for line in str(result.get("stdout", "")).splitlines():
        _, _, url = line.partition(" ")
        if re.search(r"https?://[^/@\s]+:[^/@\s]+@", url) or re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", url):
            return True
    return False


def memory_git_baseline_result(
    dirty_count: int,
    git_ok: bool,
    allow_dirty_memory: bool,
) -> tuple[str, str, dict[str, Any]]:
    if dirty_count and allow_dirty_memory:
        return (
            "pass",
            f"Memory Git baseline has {dirty_count} expected pre-commit dirty files.",
            {"dirty_count": dirty_count, "allowed_precommit": True},
        )
    if dirty_count:
        return (
            "warn",
            f"Memory Git baseline has {dirty_count} dirty files.",
            {"dirty_count": dirty_count, "allowed_precommit": False},
        )
    return (
        "pass" if git_ok else "fail",
        "Memory Git baseline is clean.",
        {"dirty_count": 0, "allowed_precommit": allow_dirty_memory},
    )


def git_remote_backup_health(memory_pathspec: str, now: dt.datetime | None = None) -> tuple[bool, dict[str, Any]]:
    upstream_result = run(
        ["git", "-C", str(GIT_ROOT), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        15,
    )
    if not upstream_result["ok"]:
        return False, {"configured": False, "error": "upstream_missing"}
    upstream = str(upstream_result.get("stdout", "")).strip()
    divergence = run(
        ["git", "-C", str(GIT_ROOT), "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        30,
    )
    memory_ahead_result = run(
        ["git", "-C", str(GIT_ROOT), "rev-list", "--count", "@{upstream}..HEAD", "--", memory_pathspec],
        30,
    )
    try:
        behind, ahead_total = [int(value) for value in str(divergence.get("stdout", "")).split()]
        ahead_memory = int(str(memory_ahead_result.get("stdout", "")).strip())
    except (TypeError, ValueError):
        return False, {
            "configured": True,
            "upstream": upstream,
            "error": "git_divergence_unreadable",
        }
    oldest_age_days: float | None = None
    if ahead_memory:
        oldest_result = run(
            [
                "git",
                "-C",
                str(GIT_ROOT),
                "log",
                "--reverse",
                "--format=%ct",
                "@{upstream}..HEAD",
                "--",
                memory_pathspec,
            ],
            30,
        )
        timestamps = [line for line in str(oldest_result.get("stdout", "")).splitlines() if line]
        try:
            oldest = dt.datetime.fromtimestamp(int(timestamps[0]), tz=dt.timezone.utc)
        except (IndexError, TypeError, ValueError, OSError):
            return False, {
                "configured": True,
                "upstream": upstream,
                "ahead_total": ahead_total,
                "ahead_memory": ahead_memory,
                "behind": behind,
                "error": "oldest_unpushed_commit_unreadable",
            }
        current = now or dt.datetime.now(dt.timezone.utc)
        oldest_age_days = max(0.0, (current - oldest).total_seconds() / 86400)
    overdue = (
        behind > 0
        or ahead_memory >= REMOTE_BACKUP_MAX_UNPUSHED_COMMITS
        or (oldest_age_days is not None and oldest_age_days >= REMOTE_BACKUP_MAX_AGE_DAYS)
    )
    return not overdue, {
        "configured": True,
        "upstream": upstream,
        "ahead_total": ahead_total,
        "ahead_memory": ahead_memory,
        "behind": behind,
        "oldest_unpushed_age_days": round(oldest_age_days, 2) if oldest_age_days is not None else None,
        "warning_threshold_commits": REMOTE_BACKUP_MAX_UNPUSHED_COMMITS,
        "warning_threshold_days": REMOTE_BACKUP_MAX_AGE_DAYS,
    }


def session_claim_hygiene(
    conn: sqlite3.Connection,
    now: dt.datetime | None = None,
    max_age_hours: int = STALE_CLAIM_HOURS,
) -> tuple[bool, dict[str, Any]]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "memory_session_claims" not in tables:
        return False, {"active": 0, "stale": [], "error": "claim_table_missing"}
    rows = conn.execute(
        "SELECT actor, rel_path, updated_at FROM memory_session_claims WHERE status='active' ORDER BY actor, rel_path"
    ).fetchall()
    current = now or dt.datetime.now(dt.timezone.utc)
    stale: list[dict[str, Any]] = []
    for row in rows:
        updated_at = parse_time(str(row["updated_at"]))
        age_hours = (current - updated_at).total_seconds() / 3600 if updated_at else None
        if updated_at is None or age_hours is not None and age_hours >= max_age_hours:
            stale.append(
                {
                    "actor": str(row["actor"]),
                    "rel_path": str(row["rel_path"]),
                    "age_hours": round(max(0.0, age_hours), 1) if age_hours is not None else None,
                    "reason": "expired" if updated_at else "invalid_timestamp",
                }
            )
    return not stale, {"active": len(rows), "stale": stale, "stale_after_hours": max_age_hours}


def eligible_vector(row: sqlite3.Row) -> bool:
    path = Path(str(row["path"]))
    return (
        path.exists()
        and path.suffix.lower() == ".md"
        and path.name != "README.md"
        and not path.name.startswith("_模板")
        and str(row["memory_type"]) not in EXCLUDED_VECTOR_TYPES
        and str(row["status"]) not in EXCLUDED_VECTOR_STATUS
        and str(row["sensitivity"] or "").lower() not in {"secret", "credential"}
    )


def repair_derived() -> list[dict[str, Any]]:
    actions = []
    index_result = run([str(SCRIPT_ROOT / "agent_memory_index.py"), "--init", "--scan", "--report"], 180)
    actions.append({"action": "rebuild_sqlite_fts", "ok": index_result["ok"], "detail": index_result["detail"]})
    if index_result["ok"] and SEMANTIC_ENABLED:
        vector_result = run(
            [str(ZVEC_PYTHON), str(SCRIPT_ROOT / "agent_memory_zvec_index.py"), "--scan", "--prune", "--json"],
            900,
            offline_env() if REQUIRE_LOCAL_MODEL else None,
        )
        actions.append({"action": "rebuild_zvec", "ok": vector_result["ok"], "detail": vector_result["detail"]})
    return actions


def collect_checks(allow_dirty_memory: bool = False) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if PRELOAD_PATH_ISSUES:
        add(
            checks,
            "runtime_config_paths",
            "fail",
            "Runtime configuration paths are unsafe; configuration was not loaded.",
            {"unsafe_paths": PRELOAD_PATH_ISSUES, "config_root": str(CONFIG_ROOT)},
        )
        return checks
    required = list(RUNTIME_FILES)
    installed_runtime = os.path.normcase(os.path.abspath(LEXICAL_RUNTIME_ROOT)) == os.path.normcase(
        os.path.abspath(CONFIG_ROOT)
    )
    runtime_verification = verify_runtime_install(CONFIG_ROOT) if installed_runtime else None
    if runtime_verification is not None:
        runtime_closure = runtime_verification.get("closure", {})
        missing = sorted(
            set(runtime_verification.get("missing", []))
            | set(runtime_closure.get("core_missing", []))
        )
        runtime_files_detail = {
            "missing": missing,
            "unsafe_paths": runtime_verification.get("unsafe_paths", []),
        }
    else:
        missing = [name for name in required if not (SCRIPT_ROOT / name).is_file()]
        runtime_files_detail = {"missing": missing}
    runtime_files_ok = not missing and not runtime_files_detail.get("unsafe_paths")
    add(
        checks,
        "runtime_files",
        "pass" if runtime_files_ok else "fail",
        "Runtime files complete." if runtime_files_ok else "Runtime files missing or unsafe.",
        runtime_files_detail,
    )
    intent_enforcement = str(WRITE_INTENT_CONFIG.get("enforcement", "off")).strip().lower() or "off"
    enforcement_status = "fail" if intent_enforcement == "enforce" else ("warn" if intent_enforcement == "advisory" else "pass")
    add(
        checks,
        "write_intent_enforcement",
        enforcement_status,
        (
            "Enforcement is unavailable until an independent trusted approval verifier is configured."
            if enforcement_status == "fail"
            else "Write intents are self-attested audit records and cannot authorize actions."
        ),
        {
            "mode": intent_enforcement,
            "provenance_trust": "self_attested",
            "can_authorize_action": False,
            "reason_code": (
                "TRUSTED_APPROVAL_VERIFIER_REQUIRED" if intent_enforcement == "enforce" else ""
            ),
        },
    )
    if runtime_verification is not None:
        manifest_ok = bool(runtime_verification.get("ok"))
        add(
            checks,
            "runtime_manifest",
            "pass" if manifest_ok else "fail",
            "Installed runtime matches its manifest." if manifest_ok else "Installed runtime drifted from its manifest.",
            runtime_verification,
        )
        if runtime_verification.get("unsafe_paths"):
            return checks
    if not STATE_DB.exists() and not STATE_DB.is_symlink():
        add(checks, "state_db", "fail", "State database is missing.", {"path": str(STATE_DB)})
        return checks
    permission_detail = sqlite_permission_report(STATE_DB)
    permission_status = (
        "fail"
        if not permission_detail["ok"]
        else "pass"
        if permission_detail["mode_enforced"]
        else "warn"
    )
    add(
        checks,
        "state_db_permissions",
        permission_status,
        (
            "State database and SQLite sidecars are private (0600)."
            if permission_status == "pass"
            else "SQLite paths are regular and non-symlinked, but Windows ACL isolation was not verified."
            if permission_status == "warn"
            else "State database or SQLite sidecar permissions are unsafe."
        ),
        permission_detail,
    )
    unsafe_path_issue = any(
        item.get("reason") in {"missing", "symlink", "not_regular"}
        for item in permission_detail["issues"]
    )
    if unsafe_path_issue:
        return checks
    conn = secure_sqlite_connect(
        STATE_DB,
        create=False,
        repair_permissions=False,
        row_factory=sqlite3.Row,
        pragmas=("PRAGMA busy_timeout=10000",),
    )
    quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    add(checks, "sqlite_integrity", "pass" if quick == "ok" else "fail", f"SQLite quick_check={quick}.")
    actual = sorted(VAULT_ROOT.rglob("*.md"))
    actual_by_path = {str(path.resolve()): path for path in actual}
    actual_rel = {path.relative_to(VAULT_ROOT).as_posix() for path in actual}
    docs = conn.execute("SELECT path, rel_path, sha256, memory_type, status, sensitivity, verified_at_source, line_count, size_bytes FROM memory_docs").fetchall()
    db_by_path = {str(row["path"]): row for row in docs}
    missing_db = sorted(path.relative_to(VAULT_ROOT).as_posix() for raw, path in actual_by_path.items() if raw not in db_by_path)
    stale_db = sorted(str(row["rel_path"]) for raw, row in db_by_path.items() if raw not in actual_by_path)
    mismatch = sorted(str(row["rel_path"]) for raw, row in db_by_path.items() if raw in actual_by_path and text_sha256(actual_by_path[raw]) != str(row["sha256"]))
    add(checks, "markdown_sqlite_parity", "pass" if not (missing_db or stale_db or mismatch) else "fail", f"Markdown={len(actual)}, SQLite={len(docs)}.", {"missing": missing_db, "stale": stale_db, "hash_mismatch": mismatch})
    fts = {str(row[0]) for row in conn.execute("SELECT DISTINCT path FROM memory_fts")}
    add(checks, "sqlite_fts_parity", "pass" if fts == set(db_by_path) else "fail", f"FTS covers {len(fts)}/{len(docs)} docs.")
    index_path = VAULT_ROOT / "INDEX.md"
    refs = set(re.findall(r"`([^`]+\.md)`", index_path.read_text(encoding="utf-8", errors="replace"))) if index_path.exists() else set()
    add(checks, "index_navigation_parity", "pass" if refs == actual_rel else "warn", f"INDEX.md lists {len(refs)}/{len(actual_rel)} docs.", {"unlisted": sorted(actual_rel - refs), "broken": sorted(refs - actual_rel)})
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"memory_vector_chunks", "memory_vector_index_state"}.issubset(tables):
        eligible = {
            str(row["path"]): {"rel_path": str(row["rel_path"]), "sha256": str(row["sha256"])}
            for row in docs
            if eligible_vector(row)
        }
        states = conn.execute(
            "SELECT path, rel_path, doc_sha256, status, last_error FROM memory_vector_index_state"
        ).fetchall()
        if not states:
            add(checks, "zvec_parity", "warn", "Optional vector index is not initialized.")
        else:
            indexed = {str(row["path"]) for row in states if row["status"] == "indexed"}
            vector_missing = sorted(eligible[path]["rel_path"] for path in eligible.keys() - indexed)
            vector_stale = sorted(str(row["rel_path"] or row["path"]) for row in states if str(row["path"]) not in eligible)
            vector_hash_mismatch = sorted(
                eligible[str(row["path"])]["rel_path"]
                for row in states
                if str(row["path"]) in eligible
                and str(row["status"]) == "indexed"
                and str(row["doc_sha256"] or "") != eligible[str(row["path"])]["sha256"]
            )
            vector_errors = sorted(
                str(row["rel_path"] or row["path"])
                for row in states
                if str(row["status"]) == "error"
            )
            vector_ok = not (vector_missing or vector_stale or vector_hash_mismatch or vector_errors)
            add(
                checks,
                "zvec_parity",
                "pass" if vector_ok else "fail",
                f"Zvec covers {len(indexed & eligible.keys())}/{len(eligible)} docs.",
                {
                    "missing": vector_missing,
                    "stale": vector_stale,
                    "hash_mismatch": vector_hash_mismatch,
                    "errors": vector_errors,
                },
            )
    else:
        add(checks, "zvec_parity", "warn", "Optional vector index is not initialized.")
    if SEMANTIC_ENABLED:
        local_model_ok = (not REQUIRE_LOCAL_MODEL) or (EMBEDDING_MODEL.is_absolute() and EMBEDDING_MODEL.is_dir())
        add(
            checks,
            "semantic_local_model",
            "pass" if local_model_ok else "fail",
            "Semantic retrieval is pinned to a managed local model." if local_model_ok else "Semantic retrieval is not backed by the required local model directory.",
            {"model": str(EMBEDDING_MODEL), "require_local_model": REQUIRE_LOCAL_MODEL},
        )
        manifest_ok, manifest_detail = verify_model_manifest()
        add(
            checks,
            "semantic_model_integrity",
            "pass" if manifest_ok else "fail",
            "Managed model files match the pinned manifest." if manifest_ok else "Managed model files drifted from the pinned manifest.",
            manifest_detail,
        )
        python_ok, python_detail = verify_semantic_python_runtime()
        add(
            checks,
            "semantic_python_runtime",
            "pass" if python_ok else "fail",
            "Semantic Python and its base interpreter are available." if python_ok else "Semantic Python runtime is broken or lost its base interpreter.",
            python_detail,
        )
        dependency_ok, dependency_detail = verify_dependency_lock()
        add(
            checks,
            "semantic_dependency_lock",
            "pass" if dependency_ok else "fail",
            "Semantic Python environment matches the exact dependency lock." if dependency_ok else "Semantic Python environment differs from the dependency lock.",
            dependency_detail,
        )
        probe_ok, probe_detail = offline_semantic_probe()
        add(
            checks,
            "semantic_offline_probe",
            "pass" if probe_ok else "fail",
            "Offline EmbeddingGemma + Zvec query succeeded." if probe_ok else "Offline EmbeddingGemma + Zvec query failed.",
            probe_detail,
        )
    source_counts = {str(row[0]): int(row[1]) for row in conn.execute("SELECT verified_at_source, COUNT(*) FROM memory_docs GROUP BY verified_at_source")}
    weak = source_counts.get("mtime_fallback", 0) + source_counts.get("needs_review", 0)
    add(
        checks,
        "verification_provenance",
        "warn" if weak else "pass",
        f"Explicit verification/provenance classification on {len(docs) - weak}/{len(docs)} docs.",
        {"by_source": source_counts, "needs_review": weak},
    )
    large = [
        {"rel_path": str(row["rel_path"]), "lines": int(row["line_count"]), "bytes": int(row["size_bytes"])}
        for row in docs
        if str(row["status"]) in {"active", "candidate"}
        and (int(row["line_count"]) > 180 or int(row["size_bytes"]) > 24576)
    ]
    add(checks, "large_memory_files", "warn" if large else "pass", f"{len(large)} docs exceed compaction advisory thresholds.", {"files": large})
    search_privacy_ok, search_privacy_detail = search_log_privacy_health(conn)
    raw_logs = int(search_privacy_detail["legacy_raw_rows"])
    add(
        checks,
        "search_log_privacy",
        "pass" if search_privacy_ok else "warn",
        f"{raw_logs} legacy raw query/path rows remain.",
        search_privacy_detail,
    )
    claims_ok, claims_detail = session_claim_hygiene(conn)
    stale_claim_count = len(claims_detail.get("stale", []))
    add(
        checks,
        "session_claim_hygiene",
        "pass" if claims_ok else "warn",
        f"Active claims={claims_detail.get('active', 0)}, stale claims={stale_claim_count}.",
        claims_detail,
    )
    conn.close()
    latest_audit = latest_jsonl(AUDIT_LOG, lambda item: item.get("status") == "ran" and item.get("ok"))
    audit_time = parse_time(str(latest_audit.get("time", ""))) if latest_audit else None
    age = (dt.datetime.now(dt.timezone.utc) - audit_time).days if audit_time else None
    add(checks, "audit_freshness", "pass" if age is not None and age <= 7 else "warn", f"Last successful audit age: {age} days." if age is not None else "No successful audit recorded.")
    closeout = latest_jsonl(CLOSEOUT_LOG)
    add(checks, "closeout_history", "pass" if closeout and closeout.get("status") in {"ok", "warning"} else "warn", f"Latest closeout status: {closeout.get('status')}." if closeout else "No closeout history.")
    observation_ok, observation_detail = closeout_observation_health()
    pending_observations = int(observation_detail.get("pending_count", 0) or 0)
    if observation_ok:
        observation_message = "Closeout observation baseline covers current formal memory history."
    elif not observation_detail.get("baseline"):
        observation_message = "No completed closeout observation baseline is recorded."
    elif observation_detail.get("warnings") or observation_detail.get("error"):
        observation_message = "Closeout observation baseline could not be verified."
    else:
        observation_message = (
            f"{pending_observations} formal memory paths still lack closeout completion observations."
        )
    add(
        checks,
        "closeout_observation_baseline",
        "pass" if observation_ok else "warn",
        observation_message,
        observation_detail,
    )

    remote_has_credential = git_remote_has_credential()
    add(
        checks,
        "git_remote_credentials",
        "fail" if remote_has_credential else "pass",
        "Git remote contains an embedded credential." if remote_has_credential else "Git remote has no embedded credential.",
    )
    try:
        memory_pathspec = VAULT_ROOT.relative_to(GIT_ROOT).as_posix()
    except ValueError:
        memory_pathspec = str(VAULT_ROOT)
    git_status = run(
        ["git", "-C", str(GIT_ROOT), "-c", "core.quotepath=false", "status", "--porcelain=v1", "--", memory_pathspec],
        30,
    )
    dirty_lines = [line for line in str(git_status.get("stdout", "")).splitlines() if line]
    dirty_status, dirty_message, dirty_detail = memory_git_baseline_result(
        len(dirty_lines), bool(git_status["ok"]), allow_dirty_memory
    )
    add(
        checks,
        "memory_git_baseline",
        dirty_status,
        dirty_message,
        dirty_detail,
    )
    backup_ok, backup_detail = git_remote_backup_health(memory_pathspec)
    unpushed_memory = int(backup_detail.get("ahead_memory", 0) or 0)
    add(
        checks,
        "memory_remote_backup",
        "pass" if backup_ok else "warn",
        (
            "Memory Git history is backed up to its upstream."
            if backup_ok and unpushed_memory == 0
            else (
                f"{unpushed_memory} unpushed memory commits remain within the backup grace window."
                if backup_ok
                else "Memory Git history has no healthy recent upstream backup."
            )
        ),
        backup_detail,
    )

    if HOST_CONFIG:
        codex_hooks_path = configured_path("codex_hooks_json")
        if codex_hooks_path:
            codex_hooks = read_json_object(codex_hooks_path)
            codex_ok = "on-stop-memory.sh" in json.dumps(codex_hooks, ensure_ascii=False)
            add(checks, "codex_stop_hook", "pass" if codex_ok else "warn", "Codex Stop hook is configured." if codex_ok else "Codex Stop hook is missing or invalid.")

        claude_settings_path = configured_path("claude_settings_json")
        claude_fragment_path = configured_path("claude_hooks_fragment")
        claude_settings = read_json_object(claude_settings_path) if claude_settings_path else {}
        expected_hooks = read_json_object(claude_fragment_path) if claude_fragment_path else {}
        if claude_settings_path or claude_fragment_path:
            claude_ok = bool(expected_hooks) and claude_settings.get("hooks") == expected_hooks
            add(checks, "claude_stop_hook", "pass" if claude_ok else "warn", "Claude Stop/SessionEnd hooks are configured." if claude_ok else "Claude hooks differ from the managed fragment.")
            live_hooks = claude_settings.get("hooks") if isinstance(claude_settings.get("hooks"), dict) else {}
            live_semantics_ok, live_semantics_detail = claude_compatible_hook_semantics(
                live_hooks,
                "claude",
            )
            fragment_semantics_ok, fragment_semantics_detail = claude_compatible_hook_semantics(
                expected_hooks,
                "claude",
            )
            semantics_ok = live_semantics_ok and fragment_semantics_ok
            add(
                checks,
                "claude_hook_semantics",
                "pass" if semantics_ok else "warn",
                (
                    "Claude Stop is scoped and SessionEnd is non-blocking."
                    if semantics_ok
                    else "Claude managed hooks have unsafe Stop/SessionEnd lifecycle semantics."
                ),
                {"live": live_semantics_detail, "managed_fragment": fragment_semantics_detail},
            )

        codebuddy_settings_path = configured_path("codebuddy_settings_json")
        codebuddy_fragment_path = configured_path("codebuddy_hooks_fragment")
        codebuddy_settings = read_json_object(codebuddy_settings_path) if codebuddy_settings_path else {}
        codebuddy_expected = read_json_object(codebuddy_fragment_path) if codebuddy_fragment_path else {}
        if codebuddy_settings_path or codebuddy_fragment_path:
            hooks_blob = json.dumps(codebuddy_settings.get("hooks", {}), ensure_ascii=False)
            fragment_ok = bool(codebuddy_expected) and codebuddy_settings.get("hooks") == codebuddy_expected
            mention_ok = (
                "agent_memory_stop_hook.py" in hooks_blob
                and "--actor codebuddy" in hooks_blob
            )
            codebuddy_ok = fragment_ok or mention_ok
            add(
                checks,
                "codebuddy_stop_hook",
                "pass" if codebuddy_ok else "warn",
                "CodeBuddy Stop/SessionEnd hooks are configured."
                if codebuddy_ok
                else "CodeBuddy hooks differ from the managed fragment or lack stop_hook --actor codebuddy.",
            )
            codebuddy_live_hooks = (
                codebuddy_settings.get("hooks")
                if isinstance(codebuddy_settings.get("hooks"), dict)
                else {}
            )
            live_semantics_ok, live_semantics_detail = claude_compatible_hook_semantics(
                codebuddy_live_hooks,
                "codebuddy",
            )
            fragment_semantics_ok, fragment_semantics_detail = claude_compatible_hook_semantics(
                codebuddy_expected,
                "codebuddy",
            )
            semantics_ok = live_semantics_ok and (
                fragment_semantics_ok if codebuddy_fragment_path else True
            )
            add(
                checks,
                "codebuddy_hook_semantics",
                "pass" if semantics_ok else "warn",
                (
                    "CodeBuddy uses scoped Claude-compatible lifecycle hooks."
                    if semantics_ok
                    else "CodeBuddy hooks have unsafe Stop/SessionEnd lifecycle semantics."
                ),
                {"live": live_semantics_detail, "managed_fragment": fragment_semantics_detail},
            )

        cc_switch_path = configured_path("cc_switch_db")
        if cc_switch_path:
            cc_ok, cc_detail = cc_switch_hooks_match(cc_switch_path, expected_hooks)
            add(checks, "claude_hook_persistence", "pass" if cc_ok else "warn", "Claude hook persistence is healthy." if cc_ok else "A provider manager may overwrite Claude hooks.", cc_detail)

        env_payload = claude_settings.get("env") if isinstance(claude_settings, dict) else {}
        base_url = str(env_payload.get("ANTHROPIC_BASE_URL", "")) if isinstance(env_payload, dict) else ""
        if claude_settings_path:
            endpoint_ok, endpoint_detail = local_endpoint_reachable(base_url)
            add(checks, "claude_runtime_endpoint", "pass" if endpoint_ok else "warn", "Claude runtime endpoint is reachable or remote." if endpoint_ok else "Claude points to a local endpoint that is not listening.", endpoint_detail)

        launch_path = configured_path("audit_launchagent")
        launch_label = HOST_CONFIG.get("audit_launchagent_label")
        if launch_path and isinstance(launch_label, str) and launch_label:
            launch_loaded = run(["launchctl", "print", f"gui/{Path.home().stat().st_uid}/{launch_label}"], 15)["ok"]
            launch_ok = launch_path.exists() and launch_loaded
            add(checks, "audit_launchagent", "pass" if launch_ok else "warn", "Weekly audit LaunchAgent is loaded." if launch_ok else "Weekly audit LaunchAgent is missing or unloaded.", {"plist_exists": launch_path.exists(), "loaded": launch_loaded})
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only health report for the complete Agent Memory pipeline.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repair-derived", action="store_true", help="Rebuild derived indexes without editing Markdown facts.")
    parser.add_argument(
        "--allow-dirty-memory",
        action="store_true",
        help="Treat the current pre-commit memory changes as expected; intended only for closeout piggyback checks.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repairs = repair_derived() if args.repair_derived else []
    checks = collect_checks(allow_dirty_memory=args.allow_dirty_memory)
    statuses = {str(item["status"]) for item in checks}
    status = "error" if "fail" in statuses else ("warning" if "warn" in statuses else "ok")
    payload = {"time": utc_now(), "version": VERSION, "status": status, "summary": {name: sum(1 for item in checks if item["status"] == name) for name in ("pass", "warn", "fail")}, "checks": checks, "repair_actions": repairs}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"agent_memory_doctor={status} version={VERSION}")
        for item in checks:
            print(f"[{item['status']}] {item['name']}: {item['message']}")
    return 2 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
