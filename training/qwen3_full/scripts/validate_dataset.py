from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from common import ACTIONS, FOLLOWUP_TYPES, read_jsonl, resolve_from_root


LEADING_PATTERNS = [
    re.compile(r"难道"),
    re.compile(r"你(?:应该|一定|肯定)"),
    re.compile(r"显然|毫无疑问"),
    re.compile(r"是不是.{0,16}(?:因为|所以|导致|造成)"),
    re.compile(r"为什么你不"),
]
PII_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "cn_id": re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证准备后的SFT数据、标签和参与者隔离。")
    parser.add_argument("--data-dir", default="prepared")
    parser.add_argument("--allow-pii", action="store_true")
    parser.add_argument("--report", default="prepared/validation_report.json")
    return parser.parse_args()


def parse_completion(row: dict[str, Any]) -> dict[str, Any]:
    completion = row.get("completion")
    if not isinstance(completion, list) or len(completion) != 1:
        raise ValueError("completion必须只包含一个assistant消息")
    message = completion[0]
    if message.get("role") != "assistant":
        raise ValueError("completion角色必须是assistant")
    value = json.loads(str(message.get("content", "")))
    if not isinstance(value, dict):
        raise ValueError("assistant内容必须是JSON对象")
    return value


def validate_row(row: dict[str, Any], location: str, allow_pii: bool) -> list[str]:
    errors: list[str] = []
    prompt = row.get("prompt")
    metadata = row.get("metadata")
    if not isinstance(prompt, list) or [item.get("role") for item in prompt] != ["system", "user"]:
        errors.append(f"{location}: prompt必须依次包含system和user")
    if not isinstance(metadata, dict):
        return errors + [f"{location}: 缺少metadata对象"]
    participant_id = str(metadata.get("participant_id", ""))
    session_id = str(metadata.get("session_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", participant_id):
        errors.append(f"{location}: participant_id必须是匿名英数字编号")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
        errors.append(f"{location}: session_id必须是匿名英数字编号")

    try:
        target = parse_completion(row)
    except Exception as exc:
        return errors + [f"{location}: {exc}"]

    required = {
        "action",
        "followup_type",
        "focus_quote",
        "interviewer_text",
        "next_question_id",
        "reason_code",
    }
    missing = sorted(required - set(target))
    if missing:
        errors.append(f"{location}: assistant JSON缺少字段 {missing}")
        return errors

    action = str(target["action"])
    label = str(target["followup_type"])
    if action not in ACTIONS:
        errors.append(f"{location}: action非法：{action}")
    if label not in FOLLOWUP_TYPES:
        errors.append(f"{location}: followup_type非法：{label}")
    valid_pairs = {
        "NEXT": {"NONE"},
        "END": {"NONE"},
        "PAUSE": {"SUPPORT"},
        "HANDOFF": {"SAFETY"},
        "FOLLOWUP": FOLLOWUP_TYPES - {"NONE", "SUPPORT", "SAFETY"},
    }
    if action in valid_pairs and label not in valid_pairs[action]:
        errors.append(f"{location}: action={action}不能配followup_type={label}")

    answer = str(metadata.get("corrected_text", ""))
    focus = str(target.get("focus_quote", ""))
    if action == "FOLLOWUP" and (not focus or focus not in answer):
        errors.append(f"{location}: FOLLOWUP的focus_quote不在人工核对文本中")
    if action != "FOLLOWUP" and focus:
        errors.append(f"{location}: 非FOLLOWUP样本的focus_quote必须为空")

    interviewer_text = str(target.get("interviewer_text", "")).strip()
    if not interviewer_text:
        errors.append(f"{location}: interviewer_text不能为空")
    if interviewer_text.count("？") + interviewer_text.count("?") > 1:
        errors.append(f"{location}: interviewer_text一次提出了多个问题")
    for pattern in LEADING_PATTERNS:
        if pattern.search(interviewer_text):
            errors.append(f"{location}: interviewer_text疑似诱导：{pattern.pattern}")

    if not allow_pii:
        for name, pattern in PII_PATTERNS.items():
            if pattern.search(answer):
                errors.append(f"{location}: corrected_text疑似包含直接标识符({name})")
    return errors


def main() -> None:
    args = parse_args()
    data_dir = resolve_from_root(args.data_dir)
    report_path = resolve_from_root(args.report)
    errors: list[str] = []
    participant_sets: dict[str, set[str]] = {}
    counts: dict[str, Any] = {}

    for split in ("train", "validation", "test"):
        path = data_dir / f"{split}.jsonl"
        if not path.is_file():
            errors.append(f"缺少文件：{path}")
            continue
        rows = read_jsonl(path)
        participant_sets[split] = {
            str(row.get("metadata", {}).get("participant_id", "")) for row in rows
        }
        participant_sets[split].discard("")
        action_counts: Counter[str] = Counter()
        type_counts: Counter[str] = Counter()
        for index, row in enumerate(rows, start=1):
            location = f"{path.name}:{index}"
            errors.extend(validate_row(row, location, args.allow_pii))
            try:
                target = parse_completion(row)
                action_counts[str(target.get("action", ""))] += 1
                type_counts[str(target.get("followup_type", ""))] += 1
            except Exception:
                pass
        counts[split] = {
            "records": len(rows),
            "participants": len(participant_sets[split]),
            "actions": dict(sorted(action_counts.items())),
            "followup_types": dict(sorted(type_counts.items())),
        }

    for left, right in (("train", "validation"), ("train", "test"), ("validation", "test")):
        overlap = participant_sets.get(left, set()) & participant_sets.get(right, set())
        if overlap:
            errors.append(f"{left}与{right}存在参与者泄漏，共{len(overlap)}人")

    report = {
        "ok": not errors,
        "errors": errors,
        "counts": counts,
        "participant_split_overlap": False
        if all(not (participant_sets.get(a, set()) & participant_sets.get(b, set())) for a, b in (("train", "validation"), ("train", "test"), ("validation", "test")))
        else True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
