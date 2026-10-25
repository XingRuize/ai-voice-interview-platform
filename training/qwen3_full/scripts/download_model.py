from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import resolve_from_root, sha256_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载完整Qwen3权重到可离线复制的本地目录。")
    parser.add_argument("--model", default="Qwen/Qwen3-8B")
    parser.add_argument("--output", default="models/Qwen3-8B")
    parser.add_argument("--revision", default=None, help="可选分支、标签或提交；留空使用平台默认值。")
    parser.add_argument("--source", choices=["huggingface", "modelscope"], default="huggingface")
    parser.add_argument("--token", default=None, help="可选。也可使用HF_TOKEN环境变量。")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--skip-checksum", action="store_true")
    return parser.parse_args()


def download_huggingface(args: argparse.Namespace, output: Path) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=args.model,
        revision=args.revision,
        local_dir=output,
        token=args.token,
        max_workers=args.max_workers,
    )


def download_modelscope(args: argparse.Namespace, output: Path) -> None:
    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise SystemExit("使用ModelScope前请执行：pip install modelscope") from exc
    downloaded = Path(
        snapshot_download(
            args.model,
            revision=args.revision,
            local_dir=str(output),
            max_workers=args.max_workers,
        )
    ).resolve()
    if downloaded != output.resolve():
        raise RuntimeError(f"ModelScope返回了意外目录：{downloaded}")


def main() -> None:
    args = parse_args()
    output = resolve_from_root(args.output)
    output.mkdir(parents=True, exist_ok=True)

    if args.source == "huggingface":
        download_huggingface(args, output)
    else:
        download_modelscope(args, output)

    required = [output / "config.json", output / "tokenizer_config.json"]
    missing = [str(path) for path in required if not path.is_file()]
    weights = sorted(output.glob("*.safetensors"))
    if missing or not weights:
        raise RuntimeError(f"模型下载不完整；缺少={missing}，权重文件数={len(weights)}")

    files = [path for path in output.rglob("*") if path.is_file()]
    manifest = {
        "model": args.model,
        "revision": args.revision or "platform-default",
        "source": args.source,
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "sha256": {}
        if args.skip_checksum
        else {str(path.relative_to(output)).replace("\\", "/"): sha256_file(path) for path in files},
    }
    (output / "download_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
