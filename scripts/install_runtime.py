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
TEMPLATE_ROOT = REPO_ROOT / "templates" / "vault"
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
    "audit-task.ps1",
    "bootstrap.py",
    "install-codex-hook.ps1",
    "install_runtime.py",
    "install-windows.ps1",
    "memoryctl",
    "stop-hook.ps1",
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


def template_hashes() -> dict[str, str]:
    if not TEMPLATE_ROOT.is_dir():
        raise FileNotFoundError(f"template root is missing: {TEMPLATE_ROOT}")
    hashes = {
        path.relative_to(REPO_ROOT).as_posix(): sha256(path)
        for path in sorted(TEMPLATE_ROOT.rglob("*"))
        if path.is_file()
    }
    required_ignore = "templates/vault/.gitignore"
    if required_ignore not in hashes:
        raise FileNotFoundError(f"required vault template is missing: {required_ignore}")
    return hashes


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
    installed_templates = template_hashes()
    return {
        "schema_version": 1,
        "installed_at": utc_now(),
        "source_repo": str(REPO_ROOT),
        "source_commit": git_value("rev-parse", "HEAD") or "archive",
        "source_dirty": bool(git_value("status", "--porcelain")),
        "runtime_root": str(config_root),
        "files": hashes,
        "support_files": support_hashes,
        "template_files": installed_templates,
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
    if manifest_path.is_symlink():
        return {"ok": False, "manifest": str(manifest_path), "unsafe_manifest": "symlink"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "manifest": str(manifest_path), "missing_manifest": True}
    expected = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(expected, dict):
        return {"ok": False, "manifest": str(manifest_path), "invalid_manifest": True}
    core_closure_missing = sorted(set(CORE_FILES) - set(expected))
    core_closure_unexpected = sorted(set(expected) - set(CORE_FILES))
    missing: list[str] = []
    mismatched: list[str] = []
    symlinked: list[str] = []
    for name, digest in expected.items():
        normalized = str(name)
        if normalized not in CORE_FILES:
            continue
        path = config_root / "scripts" / normalized
        if path.is_symlink():
            symlinked.append(normalized)
        elif not path.is_file():
            missing.append(normalized)
        elif sha256(path) != str(digest):
            mismatched.append(normalized)
    support_missing: list[str] = []
    support_mismatched: list[str] = []
    support_expected = manifest.get("support_files") if isinstance(manifest, dict) else None
    support_closure_missing = sorted(
        set(SUPPORT_FILES) - set(support_expected if isinstance(support_expected, dict) else {})
    )
    support_closure_unexpected = sorted(
        set(support_expected if isinstance(support_expected, dict) else {}) - set(SUPPORT_FILES)
    )
    if isinstance(support_expected, dict):
        for name, digest in support_expected.items():
            normalized = str(name)
            if normalized not in SUPPORT_FILES:
                continue
            path = config_root / normalized
            if path.is_symlink():
                symlinked.append(normalized)
            elif not path.is_file():
                support_missing.append(normalized)
            elif sha256(path) != str(digest):
                support_mismatched.append(normalized)
    template_missing: list[str] = []
    template_mismatched: list[str] = []
    template_unsafe_names: list[str] = []
    template_expected = manifest.get("template_files") if isinstance(manifest, dict) else None
    required_template_missing = (
        []
        if isinstance(template_expected, dict) and "templates/vault/.gitignore" in template_expected
        else ["templates/vault/.gitignore"]
    )
    if isinstance(template_expected, dict):
        for name, digest in template_expected.items():
            normalized = str(name)
            parts = normalized.split("/")
            if (
                "\\" in normalized
                or parts[:2] != ["templates", "vault"]
                or any(part in {"", ".", ".."} for part in parts)
            ):
                template_unsafe_names.append(normalized)
                continue
            path = config_root / normalized
            if path.is_symlink():
                symlinked.append(normalized)
            elif not path.is_file():
                template_missing.append(normalized)
            elif sha256(path) != str(digest):
                template_mismatched.append(normalized)
    permissions = runtime_permission_report(config_root)
    return {
        "ok": (
            not missing
            and not mismatched
            and not support_missing
            and not support_mismatched
            and not template_missing
            and not template_mismatched
            and not symlinked
            and not template_unsafe_names
            and not core_closure_missing
            and not core_closure_unexpected
            and isinstance(support_expected, dict)
            and not support_closure_missing
            and not support_closure_unexpected
            and isinstance(template_expected, dict)
            and not required_template_missing
            and permissions["ok"]
        ),
        "manifest": str(manifest_path),
        "source_commit": manifest.get("source_commit", ""),
        "source_dirty": bool(manifest.get("source_dirty")),
        "checked_files": len(expected),
        "missing": missing,
        "mismatched": mismatched,
        "support_missing": support_missing,
        "support_mismatched": support_mismatched,
        "template_missing": template_missing,
        "template_mismatched": template_mismatched,
        "symlinked": symlinked,
        "closure": {
            "core_missing": core_closure_missing,
            "core_unexpected": core_closure_unexpected,
            "support_missing": support_closure_missing,
            "support_unexpected": support_closure_unexpected,
            "required_template_missing": required_template_missing,
            "template_unsafe_names": template_unsafe_names,
        },
        "permissions": permissions,
    }


def assert_managed_target(config_root: Path, target: Path) -> None:
    """Reject managed paths that traverse a symlink or non-directory parent."""

    try:
        relative = target.relative_to(config_root)
    except ValueError as exc:
        raise StateSecurityError(f"managed runtime target escapes config root: {target}") from exc
    cursor = config_root
    if cursor.is_symlink():
        raise StateSecurityError(f"managed runtime root must not be a symlink: {cursor}")
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise StateSecurityError(f"managed runtime parent must not be a symlink: {cursor}")
        if cursor.exists() and not cursor.is_dir():
            raise StateSecurityError(f"managed runtime parent is not a directory: {cursor}")
    if target.is_symlink():
        raise StateSecurityError(f"managed runtime file must not be a symlink: {target}")
    if target.exists() and not target.is_file():
        raise StateSecurityError(f"managed runtime target is not a regular file: {target}")


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
        assert_managed_target(config_root, target)
        if target.is_file() and sha256(target) == manifest["files"][name]:
            unchanged.append(name)
            if not dry_run and POSIX_MODE_ENFORCED:
                target.chmod(source.stat().st_mode & 0o777)
            continue
        changed.append(name)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if POSIX_MODE_ENFORCED:
                target.chmod(source.stat().st_mode & 0o777)
    for name in SUPPORT_FILES:
        source = REPO_ROOT / name
        target = config_root / name
        assert_managed_target(config_root, target)
        if target.is_file() and sha256(target) == manifest["support_files"][name]:
            unchanged.append(name)
            continue
        changed.append(name)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name, digest in manifest["template_files"].items():
        source = REPO_ROOT / name
        target = config_root / name
        assert_managed_target(config_root, target)
        if target.is_file() and sha256(target) == digest:
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
