from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from agent_memory_lock import try_lock, unlock


class AgentMemoryLockTests(unittest.TestCase):
    def test_nonblocking_lock_serializes_independent_handles(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            lock_path = Path(raw_root) / "runtime.lock"
            with lock_path.open("a+", encoding="utf-8") as first, lock_path.open(
                "a+", encoding="utf-8"
            ) as second:
                self.assertTrue(try_lock(first))
                try:
                    self.assertFalse(try_lock(second))
                    if os.name == "nt":
                        self.assertGreaterEqual(lock_path.stat().st_size, 1)
                finally:
                    unlock(first)

                self.assertTrue(try_lock(second))
                unlock(second)


if __name__ == "__main__":
    unittest.main()
