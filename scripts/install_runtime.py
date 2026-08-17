#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import errno
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

from ctypes import wintypes

from agent_memory_lock import try_lock, unlock
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
RUNTIME_JOURNAL_NAME = ".runtime-install-journal.json"
RUNTIME_JOURNAL_SCHEMA = 1
RUNTIME_STAGE_PREFIX = ".runtime-stage-"
DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)
WINDOWS_FILE_SHARE_READ = 0x00000001
WINDOWS_FILE_SHARE_WRITE = 0x00000002
WINDOWS_FILE_SHARE_DELETE = 0x00000004
WINDOWS_GENERIC_WRITE = 0x40000000
WINDOWS_OPEN_EXISTING = 3
WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
WINDOWS_FILE_FLAG_WRITE_THROUGH = 0x80000000
WINDOWS_MOVEFILE_REPLACE_EXISTING = 0x00000001
WINDOWS_MOVEFILE_WRITE_THROUGH = 0x00000008
WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_CRASH_RECOVERY = True
POWER_LOSS_DURABILITY = "verified"


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class RuntimeRollbackError(StateSecurityError):
    """A failed publish has recoverable backups that must not be deleted."""

    def __init__(self, recovery_path: Path, errors: list[str]) -> None:
        self.recovery_path = recovery_path
        super().__init__(
            "runtime install failed and rollback was incomplete; "
            f"recovery_path={recovery_path}; errors=" + "; ".join(errors)
        )


class RuntimeRecoveryError(StateSecurityError):
    """A durable transaction could not be safely recovered or cleaned."""

    def __init__(self, recovery_path: Path, errors: list[str]) -> None:
        self.recovery_path = recovery_path
        super().__init__(
            "runtime transaction recovery was incomplete; "
            f"recovery_path={recovery_path}; errors=" + "; ".join(errors)
        )


class WindowsDurabilityError(StateSecurityError):
    """The active Windows filesystem cannot provide the required barriers."""

    def __init__(self, detail: str) -> None:
        prefix = "windows_durability_unsupported: "
        super().__init__(detail if detail.startswith(prefix) else prefix + detail)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (int(metadata.st_dev), int(metadata.st_ino))


def _windows_handle_identity_matches(
    metadata: os.stat_result,
    handle_identity: tuple[int, int],
) -> bool:
    """Match Win32 volume/file IDs across Python's old and widened st_dev forms."""

    volume_serial, file_index = handle_identity
    return (
        int(metadata.st_ino) == file_index
        and int(metadata.st_dev) & 0xFFFFFFFF == volume_serial
    )


def _windows_flush_path(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        raise WindowsDurabilityError("FlushFileBuffers is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    flags = WINDOWS_FILE_FLAG_WRITE_THROUGH
    if directory:
        flags |= WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(
        str(path),
        WINDOWS_GENERIC_WRITE,
        WINDOWS_FILE_SHARE_READ | WINDOWS_FILE_SHARE_WRITE | WINDOWS_FILE_SHARE_DELETE,
        None,
        WINDOWS_OPEN_EXISTING,
        flags,
        None,
    )
    if handle == WINDOWS_INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise WindowsDurabilityError(
            f"CreateFileW failed for {'directory' if directory else 'file'} "
            f"{path}: {ctypes.FormatError(error).strip()} (winerror={error})"
        )
    try:
        if not flush_file_buffers(handle):
            error = ctypes.get_last_error()
            raise WindowsDurabilityError(
                f"FlushFileBuffers failed for {path}: "
                f"{ctypes.FormatError(error).strip()} (winerror={error})"
            )
    finally:
        close_handle(handle)


def _windows_replace_write_through(source: Path, target: Path) -> None:
    if os.name != "nt":
        raise WindowsDurabilityError("MoveFileExW is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    flags = WINDOWS_MOVEFILE_REPLACE_EXISTING | WINDOWS_MOVEFILE_WRITE_THROUGH
    if not move_file(str(source), str(target), flags):
        error = ctypes.get_last_error()
        raise WindowsDurabilityError(
            f"MoveFileExW failed for {source} -> {target}: "
            f"{ctypes.FormatError(error).strip()} (winerror={error})"
        )


def _fsync_file(path: Path) -> None:
    if os.name == "nt":
        _windows_flush_path(path, directory=False)
        return
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform exposes that operation."""

    if os.name == "nt":
        _windows_flush_path(path, directory=True)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS:
                raise
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, target: Path) -> None:
    """Replace a file only after durable content and namespace barriers."""

    _fsync_file(source)
    source_parent = source.parent
    target_parent = target.parent
    if os.name == "nt":
        _windows_replace_write_through(source, target)
    else:
        os.replace(source, target)
    _fsync_file(target)
    _fsync_directory(source_parent)
    if target_parent != source_parent:
        _fsync_directory(target_parent)


def _durable_unlink(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _fsync_directory_chain(path: Path, stop: Path) -> None:
    cursor = path
    while True:
        _fsync_directory(cursor)
        if cursor == stop:
            return
        parent = cursor.parent
        if parent == cursor or not _lexically_contains(stop, parent):
            raise StateSecurityError(f"directory fsync path escapes transaction root: {path}")
        cursor = parent


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if POSIX_MODE_ENFORCED:
            temporary.chmod(PRIVATE_FILE_MODE)
        _durable_replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
        # git writes UTF-8; the locale codec (cp936 on zh-CN Windows) would
        # corrupt any non-ASCII value this returns.
        encoding="utf-8",
        errors="replace",
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
        "process_crash_recovery": PROCESS_CRASH_RECOVERY,
        "power_loss_durability": POWER_LOSS_DURABILITY,
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


def verify(
    config_root: Path,
    *,
    allow_legacy_durability_contract: bool = False,
) -> dict[str, Any]:
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
    legacy_durability_contract = (
        allow_legacy_durability_contract
        and "process_crash_recovery" not in manifest
        and "power_loss_durability" not in manifest
    )
    process_crash_recovery_valid = (
        manifest.get("process_crash_recovery") is PROCESS_CRASH_RECOVERY
        or legacy_durability_contract
    )
    power_loss_durability_valid = (
        manifest.get("power_loss_durability") == POWER_LOSS_DURABILITY
        or legacy_durability_contract
    )
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
            and process_crash_recovery_valid
            and power_loss_durability_valid
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
        "process_crash_recovery": manifest.get("process_crash_recovery", False),
        "power_loss_durability": manifest.get(
            "power_loss_durability", "unsupported"
        ),
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


def _strip_windows_device_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\") or path.startswith("\\??\\"):
        return path[4:]
    return path


def _windows_final_path_identity(path: Path) -> tuple[str, tuple[int, int]]:
    """Return the final DOS path and stable identity for an existing object."""

    if os.name != "nt":
        raise OSError("Windows final-path lookup is unavailable on this platform")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0,
        WINDOWS_FILE_SHARE_READ | WINDOWS_FILE_SHARE_WRITE | WINDOWS_FILE_SHARE_DELETE,
        None,
        WINDOWS_OPEN_EXISTING,
        WINDOWS_FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == WINDOWS_INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if written == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if written >= len(buffer):
            raise OSError(f"canonical Windows path exceeds supported length: {path}")
        information = _WindowsByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = (
            int(information.volume_serial_number),
            (int(information.file_index_high) << 32)
            | int(information.file_index_low),
        )
        return _strip_windows_device_prefix(buffer.value), identity
    finally:
        close_handle(handle)


def _canonical_runtime_lock_material(config_root: Path) -> str:
    """Canonicalize aliases while remaining stable as missing parents appear."""

    path = absolute_path(config_root)
    if os.name != "nt":
        return os.path.normcase(os.path.normpath(os.fspath(path)))
    missing: list[str] = []
    cursor = path
    while True:
        try:
            before = os.lstat(cursor)
        except FileNotFoundError:
            parent = cursor.parent
            if parent == cursor:
                raise StateSecurityError(
                    f"runtime install lock cannot find an existing parent: {config_root}"
                )
            missing.append(cursor.name)
            cursor = parent
            continue
        if _is_reparse(before):
            raise StateSecurityError(
                f"runtime install lock path is a reparse point: {cursor}"
            )
        if missing and not stat.S_ISDIR(before.st_mode):
            raise StateSecurityError(
                f"runtime install lock parent is not a directory: {cursor}"
            )
        final_path, handle_identity = _windows_final_path_identity(cursor)
        after = os.lstat(cursor)
        if (
            _is_reparse(after)
            or _metadata_identity(before) != _metadata_identity(after)
            or not _windows_handle_identity_matches(after, handle_identity)
        ):
            raise StateSecurityError(
                f"runtime install lock path changed during canonicalization: {cursor}"
            )
        canonical = os.path.join(final_path, *reversed(missing))
        return os.path.normcase(os.path.normpath(canonical))


@contextlib.contextmanager
def _runtime_install_mutex(config_root: Path) -> Iterator[None]:
    """Serialize publishers with a process-owned lock that survives stale metadata."""

    # A system-temp anchor is stable even while a fresh install creates the
    # config root's previously missing parents. The config-root digest keeps
    # independent runtimes isolated without relying on a process identifier.
    lock_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    lock_parent_issue = managed_path_issue(
        lock_parent,
        lock_parent,
        expected_kind="directory",
        allow_missing=False,
    )
    if lock_parent_issue is not None:
        raise StateSecurityError(
            f"runtime install lock parent is unsafe: {lock_parent_issue['path']}"
        )
    lock_key = hashlib.sha256(
        _canonical_runtime_lock_material(config_root).encode("utf-8")
    ).hexdigest()[:20]
    lock_path = lock_parent / f".agent-memory-runtime-install-{lock_key}.lock"
    try:
        existing = os.lstat(lock_path)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        _is_reparse(existing) or not stat.S_ISREG(existing.st_mode)
    ):
        raise StateSecurityError(
            f"runtime install lock path is unsafe: {lock_path}"
        )
    handle = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        opened = os.fstat(handle.fileno())
        current = os.lstat(lock_path)
        if (
            _is_reparse(opened)
            or _is_reparse(current)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _metadata_identity(opened) != _metadata_identity(current)
        ):
            raise StateSecurityError(f"runtime install lock path changed: {lock_path}")
        if not try_lock(handle):
            raise StateSecurityError(
                f"runtime install is already in progress: {config_root}"
            )
        locked = True
        current = os.lstat(lock_path)
        if (
            _is_reparse(current)
            or not stat.S_ISREG(current.st_mode)
            or _metadata_identity(opened) != _metadata_identity(current)
        ):
            raise StateSecurityError(f"runtime install lock path changed: {lock_path}")
        yield
    finally:
        if locked:
            unlock(handle)
        handle.close()


def _runtime_journal_path(config_root: Path) -> Path:
    return config_root / RUNTIME_JOURNAL_NAME


def _stable_json_read(path: Path) -> dict[str, Any]:
    before = os.lstat(path)
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise StateSecurityError(f"runtime transaction journal is unsafe: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            _is_reparse(opened)
            or _is_reparse(current)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _metadata_identity(before) != _metadata_identity(opened)
            or _metadata_identity(opened) != _metadata_identity(current)
            or before.st_size != opened.st_size
            or before.st_mtime_ns != opened.st_mtime_ns
            or opened.st_size != current.st_size
            or opened.st_mtime_ns != current.st_mtime_ns
        ):
            raise StateSecurityError(f"runtime transaction journal changed: {path}")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after_open = os.fstat(descriptor)
        after_path = os.lstat(path)
        stable_fields = (
            "st_size",
            "st_mtime_ns",
        )
        if (
            _metadata_identity(opened) != _metadata_identity(after_open)
            or _metadata_identity(after_open) != _metadata_identity(after_path)
            or any(getattr(opened, field) != getattr(after_open, field) for field in stable_fields)
            or any(getattr(after_open, field) != getattr(after_path, field) for field in stable_fields)
        ):
            raise StateSecurityError(f"runtime transaction journal changed: {path}")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateSecurityError(f"runtime transaction journal is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise StateSecurityError(f"runtime transaction journal is invalid: {path}")
    return payload


def _write_runtime_journal(config_root: Path, payload: dict[str, Any]) -> None:
    journal_path = _runtime_journal_path(config_root)
    assert_managed_target(config_root, journal_path)
    _atomic_json_write(journal_path, payload)
    assert_managed_target(
        config_root,
        journal_path,
        expected_kind="file",
        allow_missing=False,
    )


def _remove_runtime_journal(config_root: Path) -> None:
    journal_path = _runtime_journal_path(config_root)
    try:
        metadata = os.lstat(journal_path)
    except FileNotFoundError:
        return
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise StateSecurityError(f"runtime transaction journal is unsafe: {journal_path}")
    _durable_unlink(journal_path)


def _valid_sha256(value: object, *, allow_none: bool = False) -> bool:
    if allow_none and value is None:
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _journal_target_is_managed(relative: str) -> bool:
    if relative == "config/runtime-manifest.json" or relative in SUPPORT_FILES:
        return True
    if relative.startswith("scripts/"):
        return relative.removeprefix("scripts/") in CORE_FILES
    return _template_name_is_safe(relative)


def _validated_transaction(
    config_root: Path,
    payload: dict[str, Any],
) -> tuple[Path, list[dict[str, Any]]]:
    expected_payload_keys = {
        "schema",
        "config_root",
        "stage",
        "state",
        "previous_manifest_sha256",
        "actions",
    }
    if set(payload) != expected_payload_keys:
        raise StateSecurityError("runtime transaction journal fields are invalid")
    if payload.get("schema") != RUNTIME_JOURNAL_SCHEMA or payload.get("config_root") != ".":
        raise StateSecurityError("runtime transaction journal schema is invalid")
    state = payload.get("state")
    if state not in {"staging", "publishing", "committed"}:
        raise StateSecurityError("runtime transaction journal state is invalid")
    stage_name = payload.get("stage")
    if (
        not isinstance(stage_name, str)
        or not stage_name.startswith(RUNTIME_STAGE_PREFIX)
        or not _manifest_segment_is_safe(stage_name)
        or "/" in stage_name
        or "\\" in stage_name
    ):
        raise StateSecurityError("runtime transaction stage is invalid")
    previous_manifest = payload.get("previous_manifest_sha256")
    if not _valid_sha256(previous_manifest, allow_none=True):
        raise StateSecurityError("runtime transaction previous manifest digest is invalid")
    actions_value = payload.get("actions")
    if not isinstance(actions_value, list):
        raise StateSecurityError("runtime transaction actions are invalid")
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_action in actions_value:
        if not isinstance(raw_action, dict):
            raise StateSecurityError("runtime transaction action is invalid")
        if set(raw_action) != {
            "relative",
            "kind",
            "existed",
            "status",
            "desired_sha256",
            "backup_sha256",
        }:
            raise StateSecurityError("runtime transaction action fields are invalid")
        relative = raw_action.get("relative")
        kind = raw_action.get("kind")
        existed = raw_action.get("existed")
        status_value = raw_action.get("status")
        desired_digest = raw_action.get("desired_sha256")
        backup_digest = raw_action.get("backup_sha256")
        if (
            not isinstance(relative, str)
            or not _manifest_name_is_safe(relative)
            or not _journal_target_is_managed(relative)
            or relative in seen
            or kind not in {"publish", "remove"}
            or type(existed) is not bool
            or status_value not in {"prepared", "published"}
            or not _valid_sha256(desired_digest, allow_none=kind == "remove")
            or (kind == "remove" and desired_digest is not None)
            or not _valid_sha256(backup_digest, allow_none=not existed)
            or (existed and backup_digest is None)
            or (not existed and backup_digest is not None)
        ):
            raise StateSecurityError("runtime transaction action is invalid")
        seen.add(relative)
        actions.append(raw_action)
    if state == "staging" and actions:
        raise StateSecurityError("staging runtime transaction must not contain actions")
    manifest_positions = [
        index
        for index, action in enumerate(actions)
        if action["relative"] == "config/runtime-manifest.json"
    ]
    if manifest_positions and manifest_positions != [len(actions) - 1]:
        raise StateSecurityError("runtime transaction manifest action must be last")
    if state == "committed" and (
        not manifest_positions or actions[-1]["status"] != "published"
    ):
        raise StateSecurityError("committed runtime transaction is incomplete")
    stage_root = config_root / stage_name
    if stage_root.parent != config_root or not _lexically_contains(config_root, stage_root):
        raise StateSecurityError("runtime transaction stage escapes config root")
    try:
        stage_metadata = os.lstat(stage_root)
    except FileNotFoundError:
        pass
    else:
        if _is_reparse(stage_metadata) or not stat.S_ISDIR(stage_metadata.st_mode):
            raise StateSecurityError(f"runtime transaction stage is unsafe: {stage_root}")
        _files, _directories, stage_issues = _scan_regular_tree(stage_root)
        if stage_issues:
            raise StateSecurityError(
                "runtime transaction stage contains unsafe paths: "
                f"{stage_issues[0]['path']}"
            )
    return stage_root, actions


def _safe_remove_stage(config_root: Path, stage_root: Path) -> None:
    if (
        stage_root.parent != config_root
        or not stage_root.name.startswith(RUNTIME_STAGE_PREFIX)
        or not _manifest_segment_is_safe(stage_root.name)
    ):
        raise StateSecurityError(f"runtime transaction stage is unsafe: {stage_root}")
    try:
        metadata = os.lstat(stage_root)
    except FileNotFoundError:
        return
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise StateSecurityError(f"runtime transaction stage is unsafe: {stage_root}")
    _files, _directories, issues = _scan_regular_tree(stage_root)
    if issues:
        raise StateSecurityError(
            f"runtime transaction stage contains unsafe paths: {issues[0]['path']}"
        )
    shutil.rmtree(stage_root)
    _fsync_directory(config_root)


def _cleanup_orphan_stages(config_root: Path) -> None:
    try:
        entries = list(os.scandir(config_root))
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.name.startswith(RUNTIME_STAGE_PREFIX):
            stage_root = Path(entry.path)
            recovery_path = stage_root / ".rollback"
            try:
                recovery_metadata = os.lstat(recovery_path)
            except FileNotFoundError:
                pass
            else:
                if _is_reparse(recovery_metadata) or not stat.S_ISDIR(
                    recovery_metadata.st_mode
                ):
                    raise RuntimeRecoveryError(
                        recovery_path,
                        ["orphan rollback path is unsafe"],
                    )
                recovery_files, _directories, recovery_issues = _scan_regular_tree(
                    recovery_path
                )
                if recovery_issues or recovery_files:
                    detail = (
                        f"unsafe path: {recovery_issues[0]['path']}"
                        if recovery_issues
                        else "orphan rollback evidence requires manual recovery"
                    )
                    raise RuntimeRecoveryError(recovery_path, [detail])
            _safe_remove_stage(config_root, stage_root)


def _cleanup_journal_temporaries(config_root: Path) -> None:
    prefix = f".{RUNTIME_JOURNAL_NAME}."
    try:
        entries = list(os.scandir(config_root))
    except FileNotFoundError:
        return
    for entry in entries:
        if not (entry.name.startswith(prefix) and entry.name.endswith(".tmp")):
            continue
        path = Path(entry.path)
        metadata = os.lstat(path)
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeRecoveryError(path, ["orphan journal temporary is unsafe"])
        _durable_unlink(path)


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
        _fsync_directory(candidate)
        _fsync_directory(candidate.parent)
        assert_managed_target(
            config_root,
            candidate,
            expected_kind="directory",
            allow_missing=False,
        )


def _stage_runtime(
    config_root: Path,
    manifest: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    manifest_target = config_root / "config" / "runtime-manifest.json"
    try:
        manifest_metadata = os.lstat(manifest_target)
    except FileNotFoundError:
        previous_manifest_digest = None
    else:
        if _is_reparse(manifest_metadata) or not stat.S_ISREG(manifest_metadata.st_mode):
            raise StateSecurityError(f"managed runtime file is unsafe: {manifest_target}")
        previous_manifest_digest = sha256(manifest_target)
    stage_root = Path(tempfile.mkdtemp(prefix=RUNTIME_STAGE_PREFIX, dir=config_root))
    journal: dict[str, Any] = {
        "schema": RUNTIME_JOURNAL_SCHEMA,
        "config_root": ".",
        "stage": stage_root.name,
        "state": "staging",
        "previous_manifest_sha256": previous_manifest_digest,
        "actions": [],
    }
    try:
        _write_runtime_journal(config_root, journal)
        for relative, source, mode in _runtime_sources(manifest):
            target = stage_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if mode is not None:
                target.chmod(mode)
            _fsync_file(target)
            _fsync_directory_chain(target.parent, stage_root)
        manifest_path = stage_root / "config" / "runtime-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _fsync_file(manifest_path)
        _fsync_directory_chain(manifest_path.parent, stage_root)
        harden_runtime_permissions(stage_root)
        staged_verification = verify(stage_root)
        if not staged_verification.get("ok"):
            raise StateSecurityError(
                "staged runtime closure failed verification: "
                + json.dumps(staged_verification, ensure_ascii=False, sort_keys=True)
            )
        journal["state"] = "publishing"
        _write_runtime_journal(config_root, journal)
        return stage_root, journal
    except BaseException:
        try:
            _safe_remove_stage(config_root, stage_root)
            _remove_runtime_journal(config_root)
        except OSError:
            pass
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
    journal: dict[str, Any],
) -> None:
    backup_root = stage_root / ".rollback"
    backup_root.mkdir()
    _fsync_directory(stage_root)
    created_directories: list[Path] = []

    def publish(relative: str, mode: int | None = None) -> None:
        source = stage_root / relative
        target = config_root / relative
        source_issue = managed_path_issue(
            stage_root,
            source,
            expected_kind="file",
            allow_missing=False,
        )
        if source_issue is not None:
            raise StateSecurityError(f"staged runtime source is unsafe: {source_issue['path']}")
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
            backup_issue = managed_path_issue(stage_root, backup)
            if backup_issue is not None:
                raise StateSecurityError(
                    f"runtime rollback backup path is unsafe: {backup_issue['path']}"
                )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            backup_issue = managed_path_issue(
                stage_root,
                backup,
                expected_kind="file",
                allow_missing=False,
            )
            if backup_issue is not None:
                raise StateSecurityError(
                    f"runtime rollback backup path is unsafe: {backup_issue['path']}"
                )
            _fsync_file(backup)
            _fsync_directory_chain(backup.parent, stage_root)
        action = {
            "relative": relative,
            "kind": "publish",
            "existed": existed,
            "status": "prepared",
            "desired_sha256": sha256(source),
            "backup_sha256": sha256(backup) if existed else None,
        }
        journal["actions"].append(action)
        _write_runtime_journal(config_root, journal)
        assert_managed_target(config_root, target)
        _durable_replace(source, target)
        if mode is not None:
            target.chmod(mode)
        _fsync_file(target)
        action["status"] = "published"
        _write_runtime_journal(config_root, journal)

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
            backup_issue = managed_path_issue(stage_root, backup)
            if backup_issue is not None:
                raise StateSecurityError(
                    f"runtime rollback backup path is unsafe: {backup_issue['path']}"
                )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            backup_issue = managed_path_issue(
                stage_root,
                backup,
                expected_kind="file",
                allow_missing=False,
            )
            if backup_issue is not None:
                raise StateSecurityError(
                    f"runtime rollback backup path is unsafe: {backup_issue['path']}"
                )
            _fsync_file(backup)
            _fsync_directory_chain(backup.parent, stage_root)
            action = {
                "relative": relative,
                "kind": "remove",
                "existed": True,
                "status": "prepared",
                "desired_sha256": None,
                "backup_sha256": sha256(backup),
            }
            journal["actions"].append(action)
            _write_runtime_journal(config_root, journal)
            _durable_unlink(target)
            action["status"] = "published"
            _write_runtime_journal(config_root, journal)

        # The manifest is the commit marker and is always published last.
        publish(manifest_relative)
        harden_runtime_permissions(config_root)
        installed_verification = verify(config_root)
        if not installed_verification.get("ok"):
            raise StateSecurityError(
                "published runtime closure failed verification: "
                + json.dumps(installed_verification, ensure_ascii=False, sort_keys=True)
            )
        journal["state"] = "committed"
        _write_runtime_journal(config_root, journal)
    except BaseException as install_error:
        try:
            _recover_stale_transaction(config_root)
        except (RuntimeRollbackError, RuntimeRecoveryError) as recovery_error:
            raise recovery_error from install_error
        _remove_empty_directories(created_directories)
        raise


def _action_target_digest(config_root: Path, relative: str) -> str | None:
    target = config_root / relative
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        return None
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise StateSecurityError(f"managed runtime file is unsafe: {target}")
    return sha256(target)


def _rollback_transaction_action(
    config_root: Path,
    stage_root: Path,
    action: dict[str, Any],
) -> None:
    relative = action["relative"]
    target = config_root / relative
    backup = stage_root / ".rollback" / relative
    assert_managed_target(config_root, target)
    backup_issue = managed_path_issue(stage_root, backup)
    if backup_issue is not None:
        raise StateSecurityError(
            f"runtime rollback backup path is unsafe: {backup_issue['path']}"
        )
    current_digest = _action_target_digest(config_root, relative)
    desired_digest = action["desired_sha256"]
    backup_digest = action["backup_sha256"]
    if action["existed"]:
        try:
            backup_metadata = os.lstat(backup)
        except FileNotFoundError:
            if current_digest == backup_digest:
                return
            raise StateSecurityError(f"runtime rollback backup is missing: {backup}")
        if _is_reparse(backup_metadata) or not stat.S_ISREG(backup_metadata.st_mode):
            raise StateSecurityError(f"runtime rollback backup is unsafe: {backup}")
        if sha256(backup) != backup_digest:
            raise StateSecurityError(f"runtime rollback backup digest mismatch: {backup}")
        allowed_current = {backup_digest}
        if desired_digest is not None:
            allowed_current.add(desired_digest)
        if current_digest is not None and current_digest not in allowed_current:
            raise StateSecurityError(f"runtime rollback target changed: {target}")
        assert_managed_target(config_root, target)
        _durable_replace(backup, target)
        if sha256(target) != backup_digest:
            raise StateSecurityError(f"runtime rollback restore mismatch: {target}")
        return
    if current_digest is None:
        return
    if desired_digest is None or current_digest != desired_digest:
        raise StateSecurityError(f"runtime rollback target changed: {target}")
    _durable_unlink(target)


def _verify_restored_transaction(
    config_root: Path,
    payload: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    for action in actions:
        current_digest = _action_target_digest(config_root, action["relative"])
        expected_digest = action["backup_sha256"] if action["existed"] else None
        if current_digest != expected_digest:
            raise StateSecurityError(
                f"runtime rollback verification failed: {action['relative']}"
            )
    previous_manifest_digest = payload["previous_manifest_sha256"]
    manifest_path = config_root / "config" / "runtime-manifest.json"
    if previous_manifest_digest is None:
        if _action_target_digest(config_root, "config/runtime-manifest.json") is not None:
            raise StateSecurityError("runtime rollback left an unexpected manifest")
        return
    if _action_target_digest(config_root, "config/runtime-manifest.json") != previous_manifest_digest:
        raise StateSecurityError("runtime rollback did not restore the previous manifest")
    restored = verify(config_root, allow_legacy_durability_contract=True)
    if not restored.get("ok"):
        raise StateSecurityError(
            "restored runtime closure failed verification: "
            + json.dumps(restored, ensure_ascii=False, sort_keys=True)
        )


def _cleanup_runtime_transaction(config_root: Path, stage_root: Path) -> None:
    _safe_remove_stage(config_root, stage_root)
    _remove_runtime_journal(config_root)


def _recover_stale_transaction(config_root: Path) -> bool:
    journal_path = _runtime_journal_path(config_root)
    try:
        os.lstat(journal_path)
    except FileNotFoundError:
        _cleanup_orphan_stages(config_root)
        _cleanup_journal_temporaries(config_root)
        return False
    payload = _stable_json_read(journal_path)
    stage_root, actions = _validated_transaction(config_root, payload)
    recovery_path = stage_root / ".rollback"
    if payload["state"] == "committed":
        committed = verify(config_root)
        if not committed.get("ok"):
            raise RuntimeRecoveryError(
                recovery_path,
                [
                    "committed runtime closure failed verification: "
                    + json.dumps(committed, ensure_ascii=False, sort_keys=True)
                ],
            )
    else:
        rollback_errors: list[str] = []
        for action in reversed(actions):
            try:
                _rollback_transaction_action(config_root, stage_root, action)
            except (OSError, StateSecurityError) as exc:
                rollback_errors.append(f"{action['relative']}: {exc}")
        if not rollback_errors:
            try:
                _verify_restored_transaction(config_root, payload, actions)
            except (OSError, StateSecurityError) as exc:
                rollback_errors.append(str(exc))
        if rollback_errors:
            raise RuntimeRollbackError(recovery_path, rollback_errors)
    try:
        _cleanup_runtime_transaction(config_root, stage_root)
        _cleanup_orphan_stages(config_root)
        _cleanup_journal_temporaries(config_root)
    except (OSError, StateSecurityError) as exc:
        raise RuntimeRecoveryError(recovery_path, [str(exc)]) from exc
    return True


def install(config_root: Path, dry_run: bool) -> dict[str, Any]:
    config_root = absolute_path(config_root)
    _raise_first_unsafe_path(config_root)
    recovered_transaction = False
    if dry_run:
        try:
            journal_metadata = os.lstat(_runtime_journal_path(config_root))
        except FileNotFoundError:
            pass
        else:
            if _is_reparse(journal_metadata) or not stat.S_ISREG(journal_metadata.st_mode):
                raise StateSecurityError("runtime transaction journal is unsafe")
            raise StateSecurityError(
                "runtime transaction recovery requires a non-dry install"
            )
        manifest = expected_manifest(config_root)
        for name in manifest["template_inventory"]:
            assert_managed_target(config_root, config_root / name)
        changed, unchanged = _change_report(config_root, manifest)
    else:
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
                _fsync_directory(config_root)
                recovered_transaction = _recover_stale_transaction(config_root)
                _raise_first_unsafe_path(config_root)
                manifest = expected_manifest(config_root)
                for name in manifest["template_inventory"]:
                    assert_managed_target(config_root, config_root / name)
                changed, unchanged = _change_report(config_root, manifest)
                stage_root, journal = _stage_runtime(config_root, manifest)
                _raise_first_unsafe_path(config_root)
                _publish_staged_runtime(config_root, stage_root, manifest, journal)
                _cleanup_runtime_transaction(config_root, stage_root)
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
        "recovered_transaction": recovered_transaction,
        "process_crash_recovery": PROCESS_CRASH_RECOVERY,
        "power_loss_durability": (
            "not_checked" if dry_run else POWER_LOSS_DURABILITY
        ),
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
        payload = {
            "ok": False,
            "config_root": str(config_root),
            "error": type(exc).__name__,
            "detail": str(exc),
            "process_crash_recovery": PROCESS_CRASH_RECOVERY,
            "power_loss_durability": (
                "unsupported"
                if isinstance(exc, WindowsDurabilityError)
                else "not_verified"
            ),
        }
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
