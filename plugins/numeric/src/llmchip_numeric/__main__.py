"""IF-2 entry point of the numeric plugin: quantize (S2), L2 inference, evaluation (G1)."""

import json
from pathlib import Path

import numpy as np
from llmchip_model import Model, Tokenizer, load_ir
from llmchip_model.evalset import load_evalset
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from .evaluate import evaluate
from .l2 import L2Model
from .spec import build, storage_bits


def quantize(ctx: InvokeContext) -> Result:
    ir = ctx.input("ir")
    cfg, weights = load_ir(ir.path)
    tok_input = ctx.input("tokenizer")
    tokenizer = Tokenizer(Path(tok_input.path).read_bytes(), cfg.vocab_size)
    evalset = load_evalset(ctx.input("evalset").path)
    p = ctx.params
    spec, npz = build(
        cfg,
        weights,
        tokenizer,
        evalset.get("calibration") or evalset["prompts"],
        weight_format=p.get("weightFormat", "int4"),
        group_size=p.get("groupSize", 32),
        context=p.get("contextLength", 64),
        ir_hash=ir.hash,
        tokenizer_hash=tok_input.hash,
    )
    out = Path(ctx.out_dir)
    (out / "numeric-spec.json").write_text(json.dumps(spec, indent=2))
    (out / "l2-weights.npz").write_bytes(npz)
    model = L2Model.load(out / "numeric-spec.json", out / "l2-weights.npz")
    bits = storage_bits(spec, model.a)
    metrics: dict = {f"storage_bits.{k}": v for k, v in bits.items()}
    metrics |= {"accumulator_bits": spec["accumulator"]["bits"], "context": spec["context"]["maxTokens"]}
    return Result(
        outputs=[
            Output("spec", "numeric-spec.json", "numeric.spec", "application/json"),
            Output("weights", "l2-weights.npz", "numeric.weights", "application/x-npz"),
        ],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-numeric/quantize"},
    )


def infer(ctx: InvokeContext) -> Result:
    """L2 generations with the observation points of every step (for comparison with L3/L7)."""
    model = L2Model.load(ctx.input("spec").path, ctx.input("weights").path)
    tokenizer = Tokenizer(Path(ctx.input("tokenizer").path).read_bytes(), model.vocab)
    evalset = load_evalset(ctx.input("evalset").path)
    traces: dict[str, np.ndarray] = {}
    generations = []
    for i, prompt in enumerate(evalset["prompts"]):
        ctx.check_cancelled()
        steps: list = []
        tokens = model.generate(tokenizer.encode(prompt), evalset["steps"], steps)
        generations.append({"prompt": prompt, "tokens": tokens, "text": tokenizer.decode(tokens)})
        traces[f"p{i}.tokens"] = np.array([s["token"] for s in steps], np.int64)
        traces[f"p{i}.logits"] = np.array([s["logits"] for s in steps], np.int64)
        for point in ("attn_residual", "ffn_residual"):
            traces[f"p{i}.{point}"] = np.array([s[point] for s in steps], np.int64)
    out = Path(ctx.out_dir)
    (out / "l2.json").write_text(
        json.dumps({"layer": "L2", "generations": generations, "saturations": model.saturations}, indent=2)
    )
    np.savez_compressed(out / "l2-trace.npz", **traces)
    return Result(
        outputs=[
            Output("outputs", "l2.json", "l2.outputs", "application/json"),
            Output("trace", "l2-trace.npz", "l2.trace", "application/x-npz"),
        ],
        metrics={"prompts": len(generations), "saturations": sum(model.saturations.values())},
        parse={"status": "complete", "parser": "llmchip-numeric/infer"},
    )


def handle_evaluate(ctx: InvokeContext) -> Result:
    cfg, weights = load_ir(ctx.input("ir").path)
    tokenizer = Tokenizer(Path(ctx.input("tokenizer").path).read_bytes(), cfg.vocab_size)
    l2 = L2Model.load(ctx.input("spec").path, ctx.input("weights").path)
    if l2.spec["model"]["irHash"] not in ("", ctx.input("ir").hash):
        raise ValueError("the numeric spec was built from a different Model IR")
    report = evaluate(Model(cfg, weights), l2, tokenizer, load_evalset(ctx.input("evalset").path), ctx.progress)
    Path(ctx.out_dir, "accuracy.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return Result(
        outputs=[Output("report", "accuracy.json", "accuracy.report", "application/json")],
        metrics=report["metrics"],
        parse={"status": "complete", "parser": "llmchip-numeric/evaluate"},
    )


if __name__ == "__main__":
    run_plugin("numeric", "0.1.0", {"quantize": quantize, "infer": infer, "evaluate": handle_evaluate})
