from __future__ import annotations

import argparse
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import agent_memory_zvec_index as zvec_index


def operation_args(operation: str) -> argparse.Namespace:
    return argparse.Namespace(
        init=operation == "init",
        scan=operation == "scan",
        prune=False,
        report=operation == "report",
        changed_file=[],
        search="query" if operation == "search" else None,
        lock_timeout=7.5,
        json=False,
    )


class ZvecLockingTests(unittest.TestCase):
    def test_main_serializes_every_zvec_operation_exclusively(self) -> None:
        for operation in ("init", "scan", "report", "search"):
            with self.subTest(operation=operation):
                args = operation_args(operation)
                with mock.patch.object(
                    zvec_index,
                    "parse_args",
                    return_value=args,
                ), mock.patch.object(
                    zvec_index,
                    "run_locked",
                    return_value=0,
                ), mock.patch.object(
                    zvec_index,
                    "zvec_lock",
                    side_effect=lambda **_kwargs: contextlib.nullcontext(),
                ) as lock:
                    self.assertEqual(zvec_index.main(), 0)

                self.assertEqual(
                    lock.call_args.kwargs,
                    {"exclusive": True, "timeout": 7.5},
                )


if __name__ == "__main__":
    unittest.main()
