from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.subprocess_env import isolated_subprocess_env


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

    def test_subprocess_contender_waits_for_holder_to_unlock(self) -> None:
        holder_code = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from agent_memory_lock import try_lock, unlock; "
            "handle = Path(sys.argv[2]).open('a+', encoding='utf-8'); "
            "assert try_lock(handle); print('locked', flush=True); "
            "sys.stdin.readline(); unlock(handle); handle.close()"
        )
        contender_code = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from agent_memory_lock import try_lock, unlock; "
            "handle = Path(sys.argv[2]).open('a+', encoding='utf-8'); "
            "acquired = try_lock(handle); print('acquired' if acquired else 'blocked'); "
            "unlock(handle) if acquired else None; handle.close()"
        )
        env = isolated_subprocess_env()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            lock_path = Path(raw_root) / "subprocess.lock"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    holder_code,
                    str(SCRIPTS_ROOT),
                    str(lock_path),
                ],
                env=env,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                self.assertIsNotNone(holder.stdout)
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                blocked = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        contender_code,
                        str(SCRIPTS_ROOT),
                        str(lock_path),
                    ],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(blocked.returncode, 0, blocked.stderr)
                self.assertEqual(blocked.stdout.strip(), "blocked")

                self.assertIsNotNone(holder.stdin)
                holder.stdin.write("\n")
                holder.stdin.flush()
                _, holder_stderr = holder.communicate(timeout=15)
                self.assertEqual(holder.returncode, 0, holder_stderr)

                acquired = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        contender_code,
                        str(SCRIPTS_ROOT),
                        str(lock_path),
                    ],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(acquired.returncode, 0, acquired.stderr)
                self.assertEqual(acquired.stdout.strip(), "acquired")
                if os.name == "nt":
                    self.assertGreaterEqual(lock_path.stat().st_size, 1)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.communicate(timeout=15)


if __name__ == "__main__":
    unittest.main()
