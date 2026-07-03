"""codex_memory_*.py 兼容包装测试。"""

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    path = SCRIPT_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name.replace(".py", "")] = module
    spec.loader.exec_module(module)
    return module


class TestCompatWrappers(unittest.TestCase):
    def test_codex_wrappers_export_main(self):
        for script in (
            "codex_memory_search.py",
            "codex_memory_closeout.py",
            "codex_memory_audit.py",
            "codex_memory_audit_autorun.py",
        ):
            with self.subTest(script=script):
                module = load_script(script)
                self.assertTrue(callable(module.main))


if __name__ == "__main__":
    unittest.main()
