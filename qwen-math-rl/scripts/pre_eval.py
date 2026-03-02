#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pre-eval script (from notebook pre.ipynb), cleaned & productionized.

What it does:
1) Load GSM8K (main) test split, take first N samples (default 200).
2) Run deterministic generation with Qwen/Qwen2.5-0.5B-Instruct.
3) Extract predicted numeric answer and GT answer, write predictions.jsonl.
4) Bucket common failure modes (extract_fail / likely_truncated / boxed_but_wrong / hash_but_wrong / wrong_other),
   and save a short report under ./runs/<run_name>/.

Usage:
  python pre_eval_gsm8k_qwen25.py
  python pre_eval_gsm8k_qwen25.py --n_samples 200 --out_root ./runs --max_new_tokens 256 --dtype auto

Notes:
- This is an evaluation/sanity-check script, not training.
- Designed to be Windows-friendly (short paths, short run names).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import os
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple, List

import torch
from datasets import load_dataset
from tqdm.auto import tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM
from transformers import StoppingCriteria, StoppingCriteriaList


DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_DATASET = "gsm8k"
DEFAULT_CONFIG = "main"


# -----------------------------
# Helpers: run dir naming
# -----------------------------
def model_short_name(model_id: str) -> str:
    """Shorten model id to avoid very long paths (esp. on Windows)."""
    s = model_id.lower()
    s = s.replace("qwen/qwen", "qwen")
    s = s.replace("/", "-")
    s = s.replace(".", "p")
    s = s.replace("instruct", "inst")
    # keep only safe chars
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    return s[:32] if len(s) > 32 else s


def make_run_name(stage: str, model_id: str, n_samples: int, max_new_tokens: int) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M")
    m = model_short_name(model_id)
    return f"{stage}_{m}_{DEFAULT_DATASET}_n{n_samples}_t{max_new_tokens}_{ts}"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


# -----------------------------
# Helpers: answer extraction
# -----------------------------
_GT_RE = re.compile(r"####\s*([-+]?\d[\d,]*(?:\.\d+)?)")
_BOX_RE = re.compile(r"\\boxed\{\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*\}")
_HASH_RE = re.compile(r"####\s*([-+]?\d[\d,]*(?:\.\d+)?)")


def _normalize_num_str(s: str) -> Optional[str]:
    """Normalize number string for stable comparison.
    - Remove commas
    - Convert '12.0' -> '12'
    - Handle simple fractions 'a/b' if present
    """
    if s is None:
        return None
    s = str(s).strip()
    s = s.replace(",", "")
    if not s:
        return None

    # fraction a/b
    if re.fullmatch(r"[-+]?\d+\s*/\s*\d+", s):
        a, b = re.split(r"\s*/\s*", s)
        try:
            val = Decimal(a) / Decimal(b)
            # Quantize not necessary; just normalize
            return format(val.normalize(), "f").rstrip("0").rstrip(".") if "." in format(val, "f") else str(val)
        except (InvalidOperation, ZeroDivisionError):
            return None

    # decimal / int
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None

    # normalize 12.0 -> 12
    if d == d.to_integral_value():
        return str(d.to_integral_value())
    # keep as plain string (no scientific notation)
    out = format(d.normalize(), "f")
    # avoid trailing zeros
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out


def extract_gsm8k_gt(answer_field: str) -> Optional[str]:
    """Extract GSM8K ground-truth answer (the numeric after ####)."""
    m = _GT_RE.search(answer_field or "")
    if not m:
        return None
    return _normalize_num_str(m.group(1))


def extract_model_answer(text: str) -> Optional[str]:
    """Extract model predicted answer from model output."""
    if text is None:
        return None
    # 0) boxed
    m = _BOX_RE.search(text)
    if m:
        return _normalize_num_str(m.group(1))

    # 1) ####
    m = _HASH_RE.search(text)
    if m:
        return _normalize_num_str(m.group(1))

    # 2) Final Answer / Answer:
    m = re.search(r"(?:Final\s*Answer|Answer)\s*[:=]\s*([-+]?\d[\d,]*(?:\.\d+)?)", text, re.I)
    if m:
        return _normalize_num_str(m.group(1))

    # 3) fallback: last number in text
    cleaned = re.sub(r"(?m)^\s*\d+\.\s+", "", text)  # strip "1. 2. ..." step prefixes
    nums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", cleaned)
    if not nums:
        return None
    return _normalize_num_str(nums[-1])


# -----------------------------
# Stop criteria: stop when final answer appears
# -----------------------------
class StopOnFinalAnswer(StoppingCriteria):
    """Stop generation when output tail ends with '#### <num>' or '\\boxed{<num>}'."""
    def __init__(self, tokenizer, window: int = 256):
        super().__init__()
        self.tokenizer = tokenizer
        self.window = window
        self.pattern = re.compile(
            r"(?:####\s*[-+]?\d[\d,]*(?:\.\d+)?\s*(?:[.。])?\s*$|\\boxed\{\s*[-+]?\d[\d,]*(?:\.\d+)?\s*\}\s*$)"
        )

    def __call__(self, input_ids, scores, **kwargs):
        tail = input_ids[0][-self.window:]
        text = self.tokenizer.decode(tail, skip_special_tokens=False)
        return self.pattern.search(text) is not None


# -----------------------------
# Bucketing errors
# -----------------------------
def is_likely_truncated(text: str) -> bool:
    tail = (text or "").strip()[-80:]
    # very rough heuristics; good enough for quick triage
    return ("..." in tail) or tail.endswith(",") or tail.endswith(":") or tail.endswith("and") or tail.endswith("so")


def has_boxed(text: str) -> bool:
    return r"\boxed" in (text or "")


def has_hash(text: str) -> bool:
    return "####" in (text or "")


# -----------------------------
# Model & dataset
# -----------------------------
def pick_torch_dtype(dtype: str) -> Optional[torch.dtype]:
    dtype = (dtype or "auto").lower()
    if dtype == "auto":
        if torch.cuda.is_available():
            # prefer bf16 if supported
            if getattr(torch.cuda, "is_bf16_supported", None) and torch.cuda.is_bf16_supported():
                return torch.bfloat16
            return torch.float16
        return torch.float32
    if dtype in ("bf16", "bfloat16"):
        return torch.bfloat16
    if dtype in ("fp16", "float16"):
        return torch.float16
    if dtype in ("fp32", "float32"):
        return torch.float32
    raise ValueError(f"Unknown dtype: {dtype}")


def load_model_and_tokenizer(model_id: str, cache_dir: Optional[str], dtype: str):
    torch_dtype = pick_torch_dtype(dtype)
    device_map = "auto" if torch.cuda.is_available() else None

    tokenizer = AutoTokenizer.from_pretrained(model_id, cache_dir=cache_dir, trust_remote_code=True)

    # Make sure pad token exists for batching
    if tokenizer.pad_token is None:
        if tokenizer.eos_token is not None:
            tokenizer.pad_token = tokenizer.eos_token
        else:
            tokenizer.add_special_tokens({"pad_token": "<|pad|>"})

    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        cache_dir=cache_dir,
        torch_dtype=torch_dtype,
        device_map=device_map,
        trust_remote_code=True,
    )

    # If we added tokens, resize embeddings
    if getattr(model, "resize_token_embeddings", None) and len(tokenizer) > model.get_input_embeddings().num_embeddings:
        model.resize_token_embeddings(len(tokenizer))

    model.eval()
    return model, tokenizer


def load_gsm8k_test(n_samples: int) -> List[Dict[str, Any]]:
    ds = load_dataset(DEFAULT_DATASET, DEFAULT_CONFIG)
    test = ds["test"].select(range(n_samples))
    return list(test)


# -----------------------------
# Core run
# -----------------------------
def run_eval(
    model_id: str,
    out_root: str,
    n_samples: int,
    max_new_tokens: int,
    cache_dir: Optional[str],
    dtype: str,
    seed: int,
) -> str:
    stage = "pre_eval"
    run_name = make_run_name(stage=stage, model_id=model_id, n_samples=n_samples, max_new_tokens=max_new_tokens)
    run_dir = ensure_dir(os.path.join(out_root, run_name))
    ensure_dir(os.path.join(run_dir, "buckets"))

    # save run config early
    cfg = {
        "stage": stage,
        "model_id": model_id,
        "dataset": f"{DEFAULT_DATASET}/{DEFAULT_CONFIG}",
        "n_samples": n_samples,
        "max_new_tokens": max_new_tokens,
        "cache_dir": cache_dir,
        "dtype": dtype,
        "seed": seed,
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }
    with open(os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # set seed (mostly for reproducibility; we use greedy decode anyway)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    model, tokenizer = load_model_and_tokenizer(model_id, cache_dir=cache_dir, dtype=dtype)
    stopping_criteria = StoppingCriteriaList([StopOnFinalAnswer(tokenizer)])

    data = load_gsm8k_test(n_samples=n_samples)

    system_prompt = "You are a math assistant."
    predictions_path = os.path.join(run_dir, "predictions.jsonl")

    # inference loop
    with open(predictions_path, "w", encoding="utf-8") as f:
        for sample in tqdm(data, total=len(data), desc="Generating"):
            question = sample["question"]
            gt_answer = extract_gsm8k_gt(sample["answer"])

            user_prompt = (
                question
                + "\n\nGive the final answer at the end in ONE of these formats:\n"
                  "1) #### <number>\n"
                  "OR 2) \\boxed{<number>}\n"
                  "Do not write anything after the final answer line."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            # chat template (Qwen instruct)
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = tokenizer(text, return_tensors="pt")

            # Move to model device (works with device_map too)
            if hasattr(model, "device"):
                inputs = {k: v.to(model.device) for k, v in inputs.items()}
            else:
                # fallback
                device = "cuda" if torch.cuda.is_available() else "cpu"
                inputs = {k: v.to(device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    temperature=0.0,
                    pad_token_id=tokenizer.eos_token_id,
                    stopping_criteria=stopping_criteria,
                )

            gen_ids = outputs[0][inputs["input_ids"].shape[-1]:]
            model_output = tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
            pred_answer = extract_model_answer(model_output)

            f.write(json.dumps({
                "question": question,
                "model_output": model_output,
                "pred_answer": pred_answer,
                "gt_answer": gt_answer,
            }, ensure_ascii=False) + "\n")

    # analysis
    stats, buckets = analyze_predictions(predictions_path)

    # write report
    report_path = os.path.join(run_dir, "report.txt")
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write(f"Run dir: {run_dir}\n")
        rf.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n\n")
        rf.write("Stats:\n")
        for k, v in stats.items():
            rf.write(f"  {k}: {v}\n")
        rf.write("\nBuckets:\n")
        for k in sorted(buckets.keys()):
            rf.write(f"  {k}: {len(buckets[k])}\n")

    # dump a few examples per bucket
    dump_examples(run_dir, buckets, per_bucket=10)

    # print quick summary
    correct = stats.get("correct", 0)
    total = correct + stats.get("wrong", 0) + stats.get("extract_fail", 0)
    acc = (correct / total) if total else 0.0
    print(f"\nDone. Run dir: {run_dir}")
    print(f"Accuracy (on parsed answers): {acc:.3%}  | correct={correct} total={total}")
    print(f"Predictions: {predictions_path}")
    print(f"Report: {report_path}")
    return run_dir


def analyze_predictions(predictions_jsonl: str) -> Tuple[Counter, Dict[str, List[Dict[str, Any]]]]:
    stats: Counter = Counter()
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    with open(predictions_jsonl, encoding="utf-8") as f:
        for line in f:
            ex = json.loads(line)
            pred = ex.get("pred_answer")
            gt = ex.get("gt_answer")
            out = ex.get("model_output", "")

            if pred is None:
                stats["extract_fail"] += 1
                buckets["extract_fail"].append(ex)
                continue

            if gt is not None and str(pred) == str(gt):
                stats["correct"] += 1
                continue

            stats["wrong"] += 1

            # Bucketing heuristics
            if is_likely_truncated(out):
                tag = "likely_truncated"
            elif has_boxed(out):
                tag = "boxed_but_wrong"
            elif has_hash(out):
                tag = "hash_but_wrong"
            else:
                tag = "wrong_other"

            buckets[tag].append(ex)

    return stats, buckets


def dump_examples(run_dir: str, buckets: Dict[str, List[Dict[str, Any]]], per_bucket: int = 10) -> None:
    bucket_dir = ensure_dir(os.path.join(run_dir, "buckets"))
    for name, arr in buckets.items():
        out_path = os.path.join(bucket_dir, f"{name}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            for i, ex in enumerate(arr[:per_bucket]):
                f.write("=" * 80 + "\n")
                f.write(f"[{i}] GT: {ex.get('gt_answer')} | Pred: {ex.get('pred_answer')}\n")
                f.write("Q: " + (ex.get("question") or "") + "\n\n")
                mo = ex.get("model_output") or ""
                f.write("OUT (tail):\n" + mo[-600:] + "\n")


# -----------------------------
# CLI
# -----------------------------
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pre-eval GSM8K with Qwen2.5-0.5B-Instruct; outputs under ./runs/")
    p.add_argument("--model_id", type=str, default=DEFAULT_MODEL, help="Model id/path. Default is Qwen2.5-0.5B-Instruct.")
    p.add_argument("--out_root", type=str, default="./runs", help="Output root directory.")
    p.add_argument("--n_samples", type=int, default=200, help="Number of GSM8K test samples to evaluate (from the start).")
    p.add_argument("--max_new_tokens", type=int, default=256, help="Max new tokens to generate per problem.")
    p.add_argument("--cache_dir", type=str, default=None, help="HF cache directory.")
    p.add_argument("--dtype", type=str, default="auto", choices=["auto", "bf16", "fp16", "fp32"], help="Model dtype.")
    p.add_argument("--seed", type=int, default=42, help="Random seed.")
    return p


def main():
    args = build_argparser().parse_args()

    # Force keeping only Qwen2.5 model by default; still allow override if you really want.
    # If you want to *hard-lock* it, replace the next two lines with: args.model_id = DEFAULT_MODEL
    if args.model_id != DEFAULT_MODEL:
        print(f"[warn] model_id != default ({DEFAULT_MODEL}). You asked to keep only 2.5; consider leaving it default.")

    run_eval(
        model_id=args.model_id,
        out_root=args.out_root,
        n_samples=args.n_samples,
        max_new_tokens=args.max_new_tokens,
        cache_dir=args.cache_dir,
        dtype=args.dtype,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
