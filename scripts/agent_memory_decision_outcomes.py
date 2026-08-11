#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

from agent_memory_env import env_value, resolve_config_path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = resolve_config_path(env_value("ROOT", str(RUNTIME_ROOT / "templates" / "vault")))
SCAN_DIRECTORIES = ("决策", "项目")
SECTION_PATTERN = re.compile(r"^##\s+决策\s*[-—–]\s*结果记录\s*$")
FIELD_PATTERN = re.compile(r"^\s*[-*]\s*([^:：]+)[:：]\s*(.*?)\s*$")
REQUIRED_FIELDS = (
    "决策日期",
    "决策问题",
    "当时选项",
    "最终选择",
    "预期结果",
    "复盘日期",
    "实际结果",
    "副作用",
    "当前结论",
    "未确定项",
)
PENDING_PREFIXES = (
    "TBD",
    "TODO",
    "PENDING",
    "UNKNOWN",
    "待定",
    "待观察",
    "待验证",
    "未验证",
    "未填写",
    "待复盘",
    "待补充",
    "尚未",
    "未知",
)
OUTCOME_FIELDS = ("实际结果", "副作用", "当前结论", "未确定项")
NO_EVIDENCE_VALUES = {"无", "无证据", "NONE", "N/A", "NA"}


def is_pending_value(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True
    folded = normalized.casefold()
    return any(folded.startswith(prefix.casefold()) for prefix in PENDING_PREFIXES)


def has_meaningful_evidence(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized) and not is_pending_value(normalized) and normalized.upper() not in NO_EVIDENCE_VALUES


def parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except (AttributeError, ValueError):
        return None


def extract_record(text: str) -> dict[str, str] | None:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if SECTION_PATTERN.match(line.strip())), None)
    if start is None:
        return None
    fields: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = FIELD_PATTERN.match(line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def record_status(fields: dict[str, str], as_of: dt.date) -> tuple[str, list[str], str]:
    missing = [field for field in REQUIRED_FIELDS if not fields.get(field, "").strip()]
    if missing:
        return "invalid", missing, ""
    review_date = parse_date(fields["复盘日期"])
    decision_date = parse_date(fields["决策日期"])
    if review_date is None or decision_date is None:
        invalid_dates = []
        if decision_date is None:
            invalid_dates.append("决策日期")
        if review_date is None:
            invalid_dates.append("复盘日期")
        return "invalid", invalid_dates, fields.get("复盘日期", "")
    pending = any(is_pending_value(fields[field]) for field in OUTCOME_FIELDS)
    if pending:
        return ("scheduled" if review_date > as_of else "overdue"), [], review_date.isoformat()
    return "observed", [], review_date.isoformat()


def scan_records(vault_root: Path, as_of: dt.date) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    eligible_files = 0
    for directory_name in SCAN_DIRECTORIES:
        directory = vault_root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.name.startswith("_模板") or path.name == "README.md":
                continue
            eligible_files += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            fields = extract_record(text)
            if fields is None:
                continue
            status, missing_fields, review_date = record_status(fields, as_of)
            records.append(
                {
                    "rel_path": path.relative_to(vault_root).as_posix(),
                    "status": status,
                    "review_date": review_date,
                    "missing_fields": missing_fields,
                    "evidence_recorded": has_meaningful_evidence(fields.get("后续使用证据", "")),
                }
            )
    counts = {status: sum(1 for record in records if record["status"] == status) for status in ("observed", "scheduled", "overdue", "invalid")}
    return {
        "as_of": as_of.isoformat(),
        "eligible_files": eligible_files,
        "record_count": len(records),
        "coverage": (len(records) / eligible_files) if eligible_files else 0.0,
        "counts": counts,
        "records": records,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report decision-outcome records without modifying Markdown truth.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--as-of", default="", help="Review date in YYYY-MM-DD; defaults to today.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when an included record is invalid.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = parse_date(args.as_of) if args.as_of else dt.date.today()
    if as_of is None:
        print("invalid_as_of_date", file=sys.stderr)
        return 2
    payload = scan_records(VAULT_ROOT, as_of)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"decision_outcomes={payload['record_count']} coverage={payload['coverage']:.1%} as_of={payload['as_of']}")
        for status, count in payload["counts"].items():
            print(f"{status}={count}")
        for record in payload["records"]:
            print(f"{record['status']}: {record['rel_path']} review={record['review_date'] or '-'}")
    return 2 if args.strict and payload["counts"]["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
