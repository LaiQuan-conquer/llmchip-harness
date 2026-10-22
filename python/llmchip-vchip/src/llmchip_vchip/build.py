"""`vchip new`: baseline chips made from a model on disk, to compare hardware chips with.

- `reference`: the float model (quality baseline);
- `numeric`: the L2 integer model of a graph numeric spec built here (calibrated on the
  bundled evaluation set of the tokenizer's kind), the bit-exact golden of a hardware chip
  built from the same spec.

The model's files are referenced by absolute path, not copied; their hashes are recorded
with every run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
from llmchip_model.tokens import TextTokenizer
from llmchip_numeric import graph_eval, graph_l2

from .adapters import model_source
from .manifest import FORMAT


def _base(src: model_source.ModelSource, chip_id: str, backend: str, context: int) -> dict:
    if src.tokenizer is None:
        raise ValueError(f"no tokenizer (tokenizer.json or a llama2.c tokenizer .bin) beside {src.path}")
    kind = "hf" if src.tokenizer.suffix == ".json" else "llama2c"
    return {
        "format": FORMAT,
        "id": chip_id,
        "backend": backend,
        "description": f"{backend} chip of {src.path.name}, made by `vchip new`",
        "model": {
            "family": src.family,
            "params": src.params,
            "context": context,
            "vocab": src.vocab,
            "source": str(src.path),
        },
        "tokenizer": {"kind": kind, "path": str(src.tokenizer.resolve()), "stop": src.stop_tokens()},
        "artifacts": {"model": str(src.path.resolve())},
        "clock_mhz": None,
        "host": {"kind": "direct"},
        "capabilities": ["greedy", "logits"],
        "prompts": "prompts.json",
    }


def new_chip(
    backend: str,
    model: Path,
    chip_id: str,
    chips_dir: Path,
    *,
    context: int | None = None,
    weight_format: str = "int8",
    group_size: int = 32,
    progress=print,
) -> Path:
    model = Path(model).expanduser()
    src = model_source.load(model, context or 256)
    context = context or min(src.max_positions or 256, 256)
    if src.max_positions and context > src.max_positions:
        raise ValueError(f"context {context} exceeds the model's {src.max_positions} positions")
    chip = _base(src, chip_id, backend, context)
    out = chips_dir / chip_id
    if out.exists():
        raise ValueError(f"{out} exists")
    out.mkdir(parents=True)
    try:
        kind = chip["tokenizer"]["kind"]
        evalset = graph_eval.default_evalset(kind)
        (out / "prompts.json").write_text(json.dumps(evalset, indent=2, ensure_ascii=False))
        if backend == "numeric":
            tok = TextTokenizer(src.tokenizer, kind, src.vocab)
            progress("calibrating the float graph on the bundled evaluation set")
            stats = graph_l2.calibrate(
                src.graph, src.weights.get, graph_eval.calibration_sequences(evalset, tok, context)
            )
            progress(f"quantizing ({weight_format}, groups of {group_size})")
            spec, arrays = graph_l2.build(
                src.graph,
                src.weights.peek,
                weight_format=weight_format,
                group_size=group_size,
                context=context,
                per_layer=True,
                residual_bits=24,
                stats=stats,
            )
            (out / "numeric-spec.json").write_text(json.dumps(spec, indent=2))
            np.savez(
                out / "l2-weights.npz", meta=np.array(json.dumps({"format": graph_l2.GRAPH_WEIGHTS_FORMAT})), **arrays
            )
            chip["artifacts"] |= {"spec": "numeric-spec.json", "weights": "l2-weights.npz"}
            chip["description"] += f" ({weight_format}, groups of {group_size})"
        elif backend != "reference":
            raise ValueError(f"`vchip new` makes reference and numeric chips, not {backend}")
        (out / "chip.json").write_text(json.dumps(chip, indent=2, ensure_ascii=False) + "\n")
    except BaseException:
        shutil.rmtree(out)
        raise
    return out
