"""Prepare the B5 prompt set and its GraphL2 golden."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

REPO = Path("/root/projects/deepseek-harness/llmchip-harness")
for part in ("plugins/model-llama/src", "plugins/numeric/src"):
    sys.path.insert(0, str(REPO / part))

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", "/root/projects/deepseek-harness/tools"))
MODEL = TOOLS / "models/Qwen2.5-0.5B-Instruct"
RUN = Path(os.environ.get("LLMCHIP_B5_RUN", "/root/llmchip-runs/inc001"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def questions(n: int) -> list[dict]:
    from datasets import load_dataset

    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    ds = load_dataset("cais/mmlu", "all", split="test", cache_dir=str(TOOLS / "hf/datasets"))
    rows = []
    for i in range(n):
        row = ds[i]
        choices = "\n".join(f"{c}. {t}" for c, t in zip("ABCD", row["choices"]))
        rows.append({"id": f"mmlu-{i}", "text": f"{row['question']}\n{choices}\nAnswer:"})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--tokens", type=int, default=64)
    args = ap.parse_args()
    from transformers import AutoTokenizer

    from llmchip_model.census import census
    from llmchip_model.formats import WeightLoader, read_layout
    from llmchip_model.graph import recover
    from llmchip_numeric import graph_l2

    RUN.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(MODEL)
    rows = questions(args.n)
    for row in rows:
        row["tokens"] = tok(row["text"], truncation=True, max_length=128)["input_ids"]
    path = MODEL / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((MODEL / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [rows[0]["tokens"][:4]])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=128,
                                  stats=stats, context=512, residual_bits=24)
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    golden = []
    for row in rows:
        ids = list(row["tokens"])
        out = []
        for _ in range(args.tokens):
            nxt = int(np.argmax(l2.forward(ids)[-1]))
            ids.append(nxt)
            out.append(nxt)
        golden.append({"id": row["id"], "tokens": row["tokens"], "expected": out})
        print("golden", row["id"], len(out), flush=True)
    (RUN / "prompts.json").write_text(json.dumps({"model": str(MODEL), "n": len(rows), "prompts": rows}, indent=2))
    (RUN / "golden.json").write_text(json.dumps({"format": "llmchip-b5-golden/1", "golden": golden}, indent=2))
    identity = {"model": str(MODEL), "weights": sha(path), "spec": sha(MODEL / "config.json"),
                "tokenizer": sha(MODEL / "tokenizer.json"), "prompts": sha(RUN / "prompts.json"),
                "tokens": args.tokens, "n": len(rows)}
    (RUN / "prepare-identity.json").write_text(json.dumps(identity, indent=2, sort_keys=True))
    print("wrote", RUN, flush=True)


if __name__ == "__main__":
    main()
