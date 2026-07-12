import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.agent_memory_paths import resolve_path


class TestResolvePath(unittest.TestCase):
    def test_expands_posix_home_syntax_on_windows(self):
        fake_home = Path("C:/Users/example")
        with patch("scripts.agent_memory_paths.Path.home", return_value=fake_home):
            resolved = resolve_path("$HOME/.config/codex-memory")
        self.assertEqual(resolved, (fake_home / ".config" / "codex-memory").resolve())

    def test_expands_environment_variables_before_resolving(self):
        with patch.dict(os.environ, {"MEMORY_TEST_ROOT": "C:/memory-root"}):
            resolved = resolve_path("%MEMORY_TEST_ROOT%/state.sqlite")
        self.assertEqual(resolved, Path("C:/memory-root/state.sqlite").resolve())


if __name__ == "__main__":
    unittest.main()
