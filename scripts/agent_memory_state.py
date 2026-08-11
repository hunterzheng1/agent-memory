from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Iterable


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
POSIX_MODE_ENFORCED = os.name == "posix" and hasattr(os, "fchmod")
PERMISSION_MODEL = "posix_mode" if POSIX_MODE_ENFORCED else "windows_acl_unverified"


def _harden_mode(path: Path, mode: int) -> None:
    """Apply a POSIX mode only where it is an enforceable access boundary."""

    if POSIX_MODE_ENFORCED:
        os.chmod(path, mode, follow_symlinks=False)


def _harden_descriptor(descriptor: int, mode: int) -> None:
    if POSIX_MODE_ENFORCED:
        os.fchmod(descriptor, mode)


class StateSecurityError(OSError):
    """Raised when a private runtime path is unsafe to open."""


def absolute_path(raw_path: str | os.PathLike[str]) -> Path:
    """Return an absolute path without resolving away the final symlink."""

    return Path(os.path.abspath(os.path.expanduser(os.fspath(raw_path))))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _assert_directory(path: Path) -> None:
    if path.is_symlink():
        raise StateSecurityError(f"private directory must not be a symlink: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise StateSecurityError(f"private directory path is not a directory: {path}")


def ensure_private_directory(
    raw_path: str | os.PathLike[str],
    *,
    harden_existing: bool = False,
) -> Path:
    """Create missing directories with private semantics (POSIX mode 0700).

    Existing ancestors are left unchanged unless ``harden_existing`` is true for
    the requested leaf. The requested leaf itself may never be a symlink. On
    Windows this validates path shape without claiming POSIX-mode or ACL parity.
    """

    path = absolute_path(raw_path)
    missing: list[Path] = []
    cursor = path
    while True:
        if cursor.is_symlink():
            raise StateSecurityError(f"private directory must not be a symlink: {cursor}")
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise StateSecurityError(f"cannot locate an existing parent for: {path}")
            cursor = parent
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise StateSecurityError(f"private directory ancestor is not a directory: {cursor}")
        break

    for directory in reversed(missing):
        try:
            os.mkdir(directory, PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            _assert_directory(directory)
        _harden_mode(directory, PRIVATE_DIRECTORY_MODE)

    _assert_directory(path)
    if harden_existing:
        _harden_mode(path, PRIVATE_DIRECTORY_MODE)
    return path


def sqlite_paths(raw_path: str | os.PathLike[str]) -> tuple[Path, ...]:
    path = absolute_path(raw_path)
    return (path, *(Path(f"{path}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES))


def _inspect_private_file(path: Path, *, required: bool) -> list[dict[str, Any]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ([{"path": str(path), "reason": "missing"}] if required else [])
    if stat.S_ISLNK(metadata.st_mode):
        return [{"path": str(path), "reason": "symlink"}]
    if not stat.S_ISREG(metadata.st_mode):
        return [{"path": str(path), "reason": "not_regular"}]
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if POSIX_MODE_ENFORCED and actual_mode != PRIVATE_FILE_MODE:
        return [
            {
                "path": str(path),
                "reason": "mode",
                "expected_mode": "0600",
                "actual_mode": f"{actual_mode:04o}",
            }
        ]
    return []


def sqlite_permission_report(
    raw_path: str | os.PathLike[str],
    *,
    require_database: bool = True,
) -> dict[str, Any]:
    paths = sqlite_paths(raw_path)
    issues: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        issues.extend(_inspect_private_file(path, required=require_database and index == 0))
    return {
        "ok": not issues,
        "permission_model": PERMISSION_MODEL,
        "mode_enforced": POSIX_MODE_ENFORCED,
        "warnings": [] if POSIX_MODE_ENFORCED else ["windows_acl_unverified"],
        "database": str(paths[0]),
        "checked": [str(path) for path in paths if path.exists() or path.is_symlink()],
        "issues": issues,
    }


def harden_private_file(raw_path: str | os.PathLike[str], *, required: bool = True) -> Path:
    path = absolute_path(raw_path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise StateSecurityError(f"private file is missing: {path}")
        return path
    if stat.S_ISLNK(metadata.st_mode):
        raise StateSecurityError(f"private file must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise StateSecurityError(f"private file is not regular: {path}")
    _harden_mode(path, PRIVATE_FILE_MODE)
    return path


def harden_sqlite_files(raw_path: str | os.PathLike[str], *, require_database: bool = True) -> None:
    for index, path in enumerate(sqlite_paths(raw_path)):
        harden_private_file(path, required=require_database and index == 0)


def _create_private_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        return
    try:
        _harden_descriptor(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)


class PrivateSQLiteConnection(sqlite3.Connection):
    _agent_memory_path: Path | None = None
    _agent_memory_repair_permissions: bool = True

    def _harden(self) -> None:
        if self._agent_memory_path is not None and self._agent_memory_repair_permissions:
            harden_sqlite_files(self._agent_memory_path)

    def commit(self) -> None:
        super().commit()
        self._harden()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
            self._harden()
            return bool(result)
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._harden()
        finally:
            super().close()


def secure_sqlite_connect(
    raw_path: str | os.PathLike[str],
    *,
    timeout: float = 5.0,
    create: bool = True,
    repair_permissions: bool = True,
    read_only: bool = False,
    row_factory: Any | None = None,
    pragmas: Iterable[str] = (),
) -> sqlite3.Connection:
    """Open a private SQLite database without following a final symlink.

    Normal runtime callers repair existing mode drift before opening. Diagnostic
    callers may pass ``repair_permissions=False`` after recording the drift.
    """

    path = absolute_path(raw_path)
    if read_only:
        # A diagnostic/dry-run open must not create or chmod anything.  The
        # writable path below deliberately creates/hardens private state; the
        # read-only path only verifies that the existing parent is usable.
        _assert_directory(path.parent)
        if not path.parent.exists():
            raise StateSecurityError(f"SQLite parent directory is missing: {path.parent}")
    else:
        ensure_private_directory(path.parent, harden_existing=True)
    if path.is_symlink():
        raise StateSecurityError(f"SQLite database must not be a symlink: {path}")
    if read_only:
        create = False
        repair_permissions = False
    if not path.exists():
        if not create:
            raise StateSecurityError(f"SQLite database is missing: {path}")
        _create_private_file(path)
    report = sqlite_permission_report(path)
    non_mode_issues = [item for item in report["issues"] if item.get("reason") != "mode"]
    if non_mode_issues:
        raise StateSecurityError(f"unsafe SQLite path: {non_mode_issues[0]['reason']} {non_mode_issues[0]['path']}")
    if repair_permissions:
        harden_sqlite_files(path)

    connection_target = f"{path.as_uri()}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(
        connection_target,
        timeout=timeout,
        factory=PrivateSQLiteConnection,
        uri=read_only,
    )
    connection._agent_memory_path = path  # type: ignore[attr-defined]
    connection._agent_memory_repair_permissions = repair_permissions  # type: ignore[attr-defined]
    if row_factory is not None:
        connection.row_factory = row_factory
    try:
        if read_only:
            connection.execute("PRAGMA query_only=ON")
        for pragma in pragmas:
            connection.execute(pragma)
        if repair_permissions:
            harden_sqlite_files(path)
    except Exception:
        connection.close()
        raise
    return connection


def secure_append_text(raw_path: str | os.PathLike[str], text: str) -> Path:
    """Append UTF-8 text to a private regular file without following symlinks."""

    path = absolute_path(raw_path)
    ensure_private_directory(path.parent)
    if path.is_symlink():
        raise StateSecurityError(f"private log must not be a symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StateSecurityError(f"private log is not a regular file: {path}")
        _harden_descriptor(descriptor, PRIVATE_FILE_MODE)
        payload = text.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("private log append made no progress")
            offset += written
    finally:
        os.close(descriptor)
    return path
