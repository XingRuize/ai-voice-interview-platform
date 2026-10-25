from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import FOLLOWUP_TYPES, read_jsonl, resolve_from_root, write_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在按参与者留出的测试集上评价训练后的访谈模型。")
    parser.add_argument("--model", required=True, help="全参数模型或合并后模型目录。")
    parser.add_argument("--adapter", default=None, help="可选：未合并的LoRA适配器目录。")
    parser.add_argument("--test-file", default="prepared/test.jsonl")
    parser.add_argument("--output-dir", default="evaluation")
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--limit", type=int, default=0, help="仅调试时限制样本数；0表示全部。")
    return parser.parse_args()


def strip_to_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("未找到JSON对象")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("输出不是JSON对象")
    return value


def gold_target(row: dict[str, Any]) -> dict[str, Any]:
    completion = row["completion"]
    return json.loads(completion[0]["content"])


def safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def classification_report(gold: list[str], predicted: list[str], labels: list[str]) -> dict[str, Any]:
    per_class: dict[str, Any] = {}
    f1_values: list[float] = []
    present_f1: list[float] = []
    for label in labels:
        tp = sum(g == label and p == label for g, p in zip(gold, predicted))
        fp = sum(g != label and p == label for g, p in zip(gold, predicted))
        fn = sum(g == label and p != label for g, p in zip(gold, predicted))
        support = sum(g == label for g in gold)
        precision = safe_div(tp, tp + fp)
        recall = safe_div(tp, tp + fn)
        f1 = safe_div(2 * precision * recall, precision + recall)
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
        f1_values.append(f1)
        if support:
            present_f1.append(f1)
    return {
        "accuracy": safe_div(sum(g == p for g, p in zip(gold, predicted)), len(gold)),
        "macro_f1_all_labels": safe_div(sum(f1_values), len(f1_values)),
        "macro_f1_present_labels": safe_div(sum(present_f1), len(present_f1)),
        "per_class": per_class,
    }


def main() -> None:
    args = parse_args()
    model_path = resolve_from_root(args.model)
    adapter_path = resolve_from_root(args.adapter) if args.adapter else None
    test_path = resolve_from_root(args.test_file)
    output_dir = resolve_from_root(args.output_dir)
    if not (model_path / "config.json").is_file():
        raise FileNotFoundError(model_path / "config.json")
    if adapter_path and not (adapter_path / "adapter_config.json").is_file():
        raise FileNotFoundError(adapter_path / "adapter_config.json")
    if not test_path.is_file():
        raise FileNotFoundError(test_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available():
        raise RuntimeError("模型评价要求CUDA GPU")
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, use_fast=True)
    model_kwargs: dict[str, Any] = {
        "local_files_only": True,
        "dtype": torch.bfloat16,
        "device_map": "auto",
    }
    if args.load_in_4bit:
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
    model = AutoModelForCausalLM.from_pretrained(model_path, **model_kwargs)
    if adapter_path:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, adapter_path, local_files_only=True)
    model.eval()

    rows = read_jsonl(test_path)
    if args.limit > 0:
        rows = rows[: args.limit]
    predictions: list[dict[str, Any]] = []
    gold_actions: list[str] = []
    predicted_actions: list[str] = []
    gold_types: list[str] = []
    predicted_types: list[str] = []
    invalid_count = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)

    for index, row in enumerate(rows, start=1):
        template_kwargs = {
            "add_generation_prompt": True,
            "tokenize": True,
            "return_tensors": "pt",
        }
        try:
            inputs = tokenizer.apply_chat_template(
                row["prompt"], enable_thinking=False, **template_kwargs
            )
        except TypeError:
            inputs = tokenizer.apply_chat_template(row["prompt"], **template_kwargs)
        inputs = inputs.to(model.device)
        with torch.inference_mode():
            output = model.generate(
                inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        decoded = tokenizer.decode(output[0][inputs.shape[-1] :], skip_special_tokens=True)
        gold = gold_target(row)
        try:
            predicted = strip_to_json(decoded)
            predicted_action = str(predicted.get("action", "INVALID"))
            predicted_type = str(predicted.get("followup_type", "INVALID"))
            valid_json = True
        except Exception as exc:
            predicted = {"parse_error": str(exc), "raw_output": decoded}
            predicted_action = "INVALID"
            predicted_type = "INVALID"
            valid_json = False
            invalid_count += 1

        gold_action = str(gold["action"])
        gold_type = str(gold["followup_type"])
        gold_actions.append(gold_action)
        predicted_actions.append(predicted_action)
        gold_types.append(gold_type)
        predicted_types.append(predicted_type)
        confusion[gold_type][predicted_type] += 1
        predictions.append(
            {
                "index": index,
                "participant_id": row.get("metadata", {}).get("participant_id", ""),
                "session_id": row.get("metadata", {}).get("session_id", ""),
                "turn_id": row.get("metadata", {}).get("turn_id", ""),
                "valid_json": valid_json,
                "gold": gold,
                "predicted": predicted,
            }
        )

    metrics = {
        "records": len(rows),
        "invalid_json_rate": safe_div(invalid_count, len(rows)),
        "action": classification_report(
            gold_actions,
            predicted_actions,
            ["NEXT", "FOLLOWUP", "PAUSE", "HANDOFF", "END"],
        ),
        "followup_type": classification_report(
            gold_types,
            predicted_types,
            sorted(FOLLOWUP_TYPES),
        ),
        "safety_recall": classification_report(gold_types, predicted_types, ["SAFETY"])[
            "per_class"
        ]["SAFETY"]["recall"],
        "support_recall": classification_report(gold_types, predicted_types, ["SUPPORT"])[
            "per_class"
        ]["SUPPORT"]["recall"],
        "confusion_matrix": {gold: dict(values) for gold, values in sorted(confusion.items())},
    }
    write_jsonl(output_dir / "predictions.jsonl", predictions)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
