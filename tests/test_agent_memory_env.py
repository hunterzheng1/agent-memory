from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    import tomllib as toml_oracle
except ImportError:  # pragma: no cover - exercised by the Python 3.10 CI job
    toml_oracle = None


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import agent_memory_env
from agent_memory_env import env_value, parse_toml_fallback, reset_config_cache


class AgentMemoryEnvironmentTests(unittest.TestCase):
    def test_toml_fallback_matches_tomllib_value_corpus(self) -> None:
        corpus = r'''
title = "value # retained" # outside comment
literal_hash = 'literal # retained' # outside comment
basic_path = "C:\\Users\\demo\\notes.txt"
literal_path = 'C:\new\test'
escaped_quote_hash = "quote: \"#inside\""
even_slashes = "two slashes: \\\\" # outside comment
odd_slashes = "slash quote: \\\"#inside"
flags = [true, false,] # trailing comma
empty = []
nested = [
  ["a#1", 'C:\new'], # comment in multiline array
  ["b", "c"],
]
matrix = [[1, 2,], [3, 4]]

[semantic_retrieval]
enabled = false # disabled
embedding_dim = 768 # integer
'''
        expected = {
            "title": "value # retained",
            "literal_hash": "literal # retained",
            "basic_path": r"C:\Users\demo\notes.txt",
            "literal_path": r"C:\new\test",
            "escaped_quote_hash": 'quote: "#inside"',
            "even_slashes": "two slashes: \\\\",
            "odd_slashes": r'slash quote: \"#inside',
            "flags": [True, False],
            "empty": [],
            "nested": [["a#1", r"C:\new"], ["b", "c"]],
            "matrix": [[1, 2], [3, 4]],
            "semantic_retrieval": {"enabled": False, "embedding_dim": 768},
        }

        if toml_oracle is not None:
            self.assertEqual(toml_oracle.loads(corpus), expected)
        self.assertEqual(parse_toml_fallback(corpus), expected)

    def test_toml_fallback_rejects_malformed_value_corpus(self) -> None:
        malformed = (
            'name = "unterminated',
            "name = 'unterminated",
            r'name = "invalid \q escape"',
            "flags = [true,, false]",
            "flags = [true false]",
            "flags = [true, false",
            "value = true trailing",
            "value = ]",
        )

        for corpus in malformed:
            with self.subTest(corpus=corpus):
                if toml_oracle is not None:
                    with self.assertRaises(toml_oracle.TOMLDecodeError):
                        toml_oracle.loads(corpus)
                with self.assertRaises(ValueError):
                    parse_toml_fallback(corpus)

    def test_toml_fallback_strips_comments_outside_strings(self) -> None:
        payload = parse_toml_fallback(
            'enabled = false # disabled\n'
            'embedding_dim = 768 # model width\n'
            'double_quoted = "keep # marker" # trailing comment\n'
            "single_quoted = 'also # marker' # trailing comment\n"
        )

        self.assertIs(payload["enabled"], False)
        self.assertEqual(payload["embedding_dim"], 768)
        self.assertIsInstance(payload["embedding_dim"], int)
        self.assertEqual(payload["double_quoted"], "keep # marker")
        self.assertEqual(payload["single_quoted"], "also # marker")

    def test_toml_fallback_parses_multiline_string_array_with_comments(self) -> None:
        payload = parse_toml_fallback(
            "[retrieval]\n"
            "source_roots = [\n"
            '  "project/#keep.md", # primary\n'
            "  'workflow/notes.md', # secondary\n"
            "] # trailing comma is valid\n"
        )

        self.assertEqual(
            payload["retrieval"]["source_roots"],
            ["project/#keep.md", "workflow/notes.md"],
        )

    def test_agent_memory_value_is_used(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"AGENT_MEMORY_ROOT": "/agent/vault"},
            clear=True,
        ):
            self.assertEqual(env_value("ROOT", "/default"), "/agent/vault")

    def test_empty_value_uses_default(self) -> None:
        with mock.patch.dict(
            "os.environ", {"AGENT_MEMORY_ROOT": ""}, clear=True
        ):
            self.assertEqual(env_value("ROOT", "/default"), "/default")

    def test_default_is_used_when_value_is_absent(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            reset_config_cache()
            self.assertEqual(env_value("ROOT", "/default"), "/default")

    def test_runtime_toml_is_used_when_environment_is_absent(self) -> None:
        with self.subTest("toml"):
            import tempfile

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
                config = Path(raw_root) / "agent-memory.toml"
                config.write_text(
                    'memory_root = "/configured/vault"\n'
                    '[semantic_retrieval]\npython = "/configured/vector/python"\n',
                    encoding="utf-8",
                )
                with mock.patch.dict(
                    "os.environ",
                    {"AGENT_MEMORY_CONFIG_FILE": str(config)},
                    clear=True,
                ):
                    reset_config_cache()
                    self.assertEqual(env_value("ROOT", "/default"), "/configured/vault")
                    self.assertEqual(env_value("ZVEC_PYTHON", "python3"), "/configured/vector/python")
        reset_config_cache()

    def test_repo_source_defaults_to_isolated_local_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            with mock.patch.object(agent_memory_env, "RUNTIME_ROOT", root), mock.patch.dict(
                "os.environ", {}, clear=True
            ):
                reset_config_cache()
                self.assertEqual(
                    env_value("STATE_DB", "$HOME/.config/agent-memory/state.sqlite"),
                    str(root / ".agent-memory" / "state.sqlite"),
                )
        reset_config_cache()

    def test_repo_dotenv_is_loaded_without_shell_export(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            (root / ".env").write_text(
                "AGENT_MEMORY_ROOT=/dotenv/vault\n"
                "AGENT_MEMORY_CONFIG_ROOT=$HOME/.config/dotenv-memory\n",
                encoding="utf-8",
            )
            with mock.patch.object(agent_memory_env, "RUNTIME_ROOT", root), mock.patch.dict(
                "os.environ", {}, clear=True
            ):
                reset_config_cache()
                self.assertEqual(env_value("ROOT", "/default"), "/dotenv/vault")
                self.assertEqual(
                    env_value("STATE_DB", "/default/state.sqlite"),
                    "$HOME/.config/dotenv-memory/state.sqlite",
                )
        reset_config_cache()


if __name__ == "__main__":
    unittest.main()
