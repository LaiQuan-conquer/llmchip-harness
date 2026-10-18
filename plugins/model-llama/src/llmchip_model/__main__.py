"""IF-2 entry point of the model-llama plugin."""

import json
from pathlib import Path

from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from . import Config, Model, Tokenizer, load_ir, load_llama2c, save_ir, structure_report
from .evalset import load_evalset
from .proxy import DEFAULT_CHARS, char_tokenizer, random_checkpoint


def intake(ctx: InvokeContext) -> Result:
    """S1: llama2.c checkpoint -> Model IR and structure report (FR-MDL-01/03/04/05/07)."""
    cfg, weights = load_llama2c(Path(ctx.input("weights").path).read_bytes())
    tokenizer = Tokenizer(Path(ctx.input("tokenizer").path).read_bytes(), cfg.vocab_size)
    report = structure_report(cfg, weights, ctx.params.get("license"))
    report["tokenizer"] = {"vocabSize": len(tokenizer.vocab), "maxTokenLength": tokenizer.max_token_length}
    out = Path(ctx.out_dir)
    (out / "model.npz").write_bytes(save_ir(cfg, weights))
    (out / "structure.json").write_text(json.dumps(report, indent=2))
    return Result(
        outputs=[
            Output("ir", "model.npz", "model.ir", "application/x-npz"),
            Output("structure", "structure.json", "model.structure", "application/json"),
        ],
        metrics={
            "parameters": report["parameters"]["total"],
            "layers": cfg.n_layers,
            "dim": cfg.dim,
            "vocab": cfg.vocab_size,
        },
        parse={"status": "complete", "parser": "llmchip-model/llama2c-v0"},
    )


def reference(ctx: InvokeContext) -> Result:
    """L0: greedy generations and perplexity of the FP32 reference on an evaluation set (FR-MDL-06)."""
    cfg, weights = load_ir(ctx.input("ir").path)
    tokenizer = Tokenizer(Path(ctx.input("tokenizer").path).read_bytes(), cfg.vocab_size)
    evalset = load_evalset(ctx.input("evalset").path)
    model = Model(cfg, weights)
    generations = []
    for i, prompt in enumerate(evalset["prompts"]):
        ctx.check_cancelled()
        tokens = model.generate(tokenizer.encode(prompt), evalset["steps"])
        generations.append({"prompt": prompt, "tokens": tokens, "text": tokenizer.decode(tokens)})
        ctx.progress((i + 1) / len(evalset["prompts"]))
    nll, count = 0.0, 0
    for passage in evalset["passages"]:
        total, n = model.nll(tokenizer.encode(passage))
        nll, count = nll + total, count + n
    result = {
        "layer": "L0",
        "implementation": "NumPy float32 port of llama2.c run.c (decoding: greedy argmax, lowest id on ties)",
        "generations": generations,
        "perplexity": float(__import__("math").exp(nll / count)) if count else None,
    }
    Path(ctx.out_dir, "l0.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    metrics: dict = {"prompts": len(generations)}
    if result["perplexity"] is not None:
        metrics["perplexity"] = result["perplexity"]
    return Result(
        outputs=[Output("outputs", "l0.json", "l0.outputs", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-model/l0"},
    )


def synthesize(ctx: InvokeContext) -> Result:
    """A proxy model: the structure of a planned model with fixed-seed, untrained weights."""
    p = ctx.params
    chars = p.get("chars", DEFAULT_CHARS)
    cfg = Config(
        dim=p["dim"],
        hidden_dim=p["hiddenDim"],
        n_layers=p["layers"],
        n_heads=p["heads"],
        n_kv_heads=p["kvHeads"],
        vocab_size=len(chars) + 3,
        seq_len=p["seqLen"],
    )
    if cfg.dim % cfg.n_heads or cfg.n_heads % cfg.n_kv_heads or cfg.head_size % 2:
        raise ValueError("dim must split into heads of even size, and heads into kv heads")
    out = Path(ctx.out_dir)
    (out / "proxy.bin").write_bytes(random_checkpoint(cfg, p.get("seed", 1)))
    (out / "tokenizer.bin").write_bytes(char_tokenizer(chars))
    _, weights = load_llama2c((out / "proxy.bin").read_bytes())
    parameters = sum(v.size for k, v in weights.items() if k != "wcls")
    return Result(
        outputs=[
            Output("weights", "proxy.bin", "model.weights.llama2c", "application/octet-stream"),
            Output("tokenizer", "tokenizer.bin", "model.tokenizer.llama2c", "application/octet-stream"),
        ],
        metrics={"parameters": int(parameters), "vocab": cfg.vocab_size, "proxy": True, "trained": False},
        parse={"status": "complete", "parser": "llmchip-model/proxy"},
    )


if __name__ == "__main__":
    run_plugin("model-llama", "0.1.0", {"synthesize": synthesize, "intake": intake, "reference": reference})
