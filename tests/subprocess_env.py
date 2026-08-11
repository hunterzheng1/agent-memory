from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def isolated_subprocess_env(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build a subprocess environment without installed Agent Memory settings."""
    env = os.environ.copy()
    for key in tuple(env):
        normalized = key.upper()
        if (
            normalized.startswith("AGENT_MEMORY_")
            or normalized.startswith("CODEX_MEMORY_")
            or normalized == "MEMORY_ACTOR"
        ):
            env.pop(key, None)
    if overrides:
        env.update({key: os.fspath(value) for key, value in overrides.items()})
    return env
