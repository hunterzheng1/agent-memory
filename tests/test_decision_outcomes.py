from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory_decision_outcomes as outcomes


def note(actual: str, review: str = "2026-07-20", omit: str = "", **overrides: str) -> str:
    values = {
        "决策日期": "2026-07-01",
        "决策问题": "示例问题",
        "当时选项": "A；B",
        "最终选择": "A",
        "预期结果": "示例预期",
        "复盘日期": review,
        "实际结果": actual,
        "副作用": "无",
        "当前结论": "保留",
        "未确定项": "无",
        "后续使用证据": "后续测试已引用",
    }
    values.update(overrides)
    if omit:
        values.pop(omit)
    lines = ["# Example", "", "## 决策—结果记录", ""]
    lines.extend(f"- {key}：{value}" for key, value in values.items())
    return "\n".join(lines) + "\n"


class DecisionOutcomeTests(unittest.TestCase):
    def test_states_and_privacy_safe_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "决策").mkdir()
            (root / "项目").mkdir()
            (root / "决策" / "observed.md").write_text(note("达到预期"), encoding="utf-8")
            (root / "决策" / "scheduled.md").write_text(note("TBD", "2026-07-21"), encoding="utf-8")
            (root / "项目" / "overdue.md").write_text(note("待观察", "2026-07-18"), encoding="utf-8")
            (root / "决策" / "invalid.md").write_text(note("达到预期", omit="副作用"), encoding="utf-8")
            (root / "决策" / "_模板-ignore.md").write_text(note("secret template body"), encoding="utf-8")

            report = outcomes.scan_records(root, dt.date(2026, 7, 19))

        self.assertEqual(report["counts"], {"observed": 1, "scheduled": 1, "overdue": 1, "invalid": 1})
        self.assertEqual(report["record_count"], 4)
        self.assertNotIn("示例问题", str(report))
        invalid = next(record for record in report["records"] if record["status"] == "invalid")
        self.assertEqual(invalid["missing_fields"], ["副作用"])

    def test_invalid_date_is_invalid(self) -> None:
        fields = outcomes.extract_record(note("TBD", "not-a-date"))
        self.assertIsNotNone(fields)
        status, missing, _ = outcomes.record_status(fields or {}, dt.date(2026, 7, 19))
        self.assertEqual(status, "invalid")
        self.assertEqual(missing, ["复盘日期"])

    def test_any_unfinished_outcome_field_keeps_record_pending(self) -> None:
        for field, value in (
            ("实际结果", "unknown"),
            ("副作用", "待定"),
            ("当前结论", "TBD - 等待数据"),
            ("未确定项", "未填写"),
        ):
            with self.subTest(field=field):
                fields = outcomes.extract_record(note("达到预期", **{field: value}))
                status, missing, _ = outcomes.record_status(fields or {}, dt.date(2026, 7, 19))
                self.assertEqual(status, "scheduled")
                self.assertEqual(missing, [])

    def test_placeholder_evidence_is_not_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "决策").mkdir()
            (root / "决策" / "pending-evidence.md").write_text(
                note("达到预期", **{"后续使用证据": "TBD"}),
                encoding="utf-8",
            )
            report = outcomes.scan_records(root, dt.date(2026, 7, 19))
        self.assertFalse(report["records"][0]["evidence_recorded"])
        self.assertFalse(outcomes.has_meaningful_evidence("无"))


if __name__ == "__main__":
    unittest.main()
