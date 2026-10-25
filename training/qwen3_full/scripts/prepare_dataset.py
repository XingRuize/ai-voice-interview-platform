from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import (
    SYSTEM_PROMPT,
    TRAINING_ROOT,
    normalize_followup_type,
    parse_bool,
    read_jsonl,
    resolve_from_root,
    sha256_file,
    stable_key,
    write_jsonl,
)


REQUIRED_FIELDS = {
    "participant_id",
    "session_id",
    "turn_id",
    "question_id",
    "question_text",
    "corrected_text",
    "followup_type",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把人工标注访谈轮次转换为Qwen3对话SFT数据，并按参与者划分数据集。"
    )
    parser.add_argument(
        "--input",
        nargs="+",
        default=["data/annotated_turns.example.jsonl"],
        help="一个或多个JSONL、JSON或CSV标注文件。",
    )
    parser.add_argument(
        "--outline",
        default="data/interview_outline.json",
        help="访谈题纲JSON。",
    )
    parser.add_argument("--output-dir", default="prepared")
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--validation-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=20261022)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_source(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return read_jsonl(path)
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            value = value.get("records", value.get("turns", []))
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError(f"{path} 必须是对象数组，或包含records/turns数组")
        return value
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    raise ValueError(f"不支持的输入格式：{path}")


def load_outline(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    questions = payload.get("questions", [])
    if not isinstance(questions, list) or not questions:
        raise ValueError("题纲必须包含非空questions数组")
    by_id = {str(row["id"]): row for row in questions}
    if len(by_id) != len(questions):
        raise ValueError("题纲question id不能重复")
    return questions, by_id


def parse_history(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("history必须是数组或JSON数组字符串")
    result: list[dict[str, str]] = []
    for row in value[-2:]:
        if not isinstance(row, dict):
            raise ValueError("history中的元素必须是对象")
        result.append(
            {
                "question": str(row.get("question", "")).strip(),
                "answer": str(row.get("answer", "")).strip(),
            }
        )
    return result


def next_outline_question(
    question_id: str,
    questions: list[dict[str, Any]],
) -> dict[str, str]:
    ids = [str(row["id"]) for row in questions]
    if question_id not in ids:
        return {"id": "", "text": ""}
    index = ids.index(question_id) + 1
    if index >= len(questions):
        return {"id": "", "text": ""}
    row = questions[index]
    return {"id": str(row["id"]), "text": str(row["core_question"])}


def build_target(row: dict[str, Any], next_question: dict[str, str]) -> dict[str, str]:
    label = normalize_followup_type(row.get("followup_type"))
    safety = parse_bool(row.get("safety_flag")) or label == "SAFETY"
    distress = parse_bool(row.get("distress_flag")) or label == "SUPPORT"
    followup_needed = parse_bool(row.get("followup_needed")) or label != "NONE"
    focus = str(row.get("focus_quote", "")).strip()
    answer = str(row.get("corrected_text", "")).strip()
    approved = str(row.get("approved_followup", "")).strip()

    if safety:
        action = "HANDOFF"
        label = "SAFETY"
        interviewer_text = "我先暂停自动访谈，请研究人员来和你继续。"
        next_id = ""
        focus = ""
    elif distress:
        action = "PAUSE"
        label = "SUPPORT"
        interviewer_text = "我们可以先停一下；是否继续由你决定。"
        next_id = ""
        focus = ""
    elif followup_needed:
        if label == "NONE":
            raise ValueError("followup_needed=1时followup_type不能是NONE")
        if not approved:
            raise ValueError("FOLLOWUP样本必须填写approved_followup")
        if not focus or focus not in answer:
            raise ValueError("FOLLOWUP样本的focus_quote必须逐字出现在corrected_text中")
        action = "FOLLOWUP"
        interviewer_text = approved
        next_id = str(row.get("question_id", ""))
    elif next_question["id"]:
        action = "NEXT"
        label = "NONE"
        interviewer_text = next_question["text"]
        next_id = next_question["id"]
        focus = ""
    else:
        action = "END"
        label = "NONE"
        interviewer_text = "谢谢你的参与，今天的访谈到这里结束。"
        next_id = ""
        focus = ""

    reason_code = str(row.get("reason_code", "")).strip()
    if not reason_code:
        reason_code = {
            "HANDOFF": "safety_risk",
            "PAUSE": "participant_needs_pause",
            "FOLLOWUP": f"needs_{label.lower()}",
            "NEXT": "answer_complete",
            "END": "interview_complete",
        }[action]

    return {
        "action": action,
        "followup_type": label,
        "focus_quote": focus,
        "interviewer_text": interviewer_text,
        "next_question_id": next_id,
        "reason_code": reason_code,
    }


def convert_row(
    row: dict[str, Any],
    questions: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing = sorted(name for name in REQUIRED_FIELDS if not str(row.get(name, "")).strip())
    if missing:
        raise ValueError("缺少必填字段：" + ", ".join(missing))

    question_id = str(row["question_id"]).strip()
    outline_row = by_id.get(question_id)
    if not outline_row:
        raise ValueError(f"question_id不在题纲中：{question_id}")

    next_question = {
        "id": str(row.get("next_question_id", "")).strip(),
        "text": str(row.get("next_question_text", "")).strip(),
    }
    if bool(next_question["id"]) != bool(next_question["text"]):
        raise ValueError("next_question_id和next_question_text必须同时填写或同时留空")
    if not next_question["id"]:
        next_question = next_outline_question(question_id, questions)

    answer = str(row["corrected_text"]).strip()
    target = build_target(row, next_question)
    context = {
        "task": "choose_next_interview_action",
        "condition": str(row.get("condition", "unspecified")).strip(),
        "current_question": {
            "id": question_id,
            "section": str(row.get("section", outline_row.get("section", ""))).strip(),
            "text": str(row["question_text"]).strip(),
            "allowed_probes": outline_row.get("allowed_probes", []),
        },
        "participant_answer": answer,
        "recent_history": parse_history(row.get("history")),
        "next_core_question": next_question,
        "allowed_actions": ["NEXT", "FOLLOWUP", "PAUSE", "HANDOFF", "END"],
        "allowed_followup_types": [
            "NONE",
            "CLARIFY",
            "TIMELINE",
            "DETAIL",
            "IMPACT",
            "EMOTION",
            "COMMUNICATION",
            "SUPPORT",
            "SAFETY",
        ],
    }
    user_content = json.dumps(context, ensure_ascii=False, separators=(",", ":")) + "\n/no_think"
    return {
        "prompt": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "completion": [
            {
                "role": "assistant",
                "content": json.dumps(target, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "metadata": {
            "participant_id": str(row["participant_id"]).strip(),
            "session_id": str(row["session_id"]).strip(),
            "turn_id": str(row["turn_id"]).strip(),
            "question_id": question_id,
            "condition": str(row.get("condition", "unspecified")).strip(),
            "corrected_text": answer,
            "action": target["action"],
            "followup_type": target["followup_type"],
            "annotator_id": str(row.get("annotator_id", "")).strip(),
        },
    }


def split_participants(
    rows: list[dict[str, Any]],
    train_ratio: float,
    validation_ratio: float,
    seed: int,
) -> dict[str, set[str]]:
    participants = sorted(
        {row["metadata"]["participant_id"] for row in rows},
        key=lambda value: stable_key(seed, value),
    )
    if len(participants) < 3:
        raise ValueError("至少需要3名参与者才能建立互不重叠的训练、验证和测试集")
    if not (0 < train_ratio < 1 and 0 < validation_ratio < 1):
        raise ValueError("train-ratio和validation-ratio必须在0到1之间")
    if train_ratio + validation_ratio >= 1:
        raise ValueError("train-ratio与validation-ratio之和必须小于1")

    count = len(participants)
    train_count = max(1, int(count * train_ratio))
    validation_count = max(1, int(count * validation_ratio))
    if train_count + validation_count >= count:
        train_count = max(1, count - validation_count - 1)

    return {
        "train": set(participants[:train_count]),
        "validation": set(participants[train_count : train_count + validation_count]),
        "test": set(participants[train_count + validation_count :]),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "records": len(rows),
        "participants": len({row["metadata"]["participant_id"] for row in rows}),
        "actions": dict(sorted(Counter(row["metadata"]["action"] for row in rows).items())),
        "followup_types": dict(
            sorted(Counter(row["metadata"]["followup_type"] for row in rows).items())
        ),
    }


def main() -> None:
    args = parse_args()
    outline_path = resolve_from_root(args.outline)
    output_dir = resolve_from_root(args.output_dir)
    input_paths = [resolve_from_root(value) for value in args.input]

    for path in [outline_path, *input_paths]:
        if not path.is_file():
            raise FileNotFoundError(path)
    if output_dir.exists() and any(output_dir.iterdir()):
        if not args.overwrite:
            raise FileExistsError(f"输出目录非空：{output_dir}；如需覆盖请加--overwrite")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    questions, by_id = load_outline(outline_path)
    converted: list[dict[str, Any]] = []
    failures: list[str] = []
    for source_path in input_paths:
        for index, source_row in enumerate(load_source(source_path), start=1):
            try:
                converted.append(convert_row(source_row, questions, by_id))
            except Exception as exc:  # report all annotation errors together
                failures.append(f"{source_path.name}:{index}: {exc}")
    if failures:
        raise ValueError("数据转换失败：\n" + "\n".join(failures))

    seen_turns: set[tuple[str, str, str]] = set()
    for row in converted:
        metadata = row["metadata"]
        key = (metadata["participant_id"], metadata["session_id"], metadata["turn_id"])
        if key in seen_turns:
            raise ValueError(f"重复轮次：{key}")
        seen_turns.add(key)

    participant_splits = split_participants(
        converted, args.train_ratio, args.validation_ratio, args.seed
    )
    split_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in converted:
        participant_id = row["metadata"]["participant_id"]
        split = next(name for name, ids in participant_splits.items() if participant_id in ids)
        split_rows[split].append(row)

    for name in ("train", "validation", "test"):
        split_rows[name].sort(
            key=lambda row: stable_key(
                args.seed,
                ":".join(
                    [
                        row["metadata"]["participant_id"],
                        row["metadata"]["session_id"],
                        row["metadata"]["turn_id"],
                    ]
                ),
            )
        )
        write_jsonl(output_dir / f"{name}.jsonl", split_rows[name])

    manifest = {
        "schema_version": "1.0",
        "split_unit": "participant_id",
        "seed": args.seed,
        "ratios": {
            "train": args.train_ratio,
            "validation": args.validation_ratio,
            "test": round(1 - args.train_ratio - args.validation_ratio, 6),
        },
        "source_files": [path.name for path in input_paths],
        "outline_file": outline_path.name,
        "total": summarize(converted),
        "splits": {name: summarize(split_rows[name]) for name in ("train", "validation", "test")},
        "sha256": {
            f"{name}.jsonl": sha256_file(output_dir / f"{name}.jsonl")
            for name in ("train", "validation", "test")
        },
    }
    (output_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
