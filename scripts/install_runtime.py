#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterator

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
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
INTEGRITY_MODEL = "sha256_drift_detection_not_authentication"


class RuntimeRollbackError(StateSecurityError):
    """A failed publish has recoverable backups that must not be deleted."""

    def __init__(self, recovery_path: Path, errors: list[str]) -> None:
        self.recovery_path = recovery_path
        super().__init__(
            "runtime install failed and rollback was incomplete; "
            f"recovery_path={recovery_path}; errors=" + "; ".join(errors)
        )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _absolute_components(path: Path) -> list[Path]:
    absolute = absolute_path(path)
    anchor = Path(absolute.anchor)
    components = [anchor]
    cursor = anchor
    for part in absolute.parts[1:]:
        cursor = cursor / part
        components.append(cursor)
    return components


def _lexically_contains(root: Path, target: Path) -> bool:
    normalized_root = os.path.normcase(os.path.normpath(os.fspath(absolute_path(root))))
    normalized_target = os.path.normcase(os.path.normpath(os.fspath(absolute_path(target))))
    try:
        return os.path.commonpath((normalized_root, normalized_target)) == normalized_root
    except ValueError:
        return False


def managed_path_issue(
    config_root: Path,
    target: Path,
    *,
    expected_kind: str = "file",
    allow_missing: bool = True,
) -> dict[str, Any] | None:
    """Inspect every lexical path component without following reparse points."""

    root = absolute_path(config_root)
    path = absolute_path(target)
    if not _lexically_contains(root, path):
        return {"path": str(path), "reason": "outside_config_root"}
    components = _absolute_components(path)
    for index, component in enumerate(components):
        final = index == len(components) - 1
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            if not allow_missing:
                return {"path": str(path), "reason": "missing"}
            return None
        except OSError as exc:
            return {
                "path": str(component),
                "reason": "metadata_error",
                "detail": str(exc),
            }
        if _is_reparse(metadata):
            return {"path": str(component), "reason": "reparse_point"}
        if not final and not stat.S_ISDIR(metadata.st_mode):
            return {"path": str(component), "reason": "parent_not_directory"}
        if final and expected_kind == "file" and not stat.S_ISREG(metadata.st_mode):
            return {"path": str(component), "reason": "target_not_regular_file"}
        if final and expected_kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
            return {"path": str(component), "reason": "target_not_directory"}
    return None


def _scan_regular_tree(root: Path) -> tuple[list[Path], list[Path], list[dict[str, Any]]]:
    """Return regular files/directories without traversing a reparse point."""

    files: list[Path] = []
    directories: list[Path] = []
    issues: list[dict[str, Any]] = []
    try:
        root_metadata = os.lstat(root)
    except FileNotFoundError:
        return files, directories, issues
    except OSError as exc:
        return files, directories, [
            {"path": str(root), "reason": "metadata_error", "detail": str(exc)}
        ]
    if _is_reparse(root_metadata):
        return files, directories, [{"path": str(root), "reason": "reparse_point"}]
    if not stat.S_ISDIR(root_metadata.st_mode):
        return files, directories, [{"path": str(root), "reason": "target_not_directory"}]

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            issues.append(
                {"path": str(directory), "reason": "metadata_error", "detail": str(exc)}
            )
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                issues.append(
                    {"path": str(path), "reason": "metadata_error", "detail": str(exc)}
                )
                continue
            if _is_reparse(metadata):
                issues.append({"path": str(path), "reason": "reparse_point"})
            elif stat.S_ISDIR(metadata.st_mode):
                directories.append(path)
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(path)
            else:
                issues.append({"path": str(path), "reason": "not_regular"})
    return sorted(files), sorted(directories), issues


def scan_template_inventory(config_root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    template_root = absolute_path(config_root) / "templates" / "vault"
    issue = managed_path_issue(
        absolute_path(config_root),
        template_root,
        expected_kind="directory",
    )
    if issue is not None:
        return [], [issue]
    files, _directories, issues = _scan_regular_tree(template_root)
    inventory = [path.relative_to(absolute_path(config_root)).as_posix() for path in files]
    return sorted(inventory), issues


def template_hashes() -> dict[str, str]:
    inventory, issues = scan_template_inventory(REPO_ROOT)
    if issues:
        first = issues[0]
        raise StateSecurityError(
            f"template source must not contain a symlink or reparse point: {first['path']}"
        )
    if not inventory:
        raise FileNotFoundError(f"template root is missing or empty: {TEMPLATE_ROOT}")
    hashes = {name: sha256(REPO_ROOT / name) for name in inventory}
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
    template_inventory = sorted(installed_templates)
    return {
        "schema_version": 2,
        "installed_at": utc_now(),
        "source_repo": str(REPO_ROOT),
        "source_commit": git_value("rev-parse", "HEAD") or "archive",
        "source_dirty": bool(git_value("status", "--porcelain")),
        "runtime_root": str(config_root),
        "files": hashes,
        "support_files": support_hashes,
        "template_files": installed_templates,
        "template_inventory": template_inventory,
        "template_count": len(template_inventory),
        "integrity_model": INTEGRITY_MODEL,
    }


def harden_runtime_permissions(config_root: Path) -> None:
    assert_managed_target(config_root, config_root, expected_kind="directory", allow_missing=False)
    ensure_private_directory(config_root, harden_existing=True)
    for private_name in ("config", "logs", "reports", "proposals", "benchmarks"):
        private_dir = config_root / private_name
        assert_managed_target(config_root, private_dir, expected_kind="directory")
        try:
            os.lstat(private_dir)
        except FileNotFoundError:
            continue
        ensure_private_directory(private_dir, harden_existing=True)
        files, directories, issues = _scan_regular_tree(private_dir)
        if issues:
            first = issues[0]
            raise StateSecurityError(
                "runtime private path must not be a symlink or reparse point "
                f"and must be regular: {first['path']} ({first['reason']})"
            )
        for path in directories:
            ensure_private_directory(path, harden_existing=True)
        for path in files:
            harden_private_file(path)
    harden_sqlite_files(config_root / "state.sqlite", require_database=False)


def runtime_permission_report(config_root: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    root_issue = managed_path_issue(
        config_root,
        config_root,
        expected_kind="directory",
        allow_missing=False,
    )
    if root_issue is not None:
        if root_issue["reason"] == "missing":
            root_issue = {**root_issue, "reason": "missing_config_root"}
        issues.append(root_issue)
    else:
        metadata = os.lstat(config_root)
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
    if root_issue is None:
        config_issue = managed_path_issue(
            config_root,
            config_dir,
            expected_kind="directory",
        )
        if config_issue is not None:
            issues.append(config_issue)
        else:
            files, _directories, tree_issues = _scan_regular_tree(config_dir)
            issues.extend(tree_issues)
            for path in files:
                metadata = os.lstat(path)
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

    sqlite_report = (
        sqlite_permission_report(config_root / "state.sqlite", require_database=False)
        if root_issue is None
        else {
            "ok": False,
            "permission_model": PERMISSION_MODEL,
            "mode_enforced": POSIX_MODE_ENFORCED,
            "warnings": [] if POSIX_MODE_ENFORCED else ["windows_acl_unverified"],
            "database": str(config_root / "state.sqlite"),
            "checked": [],
            "issues": [],
        }
    )
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


WINDOWS_RESERVED_SEGMENTS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_NAME_CHARACTERS = frozenset('<>:"|?*\\')


def _manifest_segment_is_safe(segment: str) -> bool:
    if segment in {"", ".", ".."} or segment.endswith((".", " ")):
        return False
    if any(
        unicodedata.category(character).startswith("C")
        or character in WINDOWS_FORBIDDEN_NAME_CHARACTERS
        for character in segment
    ):
        return False
    device_stem = segment.split(".", 1)[0].upper()
    return device_stem not in WINDOWS_RESERVED_SEGMENTS


def _manifest_name_is_safe(name: object) -> bool:
    if not isinstance(name, str) or name.startswith("/"):
        return False
    parts = name.split("/")
    return bool(parts) and all(_manifest_segment_is_safe(part) for part in parts)


def _template_name_is_safe(name: object) -> bool:
    if not _manifest_name_is_safe(name):
        return False
    assert isinstance(name, str)
    parts = name.split("/")
    return len(parts) >= 3 and parts[:2] == ["templates", "vault"]


def _record_issue(issues: list[dict[str, Any]], issue: dict[str, Any] | None) -> None:
    if issue is None:
        return
    identity = (issue.get("path"), issue.get("reason"))
    if all((item.get("path"), item.get("reason")) != identity for item in issues):
        issues.append(issue)


def _reparse_paths(issues: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(issue.get("path", ""))
            for issue in issues
            if issue.get("reason") == "reparse_point" and issue.get("path")
        }
    )


def _managed_runtime_targets(config_root: Path) -> list[tuple[Path, str]]:
    root = absolute_path(config_root)
    targets: list[tuple[Path, str]] = [
        (root, "directory"),
        (root / "scripts", "directory"),
        (root / "config", "directory"),
        (root / "templates", "directory"),
        (root / "templates" / "vault", "directory"),
        (root / "config" / "runtime-manifest.json", "file"),
    ]
    targets.extend((root / "scripts" / name, "file") for name in CORE_FILES)
    targets.extend((root / name, "file") for name in SUPPORT_FILES)
    return targets


def _unsafe_runtime_paths(config_root: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for target, expected_kind in _managed_runtime_targets(config_root):
        _record_issue(
            issues,
            managed_path_issue(config_root, target, expected_kind=expected_kind),
        )
    return issues


def verify(config_root: Path) -> dict[str, Any]:
    config_root = absolute_path(config_root)
    manifest_path = config_root / "config" / "runtime-manifest.json"
    unsafe_paths = _unsafe_runtime_paths(config_root)
    if unsafe_paths:
        return {
            "ok": False,
            "manifest": str(manifest_path),
            "unsafe_paths": unsafe_paths,
            "symlinked": _reparse_paths(unsafe_paths),
            "permissions": {"ok": False, "not_checked": "unsafe_managed_path"},
        }

    manifest_issue = managed_path_issue(config_root, manifest_path, expected_kind="file")
    if manifest_issue is not None:
        return {
            "ok": False,
            "manifest": str(manifest_path),
            "unsafe_paths": [manifest_issue],
            "symlinked": _reparse_paths([manifest_issue]),
            "permissions": {"ok": False, "not_checked": "unsafe_managed_path"},
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "manifest": str(manifest_path),
            "missing_manifest": True,
            "unsafe_paths": [],
            "symlinked": [],
        }
    if not isinstance(manifest, dict):
        return {
            "ok": False,
            "manifest": str(manifest_path),
            "invalid_manifest": True,
            "unsafe_paths": [],
            "symlinked": [],
        }

    expected_raw = manifest.get("files")
    expected = expected_raw if isinstance(expected_raw, dict) else {}
    core_unsafe_names = sorted(
        str(name)
        for name in expected
        if not _manifest_name_is_safe(name) or "/" in str(name)
    )
    core_closure_missing = sorted(set(CORE_FILES) - set(expected))
    core_closure_unexpected = sorted(set(expected) - set(CORE_FILES))
    missing: list[str] = []
    mismatched: list[str] = []
    for name in CORE_FILES:
        path = config_root / "scripts" / name
        issue = managed_path_issue(config_root, path, expected_kind="file")
        if issue is not None:
            _record_issue(unsafe_paths, issue)
            continue
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            if name in expected:
                missing.append(name)
            continue
        if name in expected and stat.S_ISREG(metadata.st_mode) and sha256(path) != str(expected[name]):
            mismatched.append(name)

    support_expected_raw = manifest.get("support_files")
    support_expected = support_expected_raw if isinstance(support_expected_raw, dict) else {}
    support_unsafe_names = sorted(
        str(name) for name in support_expected if not _manifest_name_is_safe(name)
    )
    support_closure_missing = sorted(set(SUPPORT_FILES) - set(support_expected))
    support_closure_unexpected = sorted(set(support_expected) - set(SUPPORT_FILES))
    support_missing: list[str] = []
    support_mismatched: list[str] = []
    for name in SUPPORT_FILES:
        path = config_root / name
        issue = managed_path_issue(config_root, path, expected_kind="file")
        if issue is not None:
            _record_issue(unsafe_paths, issue)
            continue
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            if name in support_expected:
                support_missing.append(name)
            continue
        if (
            name in support_expected
            and stat.S_ISREG(metadata.st_mode)
            and sha256(path) != str(support_expected[name])
        ):
            support_mismatched.append(name)

    template_expected_raw = manifest.get("template_files")
    template_expected = template_expected_raw if isinstance(template_expected_raw, dict) else {}
    inventory_raw = manifest.get("template_inventory")
    inventory_is_list = isinstance(inventory_raw, list)
    inventory_values = inventory_raw if inventory_is_list else []
    inventory_strings = [name for name in inventory_values if isinstance(name, str)]
    inventory_names = set(inventory_strings)
    inventory_duplicates = sorted(
        {name for name in inventory_strings if inventory_strings.count(name) > 1}
    )
    inventory_invalid_entries = [str(name) for name in inventory_values if not isinstance(name, str)]
    hash_names = set(template_expected)
    template_unsafe_names = sorted(
        {
            str(name)
            for name in (*inventory_values, *template_expected.keys())
            if not _template_name_is_safe(name)
        }
    )
    template_count_raw = manifest.get("template_count")
    template_count_valid = (
        type(template_count_raw) is int
        and inventory_is_list
        and template_count_raw == len(inventory_values)
    )
    template_hash_missing = sorted(inventory_names - hash_names)
    template_hash_unexpected = sorted(hash_names - inventory_names)
    disk_inventory, template_tree_issues = scan_template_inventory(config_root)
    for issue in template_tree_issues:
        _record_issue(unsafe_paths, issue)
    disk_names = set(disk_inventory)
    template_disk_missing = sorted(inventory_names - disk_names)
    template_disk_unexpected = sorted(disk_names - inventory_names)
    required_template = "templates/vault/.gitignore"
    required_template_missing = (
        []
        if required_template in inventory_names and required_template in hash_names
        else [required_template]
    )
    template_missing: list[str] = []
    template_mismatched: list[str] = []
    for name, digest in template_expected.items():
        if not _template_name_is_safe(name):
            continue
        path = config_root / name
        issue = managed_path_issue(config_root, path, expected_kind="file")
        if issue is not None:
            _record_issue(unsafe_paths, issue)
            continue
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            template_missing.append(name)
            continue
        if stat.S_ISREG(metadata.st_mode) and sha256(path) != str(digest):
            template_mismatched.append(name)

    schema_valid = manifest.get("schema_version") == 2
    integrity_model_valid = manifest.get("integrity_model") == INTEGRITY_MODEL
    inventory_valid = (
        inventory_is_list
        and not inventory_invalid_entries
        and not inventory_duplicates
        and not template_unsafe_names
    )
    permissions = runtime_permission_report(config_root)
    closure = {
        "core_missing": core_closure_missing,
        "core_unexpected": core_closure_unexpected,
        "core_unsafe_names": core_unsafe_names,
        "support_missing": support_closure_missing,
        "support_unexpected": support_closure_unexpected,
        "support_unsafe_names": support_unsafe_names,
        "required_template_missing": required_template_missing,
        "template_unsafe_names": template_unsafe_names,
        "template_hash_missing": template_hash_missing,
        "template_hash_unexpected": template_hash_unexpected,
        "template_disk_missing": template_disk_missing,
        "template_disk_unexpected": template_disk_unexpected,
        "template_inventory_duplicates": inventory_duplicates,
        "template_inventory_invalid_entries": inventory_invalid_entries,
        "template_count_mismatch": not template_count_valid,
    }
    return {
        "ok": (
            schema_valid
            and integrity_model_valid
            and isinstance(expected_raw, dict)
            and isinstance(support_expected_raw, dict)
            and isinstance(template_expected_raw, dict)
            and inventory_valid
            and template_count_valid
            and not missing
            and not mismatched
            and not support_missing
            and not support_mismatched
            and not template_missing
            and not template_mismatched
            and not unsafe_paths
            and not template_unsafe_names
            and not core_unsafe_names
            and not support_unsafe_names
            and not core_closure_missing
            and not core_closure_unexpected
            and not support_closure_missing
            and not support_closure_unexpected
            and not required_template_missing
            and not template_hash_missing
            and not template_hash_unexpected
            and not template_disk_missing
            and not template_disk_unexpected
            and permissions["ok"]
        ),
        "manifest": str(manifest_path),
        "schema_version": manifest.get("schema_version"),
        "integrity_model": manifest.get("integrity_model", ""),
        "source_commit": manifest.get("source_commit", ""),
        "source_dirty": bool(manifest.get("source_dirty")),
        "checked_files": len(expected),
        "missing": missing,
        "mismatched": mismatched,
        "support_missing": support_missing,
        "support_mismatched": support_mismatched,
        "template_missing": sorted(template_missing),
        "template_mismatched": sorted(template_mismatched),
        "symlinked": _reparse_paths(unsafe_paths),
        "unsafe_paths": unsafe_paths,
        "closure": closure,
        "permissions": permissions,
    }


def assert_managed_target(
    config_root: Path,
    target: Path,
    *,
    expected_kind: str = "file",
    allow_missing: bool = True,
) -> None:
    """Reject managed paths that traverse a symlink, junction, or reparse point."""

    issue = managed_path_issue(
        config_root,
        target,
        expected_kind=expected_kind,
        allow_missing=allow_missing,
    )
    if issue is None:
        return
    if issue["reason"] == "reparse_point":
        raise StateSecurityError(
            "managed runtime path must not be a symlink or reparse point: "
            f"{issue['path']}"
        )
    raise StateSecurityError(
        f"managed runtime path is unsafe ({issue['reason']}): {issue['path']}"
    )


def _raise_first_unsafe_path(config_root: Path) -> None:
    unsafe_paths = _unsafe_runtime_paths(config_root)
    if not unsafe_paths:
        return
    first = unsafe_paths[0]
    if first["reason"] == "reparse_point":
        raise StateSecurityError(
            "managed runtime path must not be a symlink or reparse point: "
            f"{first['path']}"
        )
    raise StateSecurityError(
        f"managed runtime path is unsafe ({first['reason']}): {first['path']}"
    )


def _nearest_existing_directory(path: Path) -> Path:
    cursor = absolute_path(path)
    while True:
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            parent = cursor.parent
            if parent == cursor:
                raise StateSecurityError(
                    f"runtime install has no existing parent directory: {path}"
                )
            cursor = parent
            continue
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise StateSecurityError(
                f"runtime install parent is unsafe: {cursor}"
            )
        return cursor


@contextlib.contextmanager
def _runtime_install_mutex(config_root: Path) -> Iterator[None]:
    """Serialize publishers without placing a lock artifact in the runtime."""

    lock_parent = _nearest_existing_directory(config_root.parent)
    lock_key = hashlib.sha256(
        os.path.normcase(os.path.normpath(os.fspath(config_root))).encode("utf-8")
    ).hexdigest()[:20]
    lock_path = lock_parent / f".agent-memory-runtime-install-{lock_key}.lock"
    try:
        lock_path.mkdir()
    except FileExistsError as exc:
        raise StateSecurityError(
            f"runtime install is already in progress: {config_root}"
        ) from exc
    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except FileNotFoundError:
            pass


def _runtime_sources(manifest: dict[str, Any]) -> list[tuple[str, Path, int | None]]:
    sources: list[tuple[str, Path, int | None]] = []
    for name in CORE_FILES:
        source = SOURCE_ROOT / name
        mode = source.stat().st_mode & 0o777 if POSIX_MODE_ENFORCED else None
        sources.append((f"scripts/{name}", source, mode))
    sources.extend((name, REPO_ROOT / name, None) for name in SUPPORT_FILES)
    sources.extend(
        (name, REPO_ROOT / name, None) for name in manifest["template_inventory"]
    )
    return sources


def _change_report(
    config_root: Path,
    manifest: dict[str, Any],
) -> tuple[list[str], list[str]]:
    expected: list[tuple[str, str]] = []
    expected.extend((name, manifest["files"][name]) for name in CORE_FILES)
    expected.extend((name, manifest["support_files"][name]) for name in SUPPORT_FILES)
    expected.extend(manifest["template_files"].items())
    changed: list[str] = []
    unchanged: list[str] = []
    for report_name, digest in expected:
        relative = f"scripts/{report_name}" if report_name in CORE_FILES else report_name
        target = config_root / relative
        assert_managed_target(config_root, target)
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            changed.append(report_name)
            continue
        if stat.S_ISREG(metadata.st_mode) and sha256(target) == digest:
            unchanged.append(report_name)
        else:
            changed.append(report_name)
    return changed, unchanged


def _create_managed_directory(
    config_root: Path,
    directory: Path,
    created: list[Path],
) -> None:
    missing: list[Path] = []
    cursor = directory
    while cursor != config_root:
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            missing.append(cursor)
            cursor = cursor.parent
            continue
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise StateSecurityError(f"managed runtime directory is unsafe: {cursor}")
        break
    assert_managed_target(
        config_root,
        cursor,
        expected_kind="directory",
        allow_missing=False,
    )
    for candidate in reversed(missing):
        assert_managed_target(config_root, candidate, expected_kind="directory")
        candidate.mkdir()
        created.append(candidate)
        assert_managed_target(
            config_root,
            candidate,
            expected_kind="directory",
            allow_missing=False,
        )


def _stage_runtime(
    config_root: Path,
    manifest: dict[str, Any],
) -> Path:
    stage_root = Path(tempfile.mkdtemp(prefix=".runtime-stage-", dir=config_root))
    try:
        for relative, source, mode in _runtime_sources(manifest):
            target = stage_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if mode is not None:
                target.chmod(mode)
        manifest_path = stage_root / "config" / "runtime-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        harden_runtime_permissions(stage_root)
        staged_verification = verify(stage_root)
        if not staged_verification.get("ok"):
            raise StateSecurityError(
                "staged runtime closure failed verification: "
                + json.dumps(staged_verification, ensure_ascii=False, sort_keys=True)
            )
        return stage_root
    except BaseException:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _remove_empty_directories(paths: list[Path]) -> None:
    for directory in sorted(set(paths), key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _publish_staged_runtime(
    config_root: Path,
    stage_root: Path,
    manifest: dict[str, Any],
) -> None:
    backup_root = stage_root / ".rollback"
    backup_root.mkdir()
    created_directories: list[Path] = []
    actions: list[dict[str, Any]] = []

    def publish(relative: str, mode: int | None = None) -> None:
        source = stage_root / relative
        target = config_root / relative
        assert_managed_target(config_root, target)
        _create_managed_directory(config_root, target.parent, created_directories)
        assert_managed_target(config_root, target)
        backup = backup_root / relative
        existed = False
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            pass
        else:
            if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise StateSecurityError(f"managed runtime file is unsafe: {target}")
            existed = True
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        action = {"target": target, "backup": backup, "existed": existed, "published": False}
        actions.append(action)
        assert_managed_target(config_root, target)
        os.replace(source, target)
        action["published"] = True
        if mode is not None:
            target.chmod(mode)

    try:
        manifest_relative = "config/runtime-manifest.json"
        for relative, _source, mode in _runtime_sources(manifest):
            publish(relative, mode)

        desired_templates = set(manifest["template_inventory"])
        disk_templates, template_issues = scan_template_inventory(config_root)
        if template_issues:
            first = template_issues[0]
            raise StateSecurityError(
                f"managed template tree is unsafe ({first['reason']}): {first['path']}"
            )
        for relative in sorted(set(disk_templates) - desired_templates):
            target = config_root / relative
            assert_managed_target(config_root, target, allow_missing=False)
            backup = backup_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            action = {
                "target": target,
                "backup": backup,
                "existed": True,
                "published": False,
                "removed": False,
            }
            actions.append(action)
            os.replace(target, backup)
            action["removed"] = True

        # The manifest is the commit marker and is always published last.
        publish(manifest_relative)
        harden_runtime_permissions(config_root)
        installed_verification = verify(config_root)
        if not installed_verification.get("ok"):
            raise StateSecurityError(
                "published runtime closure failed verification: "
                + json.dumps(installed_verification, ensure_ascii=False, sort_keys=True)
            )
    except BaseException as install_error:
        rollback_errors: list[str] = []
        for action in reversed(actions):
            target = action["target"]
            backup = action["backup"]
            try:
                if action.get("existed") and backup.exists():
                    os.replace(backup, target)
                elif action.get("published"):
                    target.unlink(missing_ok=True)
            except OSError as exc:
                rollback_errors.append(f"{target}: {exc}")
        _remove_empty_directories(created_directories)
        if rollback_errors:
            raise RuntimeRollbackError(backup_root, rollback_errors) from install_error
        raise


def install(config_root: Path, dry_run: bool) -> dict[str, Any]:
    config_root = absolute_path(config_root)
    _raise_first_unsafe_path(config_root)
    manifest = expected_manifest(config_root)
    for name in manifest["template_inventory"]:
        assert_managed_target(config_root, config_root / name)
    changed, unchanged = _change_report(config_root, manifest)
    if not dry_run:
        root_created = False
        with _runtime_install_mutex(config_root):
            _raise_first_unsafe_path(config_root)
            try:
                try:
                    metadata = os.lstat(config_root)
                except FileNotFoundError:
                    config_root.mkdir(parents=True)
                    root_created = True
                else:
                    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                        raise StateSecurityError(
                            f"managed runtime root is unsafe: {config_root}"
                        )
                assert_managed_target(
                    config_root,
                    config_root,
                    expected_kind="directory",
                    allow_missing=False,
                )
                ensure_private_directory(config_root, harden_existing=True)
                stage_root = _stage_runtime(config_root, manifest)
                preserve_stage = False
                try:
                    _raise_first_unsafe_path(config_root)
                    _publish_staged_runtime(config_root, stage_root, manifest)
                except RuntimeRollbackError:
                    preserve_stage = True
                    raise
                finally:
                    if not preserve_stage:
                        shutil.rmtree(stage_root, ignore_errors=True)
            except BaseException:
                if root_created:
                    try:
                        config_root.rmdir()
                    except OSError:
                        pass
                raise
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
