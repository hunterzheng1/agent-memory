"""agent_memory_index.py 纯函数单元测试。

用 unittest（标准库零依赖）覆盖解析与文本处理纯函数，无需 vault 或 state DB。
运行：python -m unittest discover -s tests -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_memory_index.py"
_spec = importlib.util.spec_from_file_location("agent_memory_index", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["agent_memory_index"] = mod  # dataclass 解析类型注解需要模块在 sys.modules
_spec.loader.exec_module(mod)


class TestParseFrontmatter(unittest.TestCase):
    def test_parses_scalar_field(self):
        text = "---\nmemory_type: project\ntrack: project\n---\n# Body\n"
        meta = mod.parse_frontmatter(text)
        self.assertEqual(meta.get("memory_type"), "project")
        self.assertEqual(meta.get("track"), "project")

    def test_returns_empty_when_no_frontmatter(self):
        self.assertEqual(mod.parse_frontmatter("plain text, no frontmatter"), {})

    def test_returns_empty_when_unclosed(self):
        self.assertEqual(mod.parse_frontmatter("---\nmemory_type: project\n"), {})

    def test_parses_list_field(self):
        text = "---\nkeywords:\n  - alpha\n  - beta\n---\n"
        meta = mod.parse_frontmatter(text)
        self.assertEqual(meta.get("keywords"), ["alpha", "beta"])


class TestAsText(unittest.TestCase):
    def test_joins_list(self):
        self.assertEqual(mod.as_text(["a", "b", "c"]), "a, b, c")

    def test_none_returns_default(self):
        self.assertEqual(mod.as_text(None, "fallback"), "fallback")

    def test_strips_str(self):
        self.assertEqual(mod.as_text("  hello  "), "hello")

    def test_strips_quotes(self):
        self.assertEqual(mod.as_text('"hello"'), "hello")
        self.assertEqual(mod.as_text("'hi'"), "hi")

    def test_empty_str_returns_default(self):
        self.assertEqual(mod.as_text("", "def"), "def")


class TestHashingAndText(unittest.TestCase):
    def test_sha256_known_vector(self):
        # sha256("abc") 标准向量
        self.assertEqual(
            mod.sha256_text("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_sha256_unicode(self):
        # 与纯 Python 实现一致即可（验证编码为 utf-8）
        import hashlib
        self.assertEqual(
            mod.sha256_text("记忆"),
            hashlib.sha256("记忆".encode("utf-8")).hexdigest(),
        )

    def test_utc_now_is_iso(self):
        s = mod.utc_now()
        self.assertTrue(s.startswith("20") and "T" in s and "+" in s)


class TestMarkdownExtractors(unittest.TestCase):
    def test_title_from_first_h1(self):
        text = "# My Title\nsome body\n"
        self.assertEqual(mod.title_from_markdown(text, Path("x.md")), "My Title")

    def test_title_fallback_to_stem(self):
        self.assertEqual(mod.title_from_markdown("no heading", Path("my-file.md")), "my-file")

    def test_headings_joined(self):
        text = "# A\n## B\n### C\nbody"
        self.assertEqual(mod.headings_from_markdown(text), "A | B | C")

    def test_section_lines_captures_until_next_h2(self):
        text = "## 目标\nline1\nline2\n## 其他\nignored"
        self.assertEqual(mod.section_lines(text, ["目标"]), ["line1", "line2"])

    def test_section_lines_no_match(self):
        self.assertEqual(mod.section_lines("## X\ny", ["目标"]), [])

    def test_extract_summary_prefers_validated_section(self):
        text = (
            "## 当前有效摘要\n这是摘要第一行。\n第二行。\n"
            "## 其他\n应被忽略\n"
        )
        summary = mod.extract_summary(text)
        self.assertIn("这是摘要第一行。", summary)
        self.assertIn("第二行。", summary)
        self.assertNotIn("应被忽略", summary)

    def test_extract_summary_falls_back_to_body(self):
        text = "第一段正文。\n第二段正文。\n"
        summary = mod.extract_summary(text)
        self.assertIn("第一段正文", summary)


class TestCompactLines(unittest.TestCase):
    def test_strips_dash_prefix_and_joins(self):
        lines = ["- item one", "- item two"]
        out = mod.compact_lines(lines)
        self.assertIn("item one", out)
        self.assertIn("item two", out)
        self.assertNotIn("- ", out)

    def test_skips_code_fence(self):
        lines = ["```python", "code", "```"]
        out = mod.compact_lines(lines)
        self.assertNotIn("```", out)
        self.assertIn("code", out)

    def test_truncates_to_limit(self):
        long_line = "x" * 100
        out = mod.compact_lines([long_line], limit=10)
        self.assertEqual(len(out), 10)


if __name__ == "__main__":
    unittest.main()
