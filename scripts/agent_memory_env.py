from __future__ import annotations

import os
import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 fallback for import-time clarity
    tomllib = None  # type: ignore[assignment]


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PATH_DEFAULTS: dict[str, tuple[str, ...]] = {
    "CONFIG_ROOT": (),
    "STATE_DB": ("state.sqlite",),
    "AUDIT_DB": ("audit_decisions.sqlite",),
    "CLOSEOUT_LOG": ("logs", "closeout.jsonl"),
    "AUDIT_RUN_LOG": ("logs", "audit_runs.jsonl"),
    "AUDIT_REPORT": ("reports", "latest-audit.json"),
    "INVARIANTS": ("config", "system-invariants.json"),
    "VECTOR_DIR": ("zvec", "memory_chunks_embeddinggemma_768"),
    "ZVEC_LOCK": ("locks", "zvec.lock"),
    "MODEL_MANIFEST": ("models", "embeddinggemma-300m", "model-manifest.json"),
    "DEPENDENCY_LOCK": ("requirements-vector.lock",),
}
CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "ROOT": ("memory_root",),
    "GIT_ROOT": ("git_root",),
    "CONFIG_ROOT": ("config_root",),
    "STATE_DB": ("state_db",),
    "AUDIT_DB": ("audit_db",),
    "CLOSEOUT_LOG": ("closeout_log",),
    "AUDIT_RUN_LOG": ("audit_run_log",),
    "AUDIT_REPORT": ("audit_report",),
    "INVARIANTS": ("invariants_file",),
    "PYTHON": ("python",),
    "USER_ID": ("user_id",),
    "AGENT_ID": ("agent_id",),
    "APP_ID": ("app_id",),
    "VECTOR_DIR": ("semantic_retrieval", "vector_dir"),
    "EMBEDDING_MODEL": ("semantic_retrieval", "embedding_model"),
    "EMBEDDING_DIM": ("semantic_retrieval", "embedding_dim"),
    "EMBEDDING_DEVICE": ("semantic_retrieval", "embedding_device"),
    "ZVEC_PYTHON": ("semantic_retrieval", "python"),
    "ZVEC_LOCK": ("semantic_retrieval", "lock_path"),
    "REQUIRE_LOCAL_MODEL": ("semantic_retrieval", "require_local_model"),
    "MODEL_MANIFEST": ("semantic_retrieval", "model_manifest"),
    "MODEL_REVISION": ("semantic_retrieval", "model_revision"),
    "DEPENDENCY_LOCK": ("semantic_retrieval", "dependency_lock"),
}



def resolve_config_path(raw: str) -> Path:
    """Resolve config paths with Windows-safe $HOME / ~ / %VAR% expansion."""
    from agent_memory_paths import resolve_path

    return resolve_path(raw)


def config_path() -> Path:
    explicit = os.environ.get("AGENT_MEMORY_CONFIG_FILE", "").strip()
    if explicit:
        return resolve_config_path(explicit)
    return RUNTIME_ROOT / "config" / "agent-memory.toml"



@lru_cache(maxsize=1)
def load_dotenv() -> dict[str, str]:
    path = RUNTIME_ROOT / ".env"
    if not path.is_file():
        return {}
    payload: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                continue
            value = str(parsed)
        payload[key] = value
    return payload


def _strip_toml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _toml_array_is_complete(value: str) -> bool:
    quote: str | None = None
    escaped = False
    depth = 0
    for char in value:
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    return depth <= 0


_TOML_INTEGER = re.compile(
    r"[+-]?(?:0|[1-9](?:_?\d)*|0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|"
    r"0o[0-7](?:_?[0-7])*|0b[01](?:_?[01])*)"
)
_TOML_BASIC_ESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


class _TomlValueParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0

    def parse(self) -> object:
        value = self._parse_value()
        self._skip_whitespace()
        if self.index != len(self.text):
            raise ValueError(f"unexpected TOML value suffix at offset {self.index}")
        return value

    def _skip_whitespace(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in {" ", "\t"}:
            self.index += 1

    def _parse_value(self) -> object:
        self._skip_whitespace()
        if self.index >= len(self.text):
            raise ValueError("missing TOML value")
        char = self.text[self.index]
        if char == '"':
            return self._parse_basic_string()
        if char == "'":
            return self._parse_literal_string()
        if char == "[":
            return self._parse_array()

        start = self.index
        while (
            self.index < len(self.text)
            and self.text[self.index] not in {" ", "\t", ",", "]"}
        ):
            self.index += 1
        token = self.text[start:self.index]
        if token == "true":
            return True
        if token == "false":
            return False
        if _TOML_INTEGER.fullmatch(token):
            return int(token.replace("_", ""), 0)
        raise ValueError(f"unsupported TOML value: {token!r}")

    def _parse_basic_string(self) -> str:
        self.index += 1
        chars: list[str] = []
        while self.index < len(self.text):
            char = self.text[self.index]
            self.index += 1
            if char == '"':
                return "".join(chars)
            if char != "\\":
                if ord(char) < 0x20:
                    raise ValueError("unescaped control character in TOML basic string")
                chars.append(char)
                continue
            if self.index >= len(self.text):
                raise ValueError("unterminated TOML basic string escape")
            escape = self.text[self.index]
            self.index += 1
            if escape in _TOML_BASIC_ESCAPES:
                chars.append(_TOML_BASIC_ESCAPES[escape])
                continue
            if escape in {"u", "U"}:
                width = 4 if escape == "u" else 8
                digits = self.text[self.index:self.index + width]
                if len(digits) != width or not re.fullmatch(
                    rf"[0-9A-Fa-f]{{{width}}}", digits
                ):
                    raise ValueError("invalid TOML unicode escape")
                self.index += width
                codepoint = int(digits, 16)
                if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    raise ValueError("invalid TOML unicode scalar")
                chars.append(chr(codepoint))
                continue
            raise ValueError(f"invalid TOML basic string escape: \\{escape}")
        raise ValueError("unterminated TOML basic string")

    def _parse_literal_string(self) -> str:
        self.index += 1
        end = self.text.find("'", self.index)
        if end < 0:
            raise ValueError("unterminated TOML literal string")
        value = self.text[self.index:end]
        if any(ord(char) < 0x20 for char in value):
            raise ValueError("control character in TOML literal string")
        self.index = end + 1
        return value

    def _parse_array(self) -> list[object]:
        self.index += 1
        values: list[object] = []
        self._skip_whitespace()
        if self.index < len(self.text) and self.text[self.index] == "]":
            self.index += 1
            return values
        while True:
            values.append(self._parse_value())
            self._skip_whitespace()
            if self.index >= len(self.text):
                raise ValueError("unterminated TOML array")
            delimiter = self.text[self.index]
            self.index += 1
            if delimiter == "]":
                return values
            if delimiter != ",":
                raise ValueError(f"expected TOML array delimiter at offset {self.index - 1}")
            self._skip_whitespace()
            if self.index < len(self.text) and self.text[self.index] == "]":
                self.index += 1
                return values


def _parse_toml_value(raw_value: str) -> object:
    return _TomlValueParser(raw_value).parse()


def parse_toml_fallback(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    section: tuple[str, ...] = ()
    lines = iter(text.splitlines())
    for raw_line in lines:
        line = _strip_toml_comment(raw_line).strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = tuple(part.strip() for part in line[1:-1].split(".") if part.strip())
            continue
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith("[") and not _toml_array_is_complete(raw_value):
            value_lines = [raw_value]
            for continuation in lines:
                fragment = _strip_toml_comment(continuation).strip()
                if fragment:
                    value_lines.append(fragment)
                joined = " ".join(value_lines)
                if _toml_array_is_complete(joined):
                    break
            raw_value = " ".join(value_lines)
        value = _parse_toml_value(raw_value)
        target = payload
        for part in section:
            child = target.setdefault(part, {})
            if not isinstance(child, dict):
                break
            target = child
        else:
            target[key] = value
    return payload


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        if tomllib is not None:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
        else:
            payload = parse_toml_fallback(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def reset_config_cache() -> None:
    load_config.cache_clear()
    load_dotenv.cache_clear()


def config_value(name: str) -> object | None:
    keys = CONFIG_KEYS.get(name)
    if not keys:
        return None
    value: object = load_config()
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def local_path_default(name: str) -> str | None:
    suffix = LOCAL_PATH_DEFAULTS.get(name)
    if suffix is None:
        return None
    dotenv = load_dotenv()
    configured_root = (
        os.environ.get("AGENT_MEMORY_CONFIG_ROOT", "").strip()
        or str(config_value("CONFIG_ROOT") or "").strip()
        or dotenv.get("AGENT_MEMORY_CONFIG_ROOT", "").strip()
    )
    if configured_root:
        # Keep literal $HOME paths POSIX-shaped so Windows Path doesn't rewrite separators.
        if "$HOME" in configured_root or "${HOME}" in configured_root:
            base = configured_root.replace("\\", "/").rstrip("/")
            return "/".join([base, *suffix]) if suffix else base
        root = Path(os.path.expandvars(configured_root)).expanduser()
    elif (RUNTIME_ROOT / "config" / "runtime-manifest.json").is_file():
        root = RUNTIME_ROOT
    else:
        root = RUNTIME_ROOT / ".agent-memory"
    return str(root.joinpath(*suffix))


def env_value(name: str, default: str = "") -> str:
    """Read environment, runtime TOML, local .env, then an isolated safe default."""
    value = os.environ.get(f"AGENT_MEMORY_{name}")
    if value not in (None, ""):
        return value
    configured = config_value(name)
    if configured not in (None, ""):
        return str(configured)
    dotenv_value = load_dotenv().get(f"AGENT_MEMORY_{name}")
    if dotenv_value not in (None, ""):
        return str(dotenv_value)
    local_default = local_path_default(name)
    if local_default is not None:
        return local_default
    return default
