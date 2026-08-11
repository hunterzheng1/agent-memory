from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.subprocess_env import isolated_subprocess_env


class SubprocessEnvironmentTests(unittest.TestCase):
    def test_conflicting_global_memory_configuration_is_not_inherited(self) -> None:
        conflicts = {
            "AGENT_MEMORY_ROOT": "C:/global/agent-vault",
            "AGENT_MEMORY_CONFIG_FILE": "C:/global/agent-memory.toml",
            "CODEX_MEMORY_ROOT": "C:/global/legacy-vault",
            "MEMORY_ACTOR": "claude",
            "AGENT_MEMORY_TEST_SENTINEL": "must-not-leak",
            "TEST_NON_MEMORY_SENTINEL": "preserved",
        }
        with mock.patch.dict(os.environ, conflicts, clear=False):
            env = isolated_subprocess_env(
                {"AGENT_MEMORY_ROOT": "C:/isolated/test-vault"}
            )

        code = (
            "import json, os; "
            "print(json.dumps({"
            "'root': os.environ.get('AGENT_MEMORY_ROOT'), "
            "'config': os.environ.get('AGENT_MEMORY_CONFIG_FILE'), "
            "'legacy': os.environ.get('CODEX_MEMORY_ROOT'), "
            "'actor': os.environ.get('MEMORY_ACTOR'), "
            "'sentinel': os.environ.get('AGENT_MEMORY_TEST_SENTINEL'), "
            "'preserved': os.environ.get('TEST_NON_MEMORY_SENTINEL'), "
            "'has_path': bool(os.environ.get('PATH'))}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "root": "C:/isolated/test-vault",
                "config": None,
                "legacy": None,
                "actor": None,
                "sentinel": None,
                "preserved": "preserved",
                "has_path": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
