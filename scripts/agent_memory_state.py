from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
POSIX_MODE_ENFORCED = os.name == "posix" and hasattr(os, "fchmod")

# Windows ACL support (2026-11-24). Previously Windows only reported
# "windows_acl_unverified" and never looked at the DACL, so a private runtime file
# inheriting BUILTIN\Users / Authenticated Users could hold the state DB, search
# log and audit ledger while doctor still said "not verified". We now read the
# DACL through PowerShell (SID-based, locale independent) and can tighten it.
WINDOWS_ACL_SUPPORTED = os.name == "nt"
# SIDs allowed to keep access on a private runtime file:
# current user, LocalSystem, BUILTIN\\Administrators.
WINDOWS_TRUSTED_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544", "S-1-3-4"})
_CURRENT_USER_SID: str | None = None
# Paths whose DACL this process already verified as private. Avoids re-spawning
# PowerShell for every SQLite connection in one closeout run.
_WINDOWS_ACL_PRIVATE_CACHE: set[str] = set()
WINDOWS_ACL_QUERY = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "$me=([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value;"
    "$out=@();"
    "foreach($p in ($env:AGENT_MEMORY_ACL_TARGET -split '\\|')){"
    "  if(-not (Test-Path -LiteralPath $p)){ continue }"
    "  $acl=Get-Acl -LiteralPath $p;"
    "  $out += @{path=$p; sddl=$acl.Sddl; current_user=$me;"
    "    rules=@($acl.Access | ForEach-Object {"
    "      @{sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;"
    "        rights=$_.FileSystemRights.ToString(); type=$_.AccessControlType.ToString()}"
    "    })}"
    "}"
    "ConvertTo-Json -Compress -Depth 5 -InputObject @($out)"
)
WINDOWS_ACL_HARDEN = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "$p=$env:AGENT_MEMORY_ACL_TARGET;"
    "$me=([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value;"
    "$acl=New-Object System.Security.AccessControl.FileSecurity;"
    "$acl.SetAccessRuleProtection($true,$false);"
    "foreach($sid in @($me,'S-1-5-18','S-1-5-32-544')){"
    "  $id=New-Object System.Security.Principal.SecurityIdentifier($sid);"
    "  $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("
    "    $id,'FullControl','Allow')))"
    "}"
    "Set-Acl -LiteralPath $p -AclObject $acl"
)


def _powershell() -> list[str]:
    """PowerShell invocation prefix (pwsh preferred, then Windows PowerShell)."""

    for candidate in ("pwsh", "powershell"):
        if shutil.which(candidate):
            return [candidate, "-NoProfile", "-NonInteractive", "-Command"]
    # Fall back to the well-known install path: PATH can be minimal inside host
    # installers/hooks even though PowerShell is present.
    for candidate in (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ):
        if os.path.isfile(candidate):
            return [candidate, "-NoProfile", "-NonInteractive", "-Command"]
    return []


def _current_user_sid() -> str:
    """Return the current Windows user SID, or "" when it cannot be read.

    Cached: each lookup spawns PowerShell (~200 ms), and closeout/doctor call the
    ACL check for several files. The SID cannot change inside one process.
    """

    global _CURRENT_USER_SID
    if _CURRENT_USER_SID is not None:
        return _CURRENT_USER_SID
    if not WINDOWS_ACL_SUPPORTED:
        return ""
    prefix = _powershell()
    if not prefix:
        return ""
    try:
        result = subprocess.run(
            [*prefix, "([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value"],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    sid = (result.stdout or "").strip() if result.returncode == 0 else ""
    _CURRENT_USER_SID = sid
    return sid


def _acl_verdict(payload: dict[str, Any], current_user: str) -> dict[str, Any]:
    """Turn one ACL payload entry into a privacy verdict."""

    rules = payload.get("rules") or []
    sddl = str(payload.get("sddl") or "")
    trusted = set(WINDOWS_TRUSTED_SIDS)
    if current_user:
        trusted.add(current_user)
    untrusted: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type", "Allow")) != "Allow":
            continue
        sid = str(rule.get("sid") or "")
        if sid and sid not in trusted:
            untrusted.append({"sid": sid, "rights": str(rule.get("rights") or "")})
    # D:PAI marks a protected DACL (inheritance blocked).
    protected = "D:PAI" in sddl or "D:P" in sddl
    return {
        "private": not untrusted and protected,
        "inheritance_protection": protected,
        "inheritance_protected": protected,
        "untrusted_grantees": untrusted,
        "current_user_sid": current_user,
        "rule_count": len(rules),
    }


def windows_acl_reports(paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Inspect several DACLs in ONE PowerShell call.

    Batching matters: each spawn costs ~420 ms, and one SQLite connect used to
    probe the database plus every sidecar separately (~2 s per connect), which
    made closeout miss its own --lock-timeout under concurrency.
    """

    if not WINDOWS_ACL_SUPPORTED or not paths:
        return {}
    prefix = _powershell()
    if not prefix:
        return {}
    env = dict(os.environ)
    env["AGENT_MEMORY_ACL_TARGET"] = "|".join(str(p) for p in paths)
    result = None
    # One retry: a transient PowerShell start failure (the Windows installer can
    # be finishing other work) must not silently downgrade the verdict to
    # "unverified", which would make doctor report a false warning.
    for attempt in range(2):
        try:
            result = subprocess.run(
                [*prefix, WINDOWS_ACL_QUERY],
                capture_output=True, text=True, timeout=120, env=env,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            break
        if attempt == 0:
            time.sleep(0.2)
    if result is None or result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {}
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path") or "")
        if not raw_path:
            continue
        current_user = str(entry.get("current_user") or "") or _current_user_sid()
        if current_user:
            global _CURRENT_USER_SID
            _CURRENT_USER_SID = current_user
        verdict = _acl_verdict(entry, current_user)
        if verdict["private"]:
            _WINDOWS_ACL_PRIVATE_CACHE.add(raw_path)
        else:
            # A negative verdict must not stick: the file may be hardened a
            # moment later by the installer or a concurrent closeout.
            _WINDOWS_ACL_PRIVATE_CACHE.discard(raw_path)
        out[raw_path] = verdict
    return out


def windows_acl_report(path: Path) -> dict[str, Any] | None:
    """Inspect a single file's DACL on Windows (thin wrapper over the batch API).

    Returns ``None`` when the platform or PowerShell is unavailable so callers
    keep the previous "unverified" behaviour instead of inventing a verdict.
    ``private`` is True only when every allow-rule grants to the current user,
    LocalSystem or BUILTIN\\Administrators, and inheritance is disabled.
    """

    reports = windows_acl_reports([path])
    return reports.get(str(path))


def harden_windows_acl(path: Path) -> bool:
    """Replace the DACL with user + SYSTEM + Administrators only. Idempotent.

    Reads the DACL first and returns early when it is already private: spawning
    PowerShell to *write* an identical ACL on every open made closeout slow
    enough to miss its own lock timeout.
    """

    if not WINDOWS_ACL_SUPPORTED:
        return False
    current = windows_acl_report(path)
    if current is not None and current.get("private"):
        _WINDOWS_ACL_PRIVATE_CACHE.add(str(path))
        return True
    prefix = _powershell()
    if not prefix:
        return False
    env = dict(os.environ)
    env["AGENT_MEMORY_ACL_TARGET"] = str(path)
    try:
        result = subprocess.run(
            [*prefix, WINDOWS_ACL_HARDEN],
            capture_output=True, text=True, timeout=60, env=env, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0:
        _WINDOWS_ACL_PRIVATE_CACHE.add(str(path))
        return True
    return False


PERMISSION_MODEL = "posix_mode" if POSIX_MODE_ENFORCED else (
    "windows_acl" if WINDOWS_ACL_SUPPORTED else "windows_acl_unverified"
)


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


def _inspect_private_file(
    path: Path,
    *,
    required: bool,
    acl_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
    if WINDOWS_ACL_SUPPORTED:
        if str(path) in _WINDOWS_ACL_PRIVATE_CACHE:
            return []
        acl = acl_report if acl_report is not None else windows_acl_report(path)
        # acl is None when PowerShell is unavailable: stay "unverified" rather
        # than claim either safety or a violation we could not observe.
        if acl is not None and not acl["private"]:
            return [
                {
                    "path": str(path),
                    "reason": "acl",
                    "inheritance_protected": acl["inheritance_protected"],
                    "untrusted_grantees": acl["untrusted_grantees"],
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
    # Resolve every ACL for this database family in a single PowerShell call.
    pending = [
        path
        for path in paths
        if WINDOWS_ACL_SUPPORTED
        and str(path) not in _WINDOWS_ACL_PRIVATE_CACHE
        and (path.exists() or path.is_symlink())
    ]
    acl_reports = windows_acl_reports(pending) if pending else {}
    for index, path in enumerate(paths):
        issues.extend(
            _inspect_private_file(
                path,
                required=require_database and index == 0,
                acl_report=acl_reports.get(str(path)),
            )
        )
    # A verified private Windows DACL is an enforced boundary too, so
    # mode_enforced is True there as well; the model name says which one applied.
    acl_verified = WINDOWS_ACL_SUPPORTED and not issues
    enforced = POSIX_MODE_ENFORCED or acl_verified
    warnings: list[str] = []
    if not enforced:
        warnings.append(
            "windows_acl_unverified" if WINDOWS_ACL_SUPPORTED else "acl_not_supported"
        )
    return {
        "ok": not issues,
        "permission_model": PERMISSION_MODEL,
        "mode_enforced": enforced,
        "warnings": warnings,
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
    # NOTE: do not touch the DACL here. harden_private_file is called by the
    # Windows installer inside a strict path-snapshot window that only allows the
    # runtime scripts/manifest to change; rewriting a state DB ACL there trips
    # "installer_preflight=error reason=path_changed". DACL hardening happens in
    # harden_sqlite_files (after the snapshot) and on secure_sqlite_connect.
    return path


def harden_sqlite_files(raw_path: str | os.PathLike[str], *, require_database: bool = True) -> None:
    for index, path in enumerate(sqlite_paths(raw_path)):
        # Cheap existence gate first: sidecars usually do not exist, and probing a
        # missing path with PowerShell would cost ~450 ms for nothing.
        if not path.exists() and not path.is_symlink():
            if require_database and index == 0:
                harden_private_file(path, required=True)
            continue
        harden_private_file(path, required=require_database and index == 0)
        # DACL tightening lives here (not in harden_private_file) so the Windows
        # installer's path-snapshot window stays untouched.
        if WINDOWS_ACL_SUPPORTED and str(path) not in _WINDOWS_ACL_PRIVATE_CACHE:
            harden_windows_acl(path)


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
    # Freshly created files inherit a permissive DACL on Windows. Tighten before
    # the first permission report so a just-created database is never reported as
    # unsafe (that race made the Windows installer's own doctor step fail).
    if WINDOWS_ACL_SUPPORTED and str(path) not in _WINDOWS_ACL_PRIVATE_CACHE:
        harden_windows_acl(path)
    report = sqlite_permission_report(path)
    # A permissive DACL is repairable, not fatal: tighten it when we are allowed
    # to repair, and only refuse to open when the path shape itself is unsafe
    # (missing / symlink / not regular). Failing hard on "acl" would make every
    # fresh temp database unusable on Windows.
    repairable = {"mode", "acl"}
    non_mode_issues = [item for item in report["issues"] if item.get("reason") not in repairable]
    if non_mode_issues:
        raise StateSecurityError(f"unsafe SQLite path: {non_mode_issues[0]['reason']} {non_mode_issues[0]['path']}")
    if repair_permissions:
        # Only paths the report flagged need a write; already-private ones are in
        # the process cache, so this costs no extra PowerShell spawn.
        flagged = {
            item["path"] for item in report["issues"] if item.get("reason") in {"mode", "acl"}
        }
        for index, candidate in enumerate(sqlite_paths(path)):
            if str(candidate) not in flagged and candidate.exists():
                continue
            harden_private_file(candidate, required=index == 0)

    def open_connection(target: str, *, uri: bool, query_only: bool) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                target,
                timeout=timeout,
                factory=PrivateSQLiteConnection,
                uri=uri,
            )
            connection._agent_memory_path = path  # type: ignore[attr-defined]
            connection._agent_memory_repair_permissions = repair_permissions  # type: ignore[attr-defined]
            if row_factory is not None:
                connection.row_factory = row_factory
            if query_only:
                connection.execute("PRAGMA query_only=ON")
            for pragma in pragmas:
                connection.execute(pragma)
                if query_only:
                    connection.execute("PRAGMA query_only=ON")
            if query_only:
                # Force WAL/SHM attachment now so a mode=ro incompatibility can
                # fall back without surfacing later during a caller query.
                connection.execute("PRAGMA schema_version").fetchone()
            if repair_permissions:
                harden_sqlite_files(path)
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    if not read_only:
        return open_connection(str(path), uri=False, query_only=False)

    read_only_target = f"{path.as_uri()}?mode=ro"
    try:
        return open_connection(read_only_target, uri=True, query_only=True)
    except sqlite3.OperationalError as exc:
        error_code = getattr(exc, "sqlite_errorcode", None)
        cant_open = (
            isinstance(error_code, int)
            and (error_code & 0xFF) == getattr(sqlite3, "SQLITE_CANTOPEN", 14)
        ) or "unable to open database file" in str(exc).lower()
        if not cant_open:
            raise

    # mode=rw still refuses to create a missing database; query_only prevents
    # SQL writes while allowing SQLite builds that cannot attach WAL via mode=ro.
    read_existing_target = f"{path.as_uri()}?mode=rw"
    return open_connection(read_existing_target, uri=True, query_only=True)


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
