from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import resolve_from_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把QLoRA适配器合并到完整Qwen3权重。")
    parser.add_argument("--base-model", default="models/Qwen3-8B")
    parser.add_argument("--adapter", default="outputs/qwen3-8b-qlora")
    parser.add_argument("--output", default="outputs/qwen3-8b-merged")
    parser.add_argument("--max-shard-size", default="4GB")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_path = resolve_from_root(args.base_model)
    adapter_path = resolve_from_root(args.adapter)
    output_path = resolve_from_root(args.output)
    if not (base_path / "config.json").is_file():
        raise FileNotFoundError(base_path / "config.json")
    if not (adapter_path / "adapter_config.json").is_file():
        raise FileNotFoundError(adapter_path / "adapter_config.json")
    if output_path.exists():
        raise FileExistsError(f"输出目录已存在：{output_path}")

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(base_path, local_files_only=True)
    base_model = AutoModelForCausalLM.from_pretrained(
        base_path,
        local_files_only=True,
        dtype=torch.bfloat16,
        device_map="cpu",
        low_cpu_mem_usage=True,
    )
    model = PeftModel.from_pretrained(base_model, adapter_path, local_files_only=True)
    merged = model.merge_and_unload(safe_merge=True)
    output_path.mkdir(parents=True, exist_ok=False)
    merged.save_pretrained(
        output_path,
        safe_serialization=True,
        max_shard_size=args.max_shard_size,
    )
    tokenizer.save_pretrained(output_path)
    (output_path / "merge_manifest.json").write_text(
        json.dumps(
            {
                "base_model": str(base_path),
                "adapter": str(adapter_path),
                "output": str(output_path),
                "dtype": "bfloat16",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
