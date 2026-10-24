"""IF-2 entry point of the numeric plugin: quantize (S2), L2 inference, evaluation (S2 accuracy), the
analysis of any recovered graph (quantize_graph, evaluate_graph), and model quality by open
benchmarks (benchmark_data, benchmark, benchmark_compare)."""

import json
import os
import signal
from pathlib import Path

import numpy as np
from llmchip_model import Model, Tokenizer, load_ir
from llmchip_model.evalset import load_evalset
from llmchip_model.pipeline import LoadedModel, open_model
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from . import graph_eval, graph_l2
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
        rounding=p.get("rounding", "half-up"),
        overflow=p.get("overflow", "saturate"),
        accumulator_bits=p.get("accumulatorBits"),
    )
    out = Path(ctx.out_dir)
    (out / "numeric-spec.json").write_text(json.dumps(spec, indent=2))
    (out / "l2-weights.npz").write_bytes(npz)
    model = L2Model.load(out / "numeric-spec.json", out / "l2-weights.npz")
    bits = storage_bits(spec, model.a)
    metrics: dict = {f"storage_bits.{k}": v for k, v in bits.items()}
    metrics |= {"accumulator_bits": spec["accumulator"]["bits"], "context": spec["context"]["maxTokens"]}
    metrics |= {
        "weight_bits": spec["weights"]["storageBits"],
        "group_size": spec["weights"]["groupSize"],
        "rounding": spec["rounding"]["mode"],
        "overflow": spec["overflow"]["mode"],
        "accumulator_can_overflow": spec["accumulator"]["canOverflow"],
    }
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
        ids = tokenizer.encode(prompt)
        tokens = model.generate(ids, evalset["steps"], steps)
        traces[f"p{i}.prompt"] = np.array(ids, np.int64)
        generations.append({"prompt": prompt, "promptTokens": ids, "tokens": tokens, "text": tokenizer.decode(tokens)})
        traces[f"p{i}.tokens"] = np.array([s["token"] for s in steps], np.int64)
        traces[f"p{i}.logits"] = np.array([s["logits"] for s in steps], np.int64)
        for point in ("attn_residual", "ffn_residual"):
            traces[f"p{i}.{point}"] = np.array([s[point] for s in steps], np.int64)
    # Random token sequences, every position forced (constrained-random verification, FR-VER-04).
    rnd = evalset.get("random") or {"seed": 20261018, "sequences": 4, "length": 16}
    rng = np.random.default_rng(rnd["seed"])
    length = min(rnd["length"], model.context)
    for i in range(rnd["sequences"]):
        ctx.check_cancelled()
        ids = [1] + [int(t) for t in rng.integers(0, model.vocab, length - 1)]
        steps = []
        model.generate(ids, length, steps)
        traces[f"r{i}.prompt"] = np.array(ids, np.int64)
        traces[f"r{i}.tokens"] = np.array([s["token"] for s in steps], np.int64)
        traces[f"r{i}.logits"] = np.array([s["logits"] for s in steps], np.int64)
        for point in ("attn_residual", "ffn_residual"):
            traces[f"r{i}.{point}"] = np.array([s[point] for s in steps], np.int64)
    # Identifies what the trace was computed from, so comparisons can refuse a stale trace.
    traces["meta.spec"] = np.array(ctx.input("spec").hash)
    traces["meta.weights"] = np.array(ctx.input("weights").hash)
    traces["meta.steps"] = np.array(evalset["steps"], np.int64)
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


def _evalset(ctx: InvokeContext, kind: str) -> dict:
    given = next((i for i in ctx.inputs if i.name == "evalset"), None)
    return load_evalset(given.path) if given else graph_eval.default_evalset(kind)


def quantize_graph(ctx: InvokeContext) -> Result:
    """Quantizes a recovered graph (any model): graph numeric spec and integer weights."""
    m = LoadedModel(ctx)
    p = ctx.params
    context = p.get("contextLength", 256)
    evalset = _evalset(ctx, m.kind)
    ctx.progress(0.05, "calibration on the float graph (L0)")
    stats = graph_l2.calibrate(m.graph, m.weights.get, graph_eval.calibration_sequences(evalset, m.tokenizer, context))
    m.weights.release()
    spec, arrays = graph_l2.build(
        m.graph,
        m.weights.peek,
        weight_format=p.get("weightFormat", "int8"),
        group_size=p.get("groupSize", 32),
        context=context,
        rounding=p.get("rounding", "half-up"),
        overflow=p.get("overflow", "saturate"),
        accumulator_bits=p.get("accumulatorBits"),
        per_layer=p.get("perLayer", True),
        residual_bits=p.get("residualBits", 24),
        graph_hash=ctx.input("graph").hash,
        stats=stats,
        progress=lambda message: (ctx.check_cancelled(), ctx.log("info", message)),
    )
    out = Path(ctx.out_dir)
    (out / "numeric-spec.json").write_text(json.dumps(spec, indent=2))
    np.savez(out / "l2-weights.npz", meta=np.array(json.dumps({"format": graph_l2.GRAPH_WEIGHTS_FORMAT})), **arrays)
    bits = graph_l2.storage_bits(spec, arrays)
    metrics: dict = {f"storage_bits.{k}": v for k, v in bits.items()}
    metrics |= {
        "accumulator_bits": spec["accumulator"]["bits"],
        "accumulator_can_overflow": spec["accumulator"]["canOverflow"],
        "context": context,
        "weight_bits": spec["weights"]["storageBits"],
        "group_size": spec["weights"]["groupSize"],
        "rounding": spec["rounding"]["mode"],
        "overflow": spec["overflow"]["mode"],
        "family": m.graph["family"],
        "per_layer": spec["activations"]["granularity"] == "layer",
        "residual_bits": spec["activations"]["bits"]["x"],
    }
    return Result(
        outputs=[
            Output("spec", "numeric-spec.json", "numeric.spec", "application/json"),
            Output("weights", "l2-weights.npz", "numeric.weights", "application/x-npz"),
        ],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-numeric/quantize-graph"},
    )


def evaluate_graph(ctx: InvokeContext) -> Result:
    """Accuracy of a recovered graph's L2 against L0, on prompts and passages."""
    m = LoadedModel(ctx)
    spec = json.loads(Path(ctx.input("spec").path).read_text())
    if spec.get("model", {}).get("graphHash") not in ("", ctx.input("graph").hash):
        raise ValueError("the numeric spec was built from a different graph")
    with np.load(ctx.input("weights").path, allow_pickle=False) as z:
        if json.loads(str(z["meta"])).get("format") != graph_l2.GRAPH_WEIGHTS_FORMAT:
            raise ValueError("not the integer weights of a graph numeric spec")
        arrays = {k: z[k] for k in z.files if k != "meta"}
    l2 = graph_l2.GraphL2(m.graph, spec, arrays)
    p = ctx.params
    steps = p.get("steps", 8)
    passage_tokens = min(p.get("passageTokens", 128), l2.context - steps)
    prompts, passages = graph_eval.select_texts(
        _evalset(ctx, m.kind), m.tokenizer, p.get("maxPrompts", 4), passage_tokens
    )
    report = graph_eval.evaluate_graph(
        m.graph, m.weights.get, m.weights.release, l2, m.tokenizer, prompts, passages, steps, ctx.progress
    )
    report["model"] = {"family": m.graph["family"], "name": m.bundle.get("name")}
    Path(ctx.out_dir, "accuracy.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return Result(
        outputs=[Output("report", "accuracy.json", "accuracy.report", "application/json")],
        metrics=report["metrics"],
        parse={"status": "complete", "parser": "llmchip-numeric/evaluate-graph"},
    )


def benchmark_data(ctx: InvokeContext) -> Result:
    """Packs the open benchmarks downloaded on this machine into one artifact, with the quick set."""
    from .benchdata import pack

    source = Path(ctx.params["path"]).expanduser()
    if not source.is_dir():
        raise ValueError(f"{source} is not a directory on this machine")
    out = Path(ctx.out_dir)
    index = pack(source, out / "benchmark-data.tar", lambda message: ctx.log("info", message))
    sets = {k: v for k, v in index.items() if k != "tasks"} | {
        "tasks": {t: {k: v for k, v in info.items() if k != "files"} for t, info in index["tasks"].items()}
    }
    (out / "benchmark-sets.json").write_text(json.dumps(sets, indent=1, ensure_ascii=False))
    metrics: dict = {"benchmarks": len(index["benchmarks"]), "tasks": len(index["tasks"])}
    for bench, info in index["benchmarks"].items():
        metrics |= {f"questions.{bench}": info["questions"], f"quick.{bench}": info["quick"]}
    metrics["quick_total"] = sum(info["quick"] for info in index["benchmarks"].values())
    return Result(
        outputs=[
            Output("data", "benchmark-data.tar", "benchmark.data", "application/x-tar", "internal"),
            Output("sets", "benchmark-sets.json", "benchmark.sets", "application/json", "internal"),
        ],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-numeric/benchmark-data"},
    )


def _model_dir(ctx: InvokeContext) -> Path:
    """The imported model's files under their own names (what transformers and lm-eval load)."""
    return open_model(ctx)[0]


def benchmark(ctx: InvokeContext) -> Result:
    """Answers the quick set or the full set of the benchmarks with L0 or L2 (FR-QNT-04/08)."""
    import torch

    from . import bench
    from .benchdata import BENCHMARKS, Data
    from .benchlm import L2LM, l0_model

    p = ctx.params
    layer, chosen = p["layer"], p.get("set", "quick")
    benchmarks = p.get("benchmarks") or BENCHMARKS
    batch = p.get("batchSize", 32)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    data = Data(Path(ctx.input("data").path), Path(ctx.work_dir))
    model_dir = _model_dir(ctx)
    source = next(i for i in ctx.inputs if i.name in ("model", "bundle"))
    identity: dict = {source.name: source.hash, "precision": "bfloat16" if layer == "L0" else "numeric spec"}
    if layer == "L2":
        from .l2_torch import TorchL2

        graph = json.loads(Path(ctx.input("graph").path).read_text())
        spec = json.loads(Path(ctx.input("spec").path).read_text())
        if spec.get("model", {}).get("graphHash") not in ("", ctx.input("graph").hash):
            raise ValueError("the numeric spec was built from a different graph")
        with np.load(ctx.input("weights").path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files if k != "meta"}
        l2 = TorchL2(graph_l2.GraphL2(graph, spec, arrays), device)
        max_length = l2.context
        if p.get("contextLength") not in (None, max_length):
            raise ValueError(f"L2 runs at the context of its numeric spec ({max_length})")
        lm = L2LM(str(model_dir), l2, max_length, batch, lambda message: ctx.log("info", message))
        identity |= {"spec": ctx.input("spec").hash, "weights": ctx.input("weights").hash, "device": device}
    elif layer == "L0":
        max_length = p.get("contextLength", 2048)
        lm = l0_model(str(model_dir), max_length, batch, device)
        identity |= {"device": device}
    else:
        raise ValueError(f"unknown layer {layer}")
    head = {
        "format": bench.REPORT_FORMAT,
        "layer": layer,
        "set": chosen,
        "maxLength": max_length,
        "data": ctx.input("data").hash,
        "model": identity,
        "benchmarkList": benchmarks,
    }
    done = None
    if ctx.resume is not None:
        saved = json.loads(Path(ctx.resume.path).read_text())
        if {k: saved.get(k) for k in head} == head:
            done = saved["benchmarks"]
            ctx.log("info", f"continuing after {sorted(done)}")

    def after_each(report: dict) -> None:
        tmp = Path(ctx.work_dir, "checkpoint.tmp")
        tmp.write_text(json.dumps(head | report, ensure_ascii=False))
        tmp.replace(Path(ctx.work_dir, "checkpoint.json"))
        ctx.checkpoint("checkpoint.json", "benchmarks")
        ctx.progress(len(report["benchmarks"]) / len(benchmarks), f"{len(report['benchmarks'])} benchmarks done")

    report = bench.run(
        lm, data, benchmarks, chosen == "quick", Path(ctx.work_dir), done, after_each, lambda m: ctx.log("info", m)
    )
    import evalplus
    import lm_eval
    import transformers

    report = head | report
    report["versions"] = {
        "lm_eval": lm_eval.__version__,
        "evalplus": evalplus.__version__,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
    }
    if layer == "L2":
        report["saturations"] = l2.saturations
    Path(ctx.out_dir, "benchmark.json").write_text(json.dumps(report, ensure_ascii=False))
    metrics: dict = {"layer": layer, "set": chosen, "max_length": max_length, "benchmarks": len(report["benchmarks"])}
    for name, r in report["benchmarks"].items():
        metrics |= {f"n.{name}": r["questions"], f"seconds.{name}": r["seconds"]}
        if r["score"] is not None:
            metrics[f"score.{name}"] = 100 * r["score"]
    return Result(
        outputs=[Output("report", "benchmark.json", "benchmark.report", "application/json", "internal")],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-numeric/benchmark"},
    )


def benchmark_compare(ctx: InvokeContext) -> Result:
    """L2 against L0 on the same questions: drop and paired-bootstrap interval per benchmark."""
    from . import bench

    reference = json.loads((Path(__file__).parents[2] / "data" / "benchmark-reference.json").read_text())
    comparison, metrics = bench.compare(bench.load(ctx.input("l0").path), bench.load(ctx.input("l2").path), reference)
    Path(ctx.out_dir, "comparison.json").write_text(json.dumps(comparison, indent=1, ensure_ascii=False))
    return Result(
        outputs=[Output("comparison", "comparison.json", "benchmark.comparison", "application/json", "internal")],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-numeric/benchmark-compare"},
    )


def l2_fast_check(ctx: InvokeContext) -> Result:
    """Evidence for `spec.l2.fast`: the fast L2 against the reference L2 on the quick-set questions."""
    from transformers import AutoTokenizer

    from .benchdata import Data
    from .l2_torch import TorchL2, check_against_reference

    graph = json.loads(Path(ctx.input("graph").path).read_text())
    spec = json.loads(Path(ctx.input("spec").path).read_text())
    if spec.get("model", {}).get("graphHash") not in ("", ctx.input("graph").hash):
        raise ValueError("the numeric spec was built from a different graph")
    with np.load(ctx.input("weights").path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files if k != "meta"}
    ref = graph_l2.GraphL2(graph, spec, arrays)
    fast = TorchL2(ref)
    tokenizer = AutoTokenizer.from_pretrained(str(_model_dir(ctx)))
    questions = Data(Path(ctx.input("data").path), Path(ctx.work_dir)).quick_questions()
    prompts = [
        tokenizer.apply_chat_template([{"role": "user", "content": q}], add_generation_prompt=True, tokenize=False)
        for q in questions
    ]
    ids = [tokenizer(p, add_special_tokens=False)["input_ids"] for p in prompts]
    steps = ctx.params.get("steps", 64)
    report = check_against_reference(ref, fast, ids, steps, lambda m: ctx.log("info", m))
    Path(ctx.out_dir, "l2-fast.json").write_text(json.dumps(report, indent=1))
    return Result(
        outputs=[Output("report", "l2-fast.json", "l2.fast-check", "application/json")],
        metrics=report["metrics"],
        parse={"status": "complete", "parser": "llmchip-numeric/l2-fast-check"},
    )


def _stop(signum, _frame) -> None:
    """The kernel cancels a run with SIGTERM: remove the code sandboxes first, or they would
    outlive the run (they are containers of their own)."""
    from .benchcode import remove_sandboxes

    remove_sandboxes(grace=1.0)
    os._exit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop)
    run_plugin(
        "numeric",
        "0.3.0",
        {
            "quantize": quantize,
            "infer": infer,
            "evaluate": handle_evaluate,
            "quantize_graph": quantize_graph,
            "evaluate_graph": evaluate_graph,
            "benchmark_data": benchmark_data,
            "benchmark": benchmark,
            "benchmark_compare": benchmark_compare,
            "l2_fast_check": l2_fast_check,
        },
    )
