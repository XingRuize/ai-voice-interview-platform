from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from common import read_jsonl, resolve_from_root, sha256_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen3-8B受控研究访谈SFT训练。")
    parser.add_argument("--config", required=True, help="full或qlora训练JSON配置。")
    parser.add_argument("--resume-from-checkpoint", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查配置和数据，不导入PyTorch、不加载模型、不训练。",
    )
    parser.add_argument(
        "--allow-example-data",
        action="store_true",
        help="仅供代码调试；允许使用SYN/EXAMPLE虚构数据启动训练。正式训练禁止使用。",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "model_name_or_path",
        "train_mode",
        "train_file",
        "validation_file",
        "output_dir",
        "max_length",
        "num_train_epochs",
        "per_device_train_batch_size",
        "gradient_accumulation_steps",
        "learning_rate",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError("训练配置缺少字段：" + ", ".join(missing))
    if config["train_mode"] not in {"full", "qlora"}:
        raise ValueError("train_mode只能是full或qlora")
    return config


def inspect_inputs(config: dict[str, Any]) -> dict[str, Any]:
    train_path = resolve_from_root(config["train_file"])
    validation_path = resolve_from_root(config["validation_file"])
    if not train_path.is_file() or not validation_path.is_file():
        raise FileNotFoundError("请先运行prepare_dataset.py生成train和validation文件")
    train_rows = read_jsonl(train_path)
    validation_rows = read_jsonl(validation_path)
    if not train_rows:
        raise ValueError("训练集为空")
    if not validation_rows:
        raise ValueError("验证集为空")
    model_path = resolve_from_root(config["model_name_or_path"])
    contains_example_data = any(
        str(row.get("metadata", {}).get("participant_id", "")).startswith("SYN")
        or str(row.get("metadata", {}).get("annotator_id", "")) == "EXAMPLE"
        for row in [*train_rows, *validation_rows]
    )
    return {
        "mode": config["train_mode"],
        "model_path": str(model_path),
        "model_downloaded": (model_path / "config.json").is_file(),
        "contains_example_data": contains_example_data,
        "train_records": len(train_rows),
        "validation_records": len(validation_rows),
        "train_sha256": sha256_file(train_path),
        "validation_sha256": sha256_file(validation_path),
        "output_dir": str(resolve_from_root(config["output_dir"])),
    }


def package_versions(names: list[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    args = parse_args()
    config_path = resolve_from_root(args.config)
    config = load_config(config_path)
    inspection = inspect_inputs(config)
    if args.dry_run:
        print(json.dumps({"ok": True, "config": str(config_path), **inspection}, ensure_ascii=False, indent=2))
        return
    if inspection["contains_example_data"] and not args.allow_example_data:
        raise RuntimeError(
            "检测到SYN/EXAMPLE虚构样例。它只能验证格式，不能训练正式模型；请先准备真实人工标注数据。"
        )
    if not inspection["model_downloaded"]:
        raise FileNotFoundError(
            f"未找到完整模型：{inspection['model_path']}。请先运行scripts/download_model.py。"
        )

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    if not torch.cuda.is_available():
        raise RuntimeError("训练脚本要求CUDA GPU；CPU只适合数据准备和静态验证")
    if config.get("bf16", True) and not torch.cuda.is_bf16_supported():
        raise RuntimeError("当前GPU不支持BF16；请使用Ampere或更新架构，或在配置中改用fp16")

    model_path = resolve_from_root(config["model_name_or_path"])
    train_path = resolve_from_root(config["train_file"])
    validation_path = resolve_from_root(config["validation_file"])
    output_dir = resolve_from_root(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, use_fast=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    token_audit: dict[str, Any] = {}
    for split_name, split_path in (("train", train_path), ("validation", validation_path)):
        lengths: list[int] = []
        too_long: list[str] = []
        for row in read_jsonl(split_path):
            messages = [*row["prompt"], *row["completion"]]
            template_kwargs = {
                "tokenize": True,
                "add_generation_prompt": False,
            }
            try:
                token_ids = tokenizer.apply_chat_template(
                    messages, enable_thinking=False, **template_kwargs
                )
            except TypeError:
                token_ids = tokenizer.apply_chat_template(messages, **template_kwargs)
            length = len(token_ids)
            lengths.append(length)
            if length > int(config.get("max_length", 3072)):
                metadata = row.get("metadata", {})
                too_long.append(
                    "/".join(
                        str(metadata.get(key, ""))
                        for key in ("participant_id", "session_id", "turn_id")
                    )
                )
        if too_long:
            raise RuntimeError(
                f"{split_name}有{len(too_long)}条样本超过max_length，不能静默截断assistant标签。"
                f"请人工缩短历史或提高max_length。前5条：{too_long[:5]}"
            )
        token_audit[split_name] = {
            "records": len(lengths),
            "max_tokens": max(lengths, default=0),
            "mean_tokens": sum(lengths) / len(lengths) if lengths else 0.0,
        }

    dtype = torch.bfloat16 if config.get("bf16", True) else torch.float16
    model_kwargs: dict[str, Any] = {
        "dtype": dtype,
        "local_files_only": True,
        "attn_implementation": config.get("attn_implementation", "sdpa"),
        "use_cache": False,
    }
    peft_config = None
    if config["train_mode"] == "qlora":
        local_rank = int(os.environ.get("LOCAL_RANK", "0"))
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type=config.get("bnb_4bit_quant_type", "nf4"),
            bnb_4bit_use_double_quant=bool(config.get("bnb_4bit_use_double_quant", True)),
            bnb_4bit_compute_dtype=dtype,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            quantization_config=quantization_config,
            device_map={"": local_rank},
            **model_kwargs,
        )
        model = prepare_model_for_kbit_training(
            model,
            use_gradient_checkpointing=bool(config.get("gradient_checkpointing", True)),
        )
        peft_config = LoraConfig(
            r=int(config.get("lora_r", 64)),
            lora_alpha=int(config.get("lora_alpha", 128)),
            lora_dropout=float(config.get("lora_dropout", 0.05)),
            target_modules="all-linear",
            bias="none",
            task_type="CAUSAL_LM",
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(model_path, **model_kwargs)

    dataset = load_dataset(
        "json",
        data_files={"train": str(train_path), "validation": str(validation_path)},
    )
    for split in ("train", "validation"):
        if "metadata" in dataset[split].column_names:
            dataset[split] = dataset[split].remove_columns("metadata")

    deepspeed_value = None
    if config["train_mode"] == "full" and config.get("deepspeed"):
        deepspeed_path = resolve_from_root(config["deepspeed"])
        if not deepspeed_path.is_file():
            raise FileNotFoundError(deepspeed_path)
        deepspeed_value = str(deepspeed_path)

    training_args = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=float(config["num_train_epochs"]),
        per_device_train_batch_size=int(config["per_device_train_batch_size"]),
        per_device_eval_batch_size=int(config.get("per_device_eval_batch_size", 1)),
        gradient_accumulation_steps=int(config["gradient_accumulation_steps"]),
        learning_rate=float(config["learning_rate"]),
        weight_decay=float(config.get("weight_decay", 0.0)),
        warmup_ratio=float(config.get("warmup_ratio", 0.0)),
        lr_scheduler_type=str(config.get("lr_scheduler_type", "cosine")),
        logging_steps=int(config.get("logging_steps", 5)),
        logging_first_step=True,
        eval_strategy=str(config.get("eval_strategy", "epoch")),
        eval_steps=int(config.get("eval_steps", 50)),
        save_strategy=str(config.get("save_strategy", "epoch")),
        save_steps=int(config.get("save_steps", 50)),
        save_total_limit=int(config.get("save_total_limit", 3)),
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        bf16=bool(config.get("bf16", True)),
        fp16=not bool(config.get("bf16", True)),
        tf32=bool(config.get("tf32", True)),
        gradient_checkpointing=bool(config.get("gradient_checkpointing", True)),
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_length=int(config.get("max_length", 3072)),
        truncation_mode="keep_start",
        packing=bool(config.get("packing", False)),
        completion_only_loss=True,
        dataset_num_proc=int(config.get("dataset_num_proc", 4)),
        seed=int(config.get("seed", 42)),
        data_seed=int(config.get("seed", 42)),
        report_to=str(config.get("report_to", "none")),
        deepspeed=deepspeed_value,
        remove_unused_columns=True,
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset["validation"],
        processing_class=tokenizer,
        peft_config=peft_config,
    )
    result = trainer.train(resume_from_checkpoint=args.resume_from_checkpoint)
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(output_dir)
    trainer.save_state()

    metrics = dict(result.metrics)
    metrics.update(trainer.evaluate())
    trainer.log_metrics("final", metrics)
    trainer.save_metrics("final", metrics)

    run_manifest = {
        "config": config,
        "config_path": str(config_path),
        "git_commit": git_commit(),
        "dataset": inspection,
        "token_length_audit": token_audit,
        "packages": package_versions(
            ["torch", "transformers", "datasets", "accelerate", "trl", "peft", "bitsandbytes"]
        ),
        "cuda": torch.version.cuda,
        "gpu_count": torch.cuda.device_count(),
        "gpus": [torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())],
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
