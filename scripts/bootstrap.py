#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from agent_memory_env import resolve_config_path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "templates" / "vault"
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class BootstrapPathSecurityError(OSError):
    """A vault target is redirected, non-regular, or changed during bootstrap."""


@dataclass(frozen=True)
class FileSnapshot:
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    digest: str


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0))
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _components(path: Path) -> list[Path]:
    absolute = Path(os.path.abspath(path))
    anchor = Path(absolute.anchor)
    result = [anchor]
    cursor = anchor
    for part in absolute.parts[1:]:
        cursor = cursor / part
        result.append(cursor)
    return result


def _assert_lexically_inside(root: Path, target: Path) -> None:
    normalized_root = os.path.normcase(os.path.normpath(os.fspath(root)))
    normalized_target = os.path.normcase(os.path.normpath(os.fspath(target)))
    try:
        contained = os.path.commonpath((normalized_root, normalized_target)) == normalized_root
    except ValueError:
        contained = False
    if not contained:
        raise BootstrapPathSecurityError(f"target escapes vault root: {target}")


def _lstat_secure_path(
    root: Path,
    target: Path,
    *,
    expected_kind: str,
    allow_missing: bool,
) -> os.stat_result | None:
    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(target))
    _assert_lexically_inside(root, target)
    for index, component in enumerate(_components(target)):
        final = index == len(_components(target)) - 1
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise BootstrapPathSecurityError(f"vault path is missing: {component}")
        except OSError as exc:
            raise BootstrapPathSecurityError(
                f"vault path metadata failed: {component} ({type(exc).__name__})"
            ) from exc
        if _is_reparse(metadata):
            raise BootstrapPathSecurityError(
                f"vault path must not be a symlink or reparse point: {component}"
            )
        if not final and not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapPathSecurityError(
                f"vault path parent is not a directory: {component}"
            )
        if final and expected_kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapPathSecurityError(
                f"vault directory target is not a directory: {component}"
            )
        if final and expected_kind == "file" and not stat.S_ISREG(metadata.st_mode):
            raise BootstrapPathSecurityError(
                f"vault file target is not a regular file: {component}"
            )
    return metadata


def _ensure_secure_directory(path: Path) -> None:
    for component in _components(path):
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            component.mkdir()
            metadata = os.lstat(component)
        except OSError as exc:
            raise BootstrapPathSecurityError(
                f"vault directory metadata failed: {component} ({type(exc).__name__})"
            ) from exc
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapPathSecurityError(
                f"vault directory must not be redirected: {component}"
            )


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _stable_file_snapshot(root: Path, target: Path) -> FileSnapshot | None:
    before = _lstat_secure_path(
        root,
        target,
        expected_kind="file",
        allow_missing=True,
    )
    if before is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise BootstrapPathSecurityError(
            f"vault file open failed: {target} ({type(exc).__name__})"
        ) from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            _is_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _metadata_identity(opened) != _metadata_identity(before)
        ):
            raise BootstrapPathSecurityError(f"vault file changed before read: {target}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = _lstat_secure_path(
        root,
        target,
        expected_kind="file",
        allow_missing=False,
    )
    assert after_path is not None
    before_state = (
        _metadata_identity(before),
        int(before.st_size),
        int(before.st_mtime_ns),
    )
    after_state = (
        _metadata_identity(after_open),
        int(after_open.st_size),
        int(after_open.st_mtime_ns),
    )
    path_state = (
        _metadata_identity(after_path),
        int(after_path.st_size),
        int(after_path.st_mtime_ns),
    )
    if before_state != after_state or before_state != path_state:
        raise BootstrapPathSecurityError(f"vault file changed while reading: {target}")
    return FileSnapshot(
        identity=before_state[0],
        size=before_state[1],
        mtime_ns=before_state[2],
        digest=digest.hexdigest(),
    )


def _assert_snapshot_unchanged(
    root: Path,
    target: Path,
    expected: FileSnapshot | None,
) -> None:
    actual = _stable_file_snapshot(root, target)
    if actual != expected:
        raise BootstrapPathSecurityError(f"vault file changed before replace: {target}")


def _directory_identity(root: Path, directory: Path) -> tuple[int, int]:
    metadata = _lstat_secure_path(
        root,
        directory,
        expected_kind="directory",
        allow_missing=False,
    )
    assert metadata is not None
    return _metadata_identity(metadata)


@contextlib.contextmanager
def _vault_write_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".agent-memory-bootstrap.lock"
    _lstat_secure_path(root, lock_path, expected_kind="file", allow_missing=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise BootstrapPathSecurityError(
            f"another bootstrap writer holds the vault lock: {lock_path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise BootstrapPathSecurityError(f"vault lock is unsafe: {lock_path}")
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_template(
    root: Path,
    target: Path,
    content: bytes,
    expected: FileSnapshot | None,
    source_mode: int,
    after_cas_before_replace: Callable[[Path], None] | None = None,
) -> None:
    _ensure_secure_directory(target.parent)
    parent_identity = _directory_identity(root, target.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    backup_descriptor, backup_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".backup",
        dir=target.parent,
    )
    os.close(backup_descriptor)
    backup = Path(backup_name)
    backup.unlink()
    backup_captured = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            temporary.chmod(source_mode & 0o777)
        _assert_snapshot_unchanged(root, target, expected)
        if after_cas_before_replace is not None:
            after_cas_before_replace(target)
        if _directory_identity(root, target.parent) != parent_identity:
            raise BootstrapPathSecurityError(
                f"vault parent changed before replace: {target.parent}"
            )
        try:
            os.lstat(target)
        except FileNotFoundError:
            actual = None
        else:
            os.replace(target, backup)
            backup_captured = True
            try:
                actual = _stable_file_snapshot(root, backup)
            except BootstrapPathSecurityError:
                os.replace(backup, target)
                backup_captured = False
                raise BootstrapPathSecurityError(
                    f"actual vault object was unsafe before replace: {target}"
                )
        if actual != expected:
            if backup_captured:
                os.replace(backup, target)
                backup_captured = False
            raise BootstrapPathSecurityError(
                f"actual vault object changed before replace: {target}"
            )
        if _directory_identity(root, target.parent) != parent_identity:
            if backup_captured:
                os.replace(backup, target)
                backup_captured = False
            raise BootstrapPathSecurityError(
                f"vault parent changed during replace: {target.parent}"
            )
        if _stable_file_snapshot(root, target) is not None:
            if backup_captured:
                backup.unlink(missing_ok=True)
                backup_captured = False
            raise BootstrapPathSecurityError(
                f"vault target was recreated before replace: {target}"
            )
        os.replace(temporary, target)
        installed = _stable_file_snapshot(root, target)
        if installed is None or installed.digest != hashlib.sha256(content).hexdigest():
            if backup_captured:
                os.replace(backup, target)
                backup_captured = False
            raise BootstrapPathSecurityError(
                f"vault file verification failed after replace: {target}"
            )
        if backup_captured:
            backup.unlink()
            backup_captured = False
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if backup_captured:
            try:
                if not target.exists():
                    os.replace(backup, target)
                    backup_captured = False
            except OSError:
                pass
        try:
            backup.unlink()
        except FileNotFoundError:
            pass


def expand_path(raw: str) -> Path:
    return resolve_config_path(raw, lexical=True)


def replacements(args: argparse.Namespace) -> dict[str, str]:
    return {
        "{{USER_ID}}": args.user_id,
        "{{AGENT_ID}}": args.agent_id,
        "{{APP_ID}}": args.app_id,
        "{{STATE_DB}}": str(expand_path(args.state_db)),
    }


def render_text(text: str, mapping: dict[str, str]) -> str:
    for key, value in mapping.items():
        text = text.replace(key, value)
    return text


def _copy_template_locked(
    target_root: Path,
    mapping: dict[str, str],
    overwrite: bool,
    after_cas_before_replace: Callable[[Path], None] | None,
) -> tuple[list[Path], list[Path]]:
    written: list[Path] = []
    skipped: list[Path] = []
    sources = sorted(TEMPLATE_ROOT.rglob("*"))
    snapshots: dict[Path, FileSnapshot | None] = {}
    # Inspect the full destination closure before the first write so a late
    # junction cannot leave earlier template files partially installed.
    for source in sources:
        relative = source.relative_to(TEMPLATE_ROOT)
        target = target_root / relative
        if source.is_dir():
            _lstat_secure_path(
                target_root,
                target,
                expected_kind="directory",
                allow_missing=True,
            )
            continue
        snapshots[relative] = _stable_file_snapshot(target_root, target)

    for source in sources:
        relative = source.relative_to(TEMPLATE_ROOT)
        target = target_root / relative
        if source.is_dir():
            _ensure_secure_directory(target)
            continue
        snapshot = snapshots[relative]
        if snapshot is not None and not overwrite:
            skipped.append(relative)
            continue
        if source.suffix.lower() in {".md", ".txt"}:
            text = source.read_text(encoding="utf-8")
            content = render_text(text, mapping).encode("utf-8")
        else:
            content = source.read_bytes()
        _atomic_write_template(
            target_root,
            target,
            content,
            snapshot,
            source.stat().st_mode,
            after_cas_before_replace,
        )
        written.append(relative)
    return written, skipped


def copy_template(
    target_root: Path,
    mapping: dict[str, str],
    overwrite: bool,
    *,
    after_cas_before_replace: Callable[[Path], None] | None = None,
) -> tuple[list[Path], list[Path]]:
    with _vault_write_lock(target_root):
        return _copy_template_locked(
            target_root,
            mapping,
            overwrite,
            after_cas_before_replace,
        )


def write_env(args: argparse.Namespace, memory_root: Path) -> None:
    env_path = REPO_ROOT / ".env"
    if env_path.exists() and not args.overwrite_env:
        print(f"SKIP env_exists {env_path}")
        return
    config_root = expand_path(args.config_root)
    git_root = expand_path(args.git_root) if args.git_root else memory_root
    content = "\n".join(
        [
            f"AGENT_MEMORY_ROOT={memory_root}",
            f"AGENT_MEMORY_GIT_ROOT={git_root}",
            f"AGENT_MEMORY_CONFIG_ROOT={config_root}",
            f"AGENT_MEMORY_STATE_DB={expand_path(args.state_db)}",
            f"AGENT_MEMORY_USER_ID={args.user_id}",
            f"AGENT_MEMORY_AGENT_ID={args.agent_id}",
            f"AGENT_MEMORY_APP_ID={args.app_id}",
            f"AGENT_MEMORY_AUDIT_DB={config_root / 'audit_decisions.sqlite'}",
            f"AGENT_MEMORY_CLOSEOUT_LOG={config_root / 'logs' / 'closeout.jsonl'}",
            f"AGENT_MEMORY_AUDIT_RUN_LOG={config_root / 'logs' / 'audit_runs.jsonl'}",
            f"AGENT_MEMORY_AUDIT_REPORT={config_root / 'reports' / 'latest-audit.json'}",
            "",
        ]
    )
    env_path.write_text(content, encoding="utf-8")
    print(f"OK wrote_env {env_path}")


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            ["git", "-C", str(root), *args],
            127,
            "",
            str(exc),
        )


def existing_git_root(path: Path) -> Path | None:
    result = run_git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return resolve_config_path(result.stdout.strip())


def initialize_git_baseline(
    memory_root: Path,
    git_root: Path,
    *,
    target_was_nonempty: bool,
    written_paths: list[Path],
) -> dict[str, str]:
    """Create a baseline only when this invocation owns a new empty vault.

    Existing repositories, parent repositories, and pre-populated vaults are
    deliberately left untouched. In particular, skipped template files are
    never staged by this function.
    """

    if shutil.which("git") is None:
        return {"status": "error", "detail": "git_not_found"}
    if git_root != memory_root:
        return {"status": "skipped", "detail": "external_git_root"}

    discovered_root = existing_git_root(memory_root)
    if discovered_root is not None and discovered_root != memory_root:
        return {"status": "skipped", "detail": "external_git_root"}
    if discovered_root == memory_root:
        head = run_git(memory_root, "rev-parse", "--verify", "HEAD")
        if head.returncode == 0:
            return {"status": "existing", "detail": head.stdout.strip()}
        return {"status": "skipped", "detail": "existing_repository_without_head"}
    if target_was_nonempty:
        return {"status": "skipped", "detail": "preexisting_vault"}
    if not written_paths:
        return {"status": "skipped", "detail": "no_template_files_written"}

    initialized = run_git(memory_root, "init", "-q")
    if initialized.returncode != 0:
        return {
            "status": "error",
            "detail": initialized.stderr.strip() or "git_init_failed",
        }
    relative_paths = [path.as_posix() for path in written_paths]
    staged = run_git(memory_root, "add", "--", *relative_paths)
    if staged.returncode != 0:
        return {
            "status": "error",
            "detail": staged.stderr.strip() or "git_add_failed",
        }
    commit = run_git(
        memory_root,
        "-c",
        "user.name=Agent Memory Vault",
        "-c",
        "user.email=agent-memory@localhost",
        "commit",
        "-qm",
        "Initialize Agent Memory Vault",
    )
    if commit.returncode != 0:
        return {
            "status": "error",
            "detail": commit.stderr.strip() or "git_commit_failed",
        }
    head = run_git(memory_root, "rev-parse", "HEAD")
    return {
        "status": "created",
        "detail": head.stdout.strip() if head.returncode == 0 else "baseline_committed",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a local Agent Memory Vault from the public template.")
    parser.add_argument("--memory-root", required=True, help="Target local memory vault path.")
    parser.add_argument(
        "--state-db",
        default="$HOME/.config/agent-memory/state.sqlite",
        help="SQLite state database path.",
    )
    parser.add_argument(
        "--config-root",
        default="$HOME/.config/agent-memory",
        help="Local config/state directory for logs, audit decisions, and derived indexes.",
    )
    parser.add_argument(
        "--git-root",
        default="",
        help="Git root that contains the memory vault. Defaults to --memory-root.",
    )
    parser.add_argument("--user-id", default="demo-user", help="Non-secret user identifier.")
    parser.add_argument("--agent-id", default="shared", help="Default memory scope: shared, codex, or claude.")
    parser.add_argument("--app-id", default="agent-memory", help="Application/workspace identifier.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing template files in the target vault. No files are deleted.",
    )
    parser.add_argument("--write-env", action="store_true", help="Write a local .env file in this repo.")
    parser.add_argument("--overwrite-env", action="store_true", help="Overwrite an existing local .env file.")
    git_group = parser.add_mutually_exclusive_group()
    git_group.add_argument(
        "--init-git",
        dest="init_git",
        action="store_true",
        help="Create an initial Git baseline for a new empty vault (default).",
    )
    git_group.add_argument(
        "--no-init-git",
        dest="init_git",
        action="store_false",
        help="Do not initialize Git.",
    )
    parser.set_defaults(init_git=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not TEMPLATE_ROOT.is_dir():
        raise SystemExit(f"Template root not found: {TEMPLATE_ROOT}")

    memory_root = expand_path(args.memory_root)
    _ensure_secure_directory(memory_root)
    target_was_nonempty = any(os.scandir(memory_root))
    written, skipped = copy_template(memory_root, replacements(args), args.overwrite)
    print(f"memory_root={memory_root}")
    print(f"created_or_updated_files={len(written)}")
    print(f"skipped_existing_files={len(skipped)}")

    if args.write_env:
        write_env(args, memory_root)

    git_root = expand_path(args.git_root) if args.git_root else memory_root
    git_baseline = (
        initialize_git_baseline(
            memory_root,
            git_root,
            target_was_nonempty=target_was_nonempty,
            written_paths=written,
        )
        if args.init_git
        else {"status": "skipped", "detail": "disabled"}
    )
    print(f"git_baseline={git_baseline['status']} {git_baseline['detail']}")
    if git_baseline["status"] == "error":
        return 2

    print("next_commands:")
    if os.name == "nt":
        print("  # Runtime TOML/.env is loaded by Python; no PowerShell import is required")
    else:
        print("  source .env")
    print(f"  {sys.executable} scripts/agent_memory_evolution.py --init --scan --report")
    print(f"  {sys.executable} scripts/agent_memory_index.py --init --scan --report")
    print(f"  {sys.executable} scripts/agent_memory_closeout.py --dry-run")
    print(f"  {sys.executable} scripts/agent_memory_check.py")
    print(f"  {sys.executable} scripts/agent_memory_doctor.py")
    print("optional_semantic_retrieval:")
    print(f"  {sys.executable} -m pip install -r requirements-vector.lock")
    print(f"  {sys.executable} scripts/agent_memory_zvec_index.py --init --scan --prune")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapPathSecurityError as exc:
        print(f"BootstrapPathSecurityError: {exc}", file=sys.stderr)
        raise SystemExit(2)
