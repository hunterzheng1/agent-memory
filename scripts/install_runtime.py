#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from agent_memory_state import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    PERMISSION_MODEL,
    POSIX_MODE_ENFORCED,
    StateSecurityError,
    absolute_path,
    ensure_private_directory,
    harden_private_file,
    harden_sqlite_files,
    sqlite_permission_report,
)


SOURCE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SOURCE_ROOT.parent
CORE_FILES = (
    "agent_memory_audit.py",
    "agent_memory_audit_autorun.py",
    "agent_memory_claim.py",
    "agent_memory_check.py",
    "agent_memory_closeout.py",
    "agent_memory_doctor.py",
    "agent_memory_decision_outcomes.py",
    "agent_memory_env.py",
    "agent_memory_evolution.py",
    "agent_memory_host.py",
    "agent_memory_index.py",
    "agent_memory_intent.py",
    "agent_memory_lock.py",
    "agent_memory_paths.py",
    "agent_memory_policy_benchmark.py",
    "agent_memory_retrieval_benchmark.py",
    "agent_memory_search.py",
    "agent_memory_safety.py",
    "agent_memory_session_hook.py",
    "agent_memory_state.py",
    "agent_memory_stop_hook.py",
    "agent_memory_zvec_index.py",
    "bootstrap.py",
    "install_runtime.py",
    "memoryctl",
)
SUPPORT_FILES = (
    "requirements-vector.lock",
    "benchmarks/public-sample.json",
    "benchmarks/public-policy-reconcile.json",
    "benchmarks/public-policy-safety.json",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_value(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def expected_manifest(config_root: Path) -> dict[str, Any]:
    hashes = {name: sha256(SOURCE_ROOT / name) for name in CORE_FILES}
    support_hashes = {name: sha256(REPO_ROOT / name) for name in SUPPORT_FILES}
    return {
        "schema_version": 1,
        "installed_at": utc_now(),
        "source_repo": str(REPO_ROOT),
        "source_commit": git_value("rev-parse", "HEAD") or "archive",
        "source_dirty": bool(git_value("status", "--porcelain")),
        "runtime_root": str(config_root),
        "files": hashes,
        "support_files": support_hashes,
    }


def harden_runtime_permissions(config_root: Path) -> None:
    ensure_private_directory(config_root, harden_existing=True)
    for private_name in ("config", "logs", "reports", "proposals", "benchmarks"):
        private_dir = config_root / private_name
        if not (private_dir.exists() or private_dir.is_symlink()):
            continue
        ensure_private_directory(private_dir, harden_existing=True)
        for path in private_dir.rglob("*"):
            if path.is_symlink():
                raise StateSecurityError(f"runtime private path must not be a symlink: {path}")
            if path.is_dir():
                ensure_private_directory(path, harden_existing=True)
            elif path.is_file():
                harden_private_file(path)
            else:
                raise StateSecurityError(f"runtime private path is not regular: {path}")
    harden_sqlite_files(config_root / "state.sqlite", require_database=False)


def runtime_permission_report(config_root: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    try:
        metadata = config_root.lstat()
    except FileNotFoundError:
        issues.append({"path": str(config_root), "reason": "missing_config_root"})
    else:
        if stat.S_ISLNK(metadata.st_mode):
            issues.append({"path": str(config_root), "reason": "symlink"})
        elif not stat.S_ISDIR(metadata.st_mode):
            issues.append({"path": str(config_root), "reason": "not_directory"})
        else:
            actual_mode = stat.S_IMODE(metadata.st_mode)
            if POSIX_MODE_ENFORCED and actual_mode != PRIVATE_DIRECTORY_MODE:
                issues.append(
                    {
                        "path": str(config_root),
                        "reason": "mode",
                        "expected_mode": "0700",
                        "actual_mode": f"{actual_mode:04o}",
                    }
                )

    config_dir = config_root / "config"
    if config_dir.is_symlink():
        issues.append({"path": str(config_dir), "reason": "symlink"})
    elif config_dir.is_dir():
        for path in config_dir.rglob("*"):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                issues.append({"path": str(path), "reason": "symlink"})
            elif stat.S_ISREG(metadata.st_mode):
                actual_mode = stat.S_IMODE(metadata.st_mode)
                if POSIX_MODE_ENFORCED and actual_mode != PRIVATE_FILE_MODE:
                    issues.append(
                        {
                            "path": str(path),
                            "reason": "mode",
                            "expected_mode": "0600",
                            "actual_mode": f"{actual_mode:04o}",
                        }
                    )

    sqlite_report = sqlite_permission_report(config_root / "state.sqlite", require_database=False)
    issues.extend(sqlite_report["issues"])
    return {
        "ok": not issues,
        "permission_model": PERMISSION_MODEL,
        "mode_enforced": POSIX_MODE_ENFORCED,
        "warnings": [] if POSIX_MODE_ENFORCED else ["windows_acl_unverified"],
        "config_root": str(config_root),
        "issues": issues,
        "state": sqlite_report,
    }


def verify(config_root: Path) -> dict[str, Any]:
    manifest_path = config_root / "config" / "runtime-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "manifest": str(manifest_path), "missing_manifest": True}
    expected = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(expected, dict):
        return {"ok": False, "manifest": str(manifest_path), "invalid_manifest": True}
    missing: list[str] = []
    mismatched: list[str] = []
    for name, digest in expected.items():
        path = config_root / "scripts" / str(name)
        if not path.is_file():
            missing.append(str(name))
        elif sha256(path) != str(digest):
            mismatched.append(str(name))
    support_missing: list[str] = []
    support_mismatched: list[str] = []
    support_expected = manifest.get("support_files", {}) if isinstance(manifest, dict) else {}
    if isinstance(support_expected, dict):
        for name, digest in support_expected.items():
            path = config_root / str(name)
            if not path.is_file():
                support_missing.append(str(name))
            elif sha256(path) != str(digest):
                support_mismatched.append(str(name))
    permissions = runtime_permission_report(config_root)
    return {
        "ok": not missing and not mismatched and not support_missing and not support_mismatched and permissions["ok"],
        "manifest": str(manifest_path),
        "source_commit": manifest.get("source_commit", ""),
        "source_dirty": bool(manifest.get("source_dirty")),
        "checked_files": len(expected),
        "missing": missing,
        "mismatched": mismatched,
        "support_missing": support_missing,
        "support_mismatched": support_mismatched,
        "permissions": permissions,
    }


def install(config_root: Path, dry_run: bool) -> dict[str, Any]:
    script_root = config_root / "scripts"
    config_dir = config_root / "config"
    manifest = expected_manifest(config_root)
    changed: list[str] = []
    unchanged: list[str] = []
    if not dry_run:
        ensure_private_directory(config_root, harden_existing=True)
    for name in CORE_FILES:
        source = SOURCE_ROOT / name
        target = script_root / name
        if target.is_file() and sha256(target) == manifest["files"][name]:
            unchanged.append(name)
            if not dry_run:
                target.chmod(source.stat().st_mode & 0o777)
            continue
        changed.append(name)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            target.chmod(source.stat().st_mode & 0o777)
    for name in SUPPORT_FILES:
        source = REPO_ROOT / name
        target = config_root / name
        if target.is_file() and sha256(target) == manifest["support_files"][name]:
            unchanged.append(name)
            continue
        changed.append(name)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    if not dry_run:
        ensure_private_directory(config_dir, harden_existing=True)
        manifest_path = config_dir / "runtime-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        harden_runtime_permissions(config_root)
    permissions = (
        runtime_permission_report(config_root)
        if not dry_run
        else {
            "ok": True,
            "permission_model": PERMISSION_MODEL,
            "mode_enforced": POSIX_MODE_ENFORCED,
            "warnings": [] if POSIX_MODE_ENFORCED else ["windows_acl_unverified"],
            "not_checked": True,
        }
    )
    return {
        "ok": bool(permissions["ok"]),
        "dry_run": dry_run,
        "config_root": str(config_root),
        "changed": changed,
        "unchanged": unchanged,
        "source_commit": manifest["source_commit"],
        "source_dirty": manifest["source_dirty"],
        "permissions": permissions,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install or verify the canonical Agent Memory runtime.")
    parser.add_argument("--config-root", default="~/.config/agent-memory")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_root = absolute_path(args.config_root)
    try:
        payload = verify(config_root) if args.verify else install(config_root, args.dry_run)
    except (OSError, StateSecurityError) as exc:
        payload = {"ok": False, "config_root": str(config_root), "error": type(exc).__name__, "detail": str(exc)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"runtime={'ok' if payload.get('ok') else 'error'} root={config_root}")
        for key in ("changed", "missing", "mismatched"):
            if payload.get(key):
                print(f"{key}={','.join(payload[key])}")
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
