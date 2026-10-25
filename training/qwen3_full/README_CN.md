# Qwen3 完整版受控访谈模型训练说明

这套代码用于训练“大模型下一步怎样访谈”，不是训练语音识别、语音合成或流畅度评分。输入是当前核心题、参与者回答、最近两轮对话和下一道核心题；模型输出严格的 JSON 决策，包括是否进入下一题、是否追问、是否暂停或是否需要人工接管。

默认基础模型是官方完整权重 `Qwen/Qwen3-8B`。仓库同时提供两种训练方式：

- `configs/qwen3_8b_full.json`：全参数监督微调，更新全部 8B 参数。它是本项目所说的“完整版”。
- `configs/qwen3_8b_qlora.json`：单卡显存不足时使用 QLoRA。基础模型仍是完整 Qwen3-8B，训练结束后可把适配器合并为一个独立模型。

本目录不含模型权重、真实访谈数据或训练结果，也没有执行训练。`data/annotated_turns.example.jsonl` 全部是虚构示例，只用于检查格式，不能作为研究训练数据。

## 1. 模型学习什么

每个训练样本对应一轮完整问答，不要把同一段回答随意切成多个独立样本。模型固定输出以下字段：

```json
{
  "action": "NEXT",
  "followup_type": "NONE",
  "focus_quote": "",
  "interviewer_text": "下一道经过审核的核心问题",
  "next_question_id": "Q04_GENDER_PRIORITIES",
  "reason_code": "answer_complete"
}
```

`action` 只能是：

- `NEXT`：进入下一道适用的核心题。
- `FOLLOWUP`：提出一条人工批准的非诱导追问。
- `PAUSE`：参与者需要停顿、休息或自行决定是否继续。
- `HANDOFF`：出现安全风险，停止自动访谈并交给研究人员。
- `END`：访谈结束。

`followup_type` 只能是 `NONE`、`CLARIFY`、`TIMELINE`、`DETAIL`、`IMPACT`、`EMOTION`、`COMMUNICATION`、`SUPPORT` 或 `SAFETY`。

训练只优化 assistant 的结构化输出，不对 system 和 user 提示内容计算损失。模型不能自由扩大访谈范围；题纲仍是研究范围的唯一来源。

## 2. 目录说明

```text
training/qwen3_full/
├── configs/
│   ├── qwen3_8b_full.json       全参数训练配置
│   ├── qwen3_8b_qlora.json      单卡QLoRA配置
│   └── deepspeed_zero3.json     多卡ZeRO-3配置
├── data/
│   ├── interview_outline.json   根据正式题纲整理的机器可读题库
│   └── annotated_turns.example.jsonl
├── scripts/
│   ├── download_model.py        下载完整模型并生成校验清单
│   ├── prepare_dataset.py       转换数据并按参与者分组切分
│   ├── validate_dataset.py      检查标签、诱导风险、PII和数据泄漏
│   ├── train_sft.py             全参数或QLoRA训练入口
│   ├── evaluate_model.py        最终留出测试集评价
│   └── merge_adapter.py         把QLoRA适配器合并为独立模型
├── requirements.txt
└── requirements-full.txt
```

运行后产生的 `models/`、`prepared/`、`outputs/`、`evaluation/` 和 `wheelhouse/` 均被 Git 忽略。

## 3. 服务器和系统要求

建议使用 Ubuntu 22.04 或 24.04、Python 3.11、NVIDIA 驱动和 CUDA 12.4 以上。Qwen 官方训练文档要求 Ampere 或更新架构以使用 BF16 和 Flash Attention：<https://qwen.readthedocs.io/en/latest/training/axolotl.html>。

全参数 8B 微调需要保存权重、梯度、优化器状态和激活值。建议从 4 张 80GB GPU 起步，并预留至少 300GB 本地磁盘；实际需求会随序列长度、批大小、DeepSpeed 和驱动版本变化。单张 24GB 至 32GB GPU 优先使用 QLoRA，并从当前的 batch size 1、长度 3072 开始测试。显存数字是部署起点，不是性能保证。

Windows 可以准备和验证数据，但正式训练建议在 Linux GPU 服务器进行。训练过程中不要把患者数据上传到公共模型平台、实验跟踪平台或公共 Git 仓库。

## 4. 从 GitHub 下载代码

```bash
git clone --recursive https://github.com/XingRuize/ai-voice-interview-platform.git
cd ai-voice-interview-platform/training/qwen3_full
```

如果已经下载过仓库：

```bash
git pull
cd training/qwen3_full
```

## 5. 安装训练环境

先创建独立环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

PyTorch 必须根据服务器驱动单独安装。CUDA 12.8 示例：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

安装通用训练依赖：

```bash
pip install -r requirements.txt
```

全参数训练还要安装 DeepSpeed：

```bash
pip install -r requirements-full.txt
```

安装完成后检查 GPU：

```bash
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
```

## 6. 下载完整 Qwen3-8B

从 Hugging Face 下载：

```bash
python scripts/download_model.py \
  --source huggingface \
  --model Qwen/Qwen3-8B \
  --output models/Qwen3-8B
```

在中国网络环境中也可使用 ModelScope。先安装 `modelscope`，再下载：

```bash
pip install modelscope
python scripts/download_model.py \
  --source modelscope \
  --model Qwen/Qwen3-8B \
  --output models/Qwen3-8B
```

脚本会确认配置、分词器和 Safetensors 权重是否存在，并生成 `models/Qwen3-8B/download_manifest.json`。官方 Hugging Face 模型页显示 Qwen3-8B 原始仓库约 16.4GB：<https://huggingface.co/Qwen/Qwen3-8B>。

### 完全离线训练

在一台允许联网、且与训练服务器相同的 Linux 和 Python 环境中完成以下操作：

```bash
mkdir -p wheelhouse
pip download torch --index-url https://download.pytorch.org/whl/cu128 -d wheelhouse
pip download -r requirements-full.txt -d wheelhouse
python scripts/download_model.py --model Qwen/Qwen3-8B --output models/Qwen3-8B
```

把整个仓库目录、`wheelhouse/` 和 `models/Qwen3-8B/` 通过获准的加密介质导入离线服务器。离线安装：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --no-index --find-links wheelhouse torch
pip install --no-index --find-links wheelhouse -r requirements-full.txt
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

训练脚本使用 `local_files_only=True`，不会在训练时连接模型网站；配置中的 `report_to` 也是 `none`。

## 7. 准备人工标注数据

真实标注文件应放在仓库外的受控目录，或放在已被 Git 忽略的 `private_data/`。支持 JSONL、JSON 和 CSV。每一行至少需要：

| 字段 | 说明 |
| --- | --- |
| `participant_id` | 匿名参与者编号；同一个人的全部轮次必须一致 |
| `session_id` | 一次访谈编号 |
| `turn_id` | 本次问答轮次编号 |
| `condition` | 实验条件，例如 baseline 或 healthcare |
| `question_id` | 必须来自 `interview_outline.json` |
| `section` | 题目所属部分 |
| `question_text` | AI 实际提出的问题 |
| `corrected_text` | 人工核对后的逐字文本，保留停顿、填充词和改口标记 |
| `history` | 可选，最近对话的 JSON 数组 |
| `next_question_id` | 下一道适用核心题编号；条件题跳过时必须人工填写 |
| `next_question_text` | 与下一题编号对应的文本 |
| `followup_needed` | 0 或 1 |
| `followup_type` | 九种标签之一 |
| `focus_quote` | 追问时必须逐字来自 `corrected_text` |
| `approved_followup` | 两名标注者确认的非诱导追问；FOLLOWUP 时必填 |
| `distress_flag` | 是否需要减慢或暂停 |
| `safety_flag` | 是否必须人工接管 |
| `reason_code` | 简短、预先定义的决策原因码 |
| `annotator_id` | 标注者编号，不填写姓名 |

先用虚构示例检查完整流程：

```bash
python scripts/prepare_dataset.py --overwrite
python scripts/validate_dataset.py
```

使用真实标注文件：

```bash
python scripts/prepare_dataset.py \
  --input /secure_data/annotated_turns.jsonl \
  --output-dir prepared \
  --train-ratio 0.70 \
  --validation-ratio 0.15 \
  --seed 20261022 \
  --overwrite

python scripts/validate_dataset.py --data-dir prepared
```

脚本按 `participant_id` 切分，而不是随机切分问答轮次。至少需要 3 名参与者才能产生互不重叠的三个集合；这只是程序最低要求，不代表样本量足以训练或发表。`dataset_manifest.json` 会记录各集合样本数、类别分布和文件 SHA-256。

校验器默认阻止常见手机号、邮箱和中国身份证号进入训练文本。发现错误必须回到标注文件修改，不要使用 `--allow-pii` 绕过正式数据检查。

## 8. 训练前只做检查

以下命令不加载模型、不占用 GPU，也不训练：

```bash
python scripts/train_sft.py \
  --config configs/qwen3_8b_full.json \
  --dry-run
```

看到 `"ok": true` 只说明路径、配置和数据文件可以读取，不代表模型已经训练。

训练入口会拒绝 `participant_id` 以 `SYN` 开头或 `annotator_id=EXAMPLE` 的虚构样例，避免误把格式示例当作正式训练数据。正式启动前还会用 Qwen 分词器检查每条样本；超过 `max_length` 时直接停止，不会静默截掉回答或标准输出。

## 9. 全参数完整版训练

先确认 `prepared/` 来自正式数据，配置与预注册方案一致。四张 GPU 示例：

```bash
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

torchrun --standalone --nproc_per_node=4 \
  scripts/train_sft.py \
  --config configs/qwen3_8b_full.json
```

输出位于 `outputs/qwen3-8b-full/`。它是完整独立模型，不需要再合并适配器。中断后续训：

```bash
torchrun --standalone --nproc_per_node=4 \
  scripts/train_sft.py \
  --config configs/qwen3_8b_full.json \
  --resume-from-checkpoint outputs/qwen3-8b-full/checkpoint-XXXX
```

不要盲目增加 epoch。先看验证集 loss、人工接受率和少数类召回，再决定是否继续。

## 10. 单卡 QLoRA 训练

如果只有一张 24GB 至 32GB 显卡：

```bash
export CUDA_VISIBLE_DEVICES=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

python scripts/train_sft.py \
  --config configs/qwen3_8b_qlora.json
```

输出 `outputs/qwen3-8b-qlora/` 只包含适配器和训练记录。需要独立模型时合并：

```bash
python scripts/merge_adapter.py \
  --base-model models/Qwen3-8B \
  --adapter outputs/qwen3-8b-qlora \
  --output outputs/qwen3-8b-merged
```

Hugging Face PEFT 文档说明，`merge_and_unload()` 会把适配器合并进基础权重，得到不依赖 PEFT 适配器的独立模型：<https://huggingface.co/docs/peft/en/package_reference/lora>。

## 11. 最终测试

测试集必须在模型、阈值和提示词全部锁定后使用一次。全参数模型：

```bash
python scripts/evaluate_model.py \
  --model outputs/qwen3-8b-full \
  --test-file prepared/test.jsonl \
  --output-dir evaluation/full
```

未合并的 QLoRA：

```bash
python scripts/evaluate_model.py \
  --model models/Qwen3-8B \
  --adapter outputs/qwen3-8b-qlora \
  --load-in-4bit \
  --test-file prepared/test.jsonl \
  --output-dir evaluation/qlora
```

结果包括 JSON 合法率、action accuracy、追问类型 Macro-F1、各类 precision/recall/F1、SUPPORT recall、SAFETY recall 和混淆矩阵。正式报告还必须由研究人员盲法评价追问的相关性、非诱导性、必要性和可接受率。

## 12. 与当前网页程序的关系

当前 `web_app.py` 使用 Ollama 中的 Qwen3-4B，并由固定模板生成追问。这套训练代码输出的模型增加了 `PAUSE`、`HANDOFF`、九种追问类型和经过人工批准的 `interviewer_text`。训练完成后还需要单独完成运行时适配、量化、Ollama 或其他本地推理服务导入，以及同样严格的输出校验；本次提交按要求只提供训练代码，不替换现有可用的访谈程序。

无论离线指标多高，SAFETY 都不能只交给模型。上线时仍要保留关键词或规则检测、人工接管、日志审计和参与者主动暂停/退出按钮。

## 13. 正式训练前的停止条件

出现以下任一情况时不要开始正式训练：

- 题纲、标签定义或伦理方案仍在修改。
- 同一参与者出现在多个数据集合。
- 自动转写没有经过人工核对。
- FOLLOWUP 没有人工批准的标准追问。
- SUPPORT 或 SAFETY 样本太少，无法单独评价召回。
- 标注者一致性不足，且尚未修改规则重新试标。
- 数据目录、模型目录或日志可能同步到公网。

先稳定标注和实验设计，再训练模型。训练代码可以运行，不等于模型已经适合临床或研究现场使用。
