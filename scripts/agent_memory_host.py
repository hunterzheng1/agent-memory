#!/usr/bin/env python3
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


__all__ = ("HostContext", "UnknownActorError", "actor_names", "scope_names", "resolve")


@dataclass(frozen=True, slots=True)
class HostContext:
    actor: str
    session_id: str
    search_scope: str
    hook_protocol: str


class UnknownActorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _HostPolicy:
    session_env: tuple[str, ...]
    search_scope: str
    hook_protocol: str


_GENERIC_SESSION_ENV = ("AGENT_MEMORY_SESSION_ID",)
_REGISTRY: Mapping[str, _HostPolicy] = MappingProxyType(
    {
        "codex": _HostPolicy(
            session_env=("AGENT_MEMORY_SESSION_ID", "CODEX_THREAD_ID"),
            search_scope="codex",
            hook_protocol="codex",
        ),
        "claude": _HostPolicy(
            session_env=("AGENT_MEMORY_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
            search_scope="claude",
            hook_protocol="claude",
        ),
        "codebuddy": _HostPolicy(
            session_env=("AGENT_MEMORY_SESSION_ID", "CODEBUDDY_SESSION_ID"),
            search_scope="codebuddy",
            hook_protocol="claude",
        ),
        "cursor": _HostPolicy(
            session_env=_GENERIC_SESSION_ENV,
            search_scope="cursor",
            hook_protocol="",
        ),
        "pi": _HostPolicy(
            session_env=("AGENT_MEMORY_SESSION_ID", "PI_SESSION_ID"),
            search_scope="pi",
            hook_protocol="",
        ),
        "human": _HostPolicy(session_env=_GENERIC_SESSION_ENV, search_scope="", hook_protocol=""),
        "migration": _HostPolicy(session_env=_GENERIC_SESSION_ENV, search_scope="", hook_protocol=""),
        "test": _HostPolicy(session_env=_GENERIC_SESSION_ENV, search_scope="", hook_protocol=""),
    }
)
_ACTOR_NAMES = tuple(_REGISTRY)
_HOOK_ACTOR_NAMES = tuple(actor for actor, policy in _REGISTRY.items() if policy.hook_protocol)
_SCOPE_NAMES = (
    "shared",
    *(policy.search_scope for policy in _REGISTRY.values() if policy.search_scope),
)
_PAYLOAD_SESSION_KEYS = (
    "session_id",
    "sessionId",
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
)


def actor_names(*, hook_only: bool = False) -> tuple[str, ...]:
    return _HOOK_ACTOR_NAMES if hook_only else _ACTOR_NAMES


def scope_names() -> tuple[str, ...]:
    return _SCOPE_NAMES


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def resolve(
    actor: str,
    *,
    env: Mapping[str, object] | None = None,
    explicit_session_id: str = "",
    payload: Mapping[str, object] | None = None,
) -> HostContext:
    try:
        policy = _REGISTRY[actor]
    except (KeyError, TypeError) as exc:
        raise UnknownActorError(f"unknown memory actor: {actor!r}") from exc

    session_id = _clean(explicit_session_id)
    if not session_id and payload is not None:
        for key in _PAYLOAD_SESSION_KEYS:
            session_id = _clean(payload.get(key))
            if session_id:
                break
    if not session_id:
        source_env = os.environ if env is None else env
        for key in policy.session_env:
            session_id = _clean(source_env.get(key))
            if session_id:
                break

    return HostContext(
        actor=actor,
        session_id=session_id,
        search_scope=policy.search_scope,
        hook_protocol=policy.hook_protocol,
    )
