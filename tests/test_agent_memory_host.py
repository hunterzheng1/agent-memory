from __future__ import annotations

import importlib
import os
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def load_host():
    try:
        return importlib.import_module("agent_memory_host")
    except ModuleNotFoundError as exc:
        raise AssertionError("agent_memory_host registry module is missing") from exc


class HostRegistryTests(unittest.TestCase):
    def test_interface_lists_every_actor_scope_and_hook_actor(self) -> None:
        host = load_host()

        self.assertEqual(
            host.actor_names(),
            ("codex", "claude", "codebuddy", "cursor", "pi", "zcode", "human", "migration", "test"),
        )
        self.assertEqual(host.actor_names(hook_only=True), ("codex", "claude", "codebuddy", "zcode"))
        self.assertEqual(
            host.scope_names(),
            ("shared", "codex", "claude", "codebuddy", "cursor", "pi", "zcode"),
        )
        self.assertNotIn("shared", host.actor_names())
        self.assertEqual(
            host.__all__,
            ("HostContext", "UnknownActorError", "actor_names", "scope_names", "resolve"),
        )

    def test_resolve_returns_policy_for_every_actor(self) -> None:
        host = load_host()
        expected = {
            "codex": ("codex", "codex"),
            "claude": ("claude", "claude"),
            "codebuddy": ("codebuddy", "claude"),
            "cursor": ("cursor", ""),
            "pi": ("pi", ""),
            "zcode": ("zcode", "claude"),
            "human": ("", ""),
            "migration": ("", ""),
            "test": ("", ""),
        }

        for actor, (search_scope, hook_protocol) in expected.items():
            with self.subTest(actor=actor):
                context = host.resolve(actor, env={})
                self.assertEqual(context.actor, actor)
                self.assertEqual(context.session_id, "")
                self.assertEqual(context.search_scope, search_scope)
                self.assertEqual(context.hook_protocol, hook_protocol)

    def test_context_is_an_immutable_value_with_only_host_fields(self) -> None:
        host = load_host()
        context = host.resolve("codex", env={})

        self.assertEqual(
            tuple(field.name for field in fields(context)),
            ("actor", "session_id", "search_scope", "hook_protocol"),
        )
        self.assertFalse(hasattr(context, "__dict__"))
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            setattr(context, "session_id", "replacement")

    def test_explicit_session_precedes_payload_and_environment(self) -> None:
        host = load_host()

        context = host.resolve(
            "codex",
            explicit_session_id="  explicit-session  ",
            payload={"session_id": "payload-session"},
            env={"AGENT_MEMORY_SESSION_ID": "environment-session"},
        )

        self.assertEqual(context.session_id, "explicit-session")

    def test_each_payload_session_alias_precedes_environment(self) -> None:
        host = load_host()
        aliases = (
            "session_id",
            "sessionId",
            "thread_id",
            "threadId",
            "conversation_id",
            "conversationId",
        )

        for key in aliases:
            with self.subTest(key=key):
                context = host.resolve(
                    "claude",
                    explicit_session_id="   ",
                    payload={key: "  payload-session  "},
                    env={"AGENT_MEMORY_SESSION_ID": "environment-session"},
                )
                self.assertEqual(context.session_id, "payload-session")

        context = host.resolve(
            "claude",
            payload={"session_id": " first ", "thread_id": "second"},
            env={"AGENT_MEMORY_SESSION_ID": "environment-session"},
        )
        self.assertEqual(context.session_id, "first")

    def test_actor_environment_fallbacks_are_ordered_and_stripped(self) -> None:
        host = load_host()
        cases = (
            ("codex", {"AGENT_MEMORY_SESSION_ID": " ", "CODEX_THREAD_ID": " codex-thread "}, "codex-thread"),
            (
                "claude",
                {
                    "AGENT_MEMORY_SESSION_ID": " ",
                    "CLAUDE_SESSION_ID": " ",
                    "CLAUDE_CODE_SESSION_ID": " claude-code-session ",
                },
                "claude-code-session",
            ),
            (
                "codebuddy",
                {"AGENT_MEMORY_SESSION_ID": " ", "CODEBUDDY_SESSION_ID": " codebuddy-session "},
                "codebuddy-session",
            ),
            ("cursor", {"AGENT_MEMORY_SESSION_ID": " cursor-session "}, "cursor-session"),
            ("pi", {"AGENT_MEMORY_SESSION_ID": " ", "PI_SESSION_ID": " pi-session "}, "pi-session"),
            (
                "zcode",
                {"AGENT_MEMORY_SESSION_ID": " ", "ZCODE_SESSION_ID": " ", "CLAUDE_SESSION_ID": " zcode-hook-session "},
                "zcode-hook-session",
            ),
            ("human", {"AGENT_MEMORY_SESSION_ID": " human-session "}, "human-session"),
            ("migration", {"AGENT_MEMORY_SESSION_ID": " migration-session "}, "migration-session"),
            ("test", {"AGENT_MEMORY_SESSION_ID": " test-session "}, "test-session"),
        )

        for actor, env, expected in cases:
            with self.subTest(actor=actor):
                self.assertEqual(host.resolve(actor, env=env).session_id, expected)

    def test_default_environment_uses_the_current_process_mapping(self) -> None:
        host = load_host()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": " process-thread "}, clear=True):
            self.assertEqual(host.resolve("codex").session_id, "process-thread")

    def test_codebuddy_never_reads_claude_or_codex_sessions(self) -> None:
        host = load_host()
        context = host.resolve(
            "codebuddy",
            env={
                "CLAUDE_SESSION_ID": "claude-session",
                "CLAUDE_CODE_SESSION_ID": "claude-code-session",
                "CODEX_THREAD_ID": "codex-thread",
            },
        )

        self.assertEqual(context.session_id, "")

    def test_cursor_uses_only_generic_session_and_has_no_hook(self) -> None:
        host = load_host()
        inherited = host.resolve(
            "cursor",
            env={
                "CURSOR_SESSION_ID": "cursor-native-session",
                "CLAUDE_SESSION_ID": "claude-session",
                "CODEX_THREAD_ID": "codex-thread",
            },
        )
        generic = host.resolve(
            "cursor",
            env={
                "AGENT_MEMORY_SESSION_ID": " generic-session ",
                "CURSOR_SESSION_ID": "cursor-native-session",
            },
        )

        self.assertEqual(inherited.session_id, "")
        self.assertEqual(generic.session_id, "generic-session")
        self.assertEqual(generic.search_scope, "cursor")
        self.assertEqual(generic.hook_protocol, "")

    def test_pi_uses_native_session_and_has_no_hook(self) -> None:
        host = load_host()
        inherited = host.resolve(
            "pi",
            env={
                "CLAUDE_SESSION_ID": "claude-session",
                "CODEX_THREAD_ID": "codex-thread",
            },
        )
        native = host.resolve(
            "pi",
            env={"AGENT_MEMORY_SESSION_ID": " ", "PI_SESSION_ID": " pi-native "},
        )

        self.assertEqual(inherited.session_id, "")
        self.assertEqual(native.session_id, "pi-native")
        self.assertEqual(native.search_scope, "pi")
        self.assertEqual(native.hook_protocol, "")

    def test_zcode_reads_hook_injected_session_and_uses_claude_protocol(self) -> None:
        host = load_host()
        inherited = host.resolve(
            "zcode",
            env={
                "CLAUDE_SESSION_ID": "zcode-hook-session",
                "CODEX_THREAD_ID": "codex-thread",
                "PI_SESSION_ID": "pi-session",
            },
        )
        explicit = host.resolve(
            "zcode",
            env={"AGENT_MEMORY_SESSION_ID": " explicit-session ", "CLAUDE_SESSION_ID": "zcode-hook-session"},
        )

        # ZCode injects CLAUDE_SESSION_ID into hook processes; other hosts'
        # session variables must never leak into zcode attribution.
        self.assertEqual(inherited.session_id, "zcode-hook-session")
        self.assertEqual(explicit.session_id, "explicit-session")
        self.assertEqual(explicit.search_scope, "zcode")
        # ZCode hook payloads are Claude-shaped, so the stop hook reuses the
        # claude protocol parser.
        self.assertEqual(explicit.hook_protocol, "claude")

    def test_unknown_and_legacy_actor_names_are_rejected(self) -> None:
        host = load_host()

        for actor in ("shared", "yichen-content-studio", "unknown"):
            with self.subTest(actor=actor):
                with self.assertRaises(host.UnknownActorError):
                    host.resolve(actor, env={})


if __name__ == "__main__":
    unittest.main()
