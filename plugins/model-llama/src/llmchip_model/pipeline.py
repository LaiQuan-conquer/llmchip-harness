"""Handlers of the weights-to-graph pipeline: import (S0), recover, run_graph, compare (S1).

- `import_model` reads a downloaded model (file or directory) from this machine, keeps
  its files as artifacts and takes the tensor census from their headers.
- `recover_graph` turns the census into the computation graph (`graph.recover`).
- `run_graph` executes the recovered graph (L0) on prompts and reports readable text.
- `compare_upstream` executes it again next to the official implementation and
  compares logits and greedy continuations.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
from llmchip_sdk import InvokeContext, Output, Result

from .census import census
from .execute import greedy
from .formats import FileLayout, WeightLoader, is_llama2c_checkpoint, read_layout
from .graph import recover
from .tokens import TextTokenizer
from .upstream import Reference, RunCReference, TransformersReference, run_suite

TOKENIZER_AUX = {
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.json",
    "merges.txt",
    "tokenizer.model",
    "added_tokens.json",
}
DEFAULT_PROMPTS = {
    "llama2c": ["Once upon a time", "The little dog", "Tom and Lily went to the park"],
    "hf": ["The capital of France is", "Once upon a time,", "def fibonacci(n):", "1, 2, 3, 4,"],
}


def _role(path: Path) -> tuple[str, str | None]:
    """(role, weight format) of a file in a model directory."""
    name = path.name
    if name == "config.json":
        return "config", None
    if name == "generation_config.json":
        return "generation_config", None
    if name == "tokenizer.json":
        return "tokenizer", None
    if name in TOKENIZER_AUX:
        return "tokenizer_aux", None
    if name.endswith(".safetensors.index.json"):
        return "weights_index", "safetensors"
    if path.suffix == ".safetensors":
        return "weights", "safetensors"
    if path.suffix == ".gguf":
        return "weights", "gguf"
    if path.suffix == ".bin":
        if is_llama2c_checkpoint(path):
            return "weights", "llama2c"
        if name.startswith("tok"):
            return "tokenizer", None
        return "ignored", None
    if name.upper().startswith("LICENSE"):
        return "license", None
    return "ignored", None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def _place(src: Path, dst: Path) -> None:
    """Hard-link when possible (no copy of multi-GB files), else copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _from_artifacts(ctx: InvokeContext) -> Path:
    """A project's own llama2.c weights and tokenizer (e.g. the proxy model), laid out as a model directory."""
    directory = Path(ctx.work_dir, "model")
    directory.mkdir(exist_ok=True)
    for name, file in (("weights", "model.bin"), ("tokenizer", "tokenizer.bin")):
        _place(Path(ctx.input(name).path), directory / file)
    return directory


def import_model(ctx: InvokeContext) -> Result:
    """Imports a model from a path on this machine, or from a project's llama2.c weight artifacts."""
    from_artifacts = "path" not in ctx.params
    if from_artifacts and not any(i.name == "weights" for i in ctx.inputs):
        raise ValueError("give either `path` or the `weights` and `tokenizer` artifacts")
    source = _from_artifacts(ctx) if from_artifacts else Path(ctx.params["path"]).expanduser()
    if not source.exists():
        raise ValueError(f"{source} does not exist on this machine")
    candidates = sorted(p for p in ([source] if source.is_file() else source.iterdir()) if p.is_file())
    files, ignored = [], []
    for p in candidates:
        role, fmt = _role(p)
        (ignored if role == "ignored" else files).append({"path": p, "role": role, "format": fmt})
    formats = {f["format"] for f in files if f["role"] == "weights"}
    if not formats:
        raise ValueError(f"no weight file (.safetensors, .gguf or llama2.c .bin) in {source}")
    # One model, one format: prefer safetensors, then llama2.c, then GGUF.
    chosen = next(f for f in ("safetensors", "llama2c", "gguf") if f in formats)
    if chosen == "gguf" and sum(f["format"] == "gguf" for f in files) > 1:
        raise ValueError("several GGUF files (different quantizations?); import one file")
    for f in list(files):
        if f["format"] and f["format"] != chosen:
            files.remove(f)
            ignored.append({**f, "reason": "OTHER_FORMAT", "chosen": chosen})
    out = Path(ctx.out_dir)
    label = ctx.params.get("label", "internal")
    outputs, listed = [], []
    for i, f in enumerate(files):
        p: Path = f["path"]
        _place(p, out / p.name)
        listed.append(
            {
                "name": p.name,
                "role": f["role"],
                "format": f["format"],
                "bytes": p.stat().st_size,
                "sha256": _sha256(out / p.name),
            }
        )
        media = (
            "application/json"
            if p.suffix == ".json"
            else "text/plain"
            if p.suffix == ".txt"
            else "application/octet-stream"
        )
        outputs.append(Output("file", p.name, "model.file", media, label))
        ctx.progress((i + 1) / (len(files) + 1), p.name)
    weights = [out / f["name"] for f in listed if f["role"] == "weights"]
    layouts = [read_layout(p) for p in weights]
    config_file = next((f for f in listed if f["role"] == "config"), None)
    config = json.loads((out / config_file["name"]).read_text()) if config_file else None
    result = census(layouts, config, int(ctx.params.get("context", 64)), paths={p.name: p for p in weights})
    bundle = {
        "format": "llmchip-model-bundle/1",
        "source": f"artifact {ctx.input('weights').hash}" if from_artifacts else str(source),
        "name": ctx.params.get("name") or (source.name if source.is_dir() else source.stem),
        "weightFormat": chosen,
        "files": listed,
        "ignored": [{"name": f["path"].name, "reason": f.get("reason", "NOT_A_MODEL_FILE")} for f in ignored],
    }
    (out / "bundle.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
    (out / "census.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    outputs += [
        Output("bundle", "bundle.json", "model.bundle", "application/json", label),
        Output("census", "census.json", "model.census", "application/json", label),
    ]
    checks = {c["id"]: c["ok"] for c in result["checks"]}
    return Result(
        outputs=outputs,
        metrics={
            "files": len(listed),
            "tensors": result["totals"]["tensors"],
            "parameters": result["totals"]["parameters"],
            "stored_parameters": result["totals"]["storedParameters"],
            "macs_per_token": result["macsPerToken"]["total"],
            "all_tensors_classified": checks["classified"],
            "header_consistent": checks["bytes"],
            "layers_uniform": checks["layers"],
            "config_agrees": checks["config"],
            "output_consistent": checks["output"],
            "config_present": config is not None,
            "weight_format": chosen,
            "model_type": str(result["hyper"].get("modelType", {}).get("value", "unknown")),
        },
        parse={"status": "complete", "parser": "llmchip-model/census-1"},
    )


def recover_graph(ctx: InvokeContext) -> Result:
    c = json.loads(Path(ctx.input("census").path).read_text())
    g = recover(c)
    Path(ctx.out_dir, "graph.json").write_text(json.dumps(g, indent=2, ensure_ascii=False))
    if g["supported"]:
        cov = g["coverage"]
        metrics = {
            "family_supported": True,
            "family": g["family"],
            "tensors_bound": cov["bound"],
            "tensors_ignored": len(cov["ignored"]),
            "tensors_unmatched": len(cov["unmatched"]),
            "tensors_missing": len(cov["missing"]),
            "layers": g["nodes"]["repeat"],
            "nodes": len(g["nodes"]["prologue"])
            + len(g["nodes"]["block"]) * g["nodes"]["repeat"]
            + len(g["nodes"]["epilogue"]),
        }
    else:
        # Nothing was bound: every tensor counts as unmatched.
        metrics = {
            "family_supported": False,
            "family": "unsupported",
            "tensors_bound": 0,
            "tensors_ignored": 0,
            "tensors_unmatched": c["totals"]["tensors"],
            "tensors_missing": 0,
        }
    return Result(
        outputs=[Output("graph", "graph.json", "model.graph", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-model/graph-1"},
    )


class LoadedModel:
    """A recovered graph with its weights and tokenizer, from the run's inputs
    (`graph`, `bundle` and `files`). Also used by the numeric plugin (S2)."""

    def __init__(self, ctx: InvokeContext) -> None:
        self.graph = json.loads(Path(ctx.input("graph").path).read_text())
        if not self.graph.get("supported"):
            raise ValueError(f"the graph was not recovered: {self.graph.get('reason')}")
        self.bundle = json.loads(Path(ctx.input("bundle").path).read_text())
        by_hash = {i.hash: Path(i.path) for i in ctx.inputs if i.name == "files"}
        self.paths: dict[str, Path] = {}
        for f in self.bundle["files"]:
            if f["sha256"] not in by_hash:
                raise ValueError(f"file {f['name']} of the bundle is not among the run's inputs")
            self.paths[f["name"]] = by_hash[f["sha256"]]
        self.weight_files = [f["name"] for f in self.bundle["files"] if f["role"] == "weights"]
        self.layouts: list[FileLayout] = [self._layout(name) for name in self.weight_files]
        self.weights = WeightLoader(self.layouts, {name: self.paths[name] for name in self.weight_files})
        tok = next((f for f in self.bundle["files"] if f["role"] == "tokenizer"), None)
        if tok is None:
            raise ValueError("the bundle has no tokenizer")
        self.tokenizer = TextTokenizer(
            self.paths[tok["name"]], "hf" if tok["name"].endswith(".json") else "llama2c", self.graph["hyper"]["vocab"]
        )
        self.kind = "llama2c" if self.graph["family"] == "llama2c" else "hf"

    def _layout(self, name: str) -> FileLayout:
        # Inputs live under run-specific names; the layout must carry the bundle's file name.
        layout = read_layout(_named(self.paths[name], name))
        layout.name = name
        for t in layout.tensors:
            t.file = name
        return layout

    def prompts(self, params: dict) -> list[str]:
        """The given prompts, or defaults this tokenizer can encode (a proxy model's alphabet is tiny)."""
        given = params.get("prompts")
        if given:
            bad = [p for p in given if not self.tokenizer.encodable(p)]
            if bad:
                raise ValueError(f"this model's tokenizer cannot encode: {bad}")
            return list(given)
        chosen = []
        for p in DEFAULT_PROMPTS[self.kind]:
            for variant in (p, p.lower()):
                if self.tokenizer.encodable(variant):
                    chosen.append(variant)
                    break
        if not chosen:
            raise ValueError("none of the default prompts can be encoded; give prompts")
        return chosen

    def run(self, prompt: str, steps: int) -> dict:
        ids = self.tokenizer.encode(prompt)
        t0 = time.monotonic()
        generated, logits = greedy(self.graph, self.weights.get, ids, steps)
        probs = np.exp(logits.astype(np.float64) - logits.max())
        probs /= probs.sum()
        top = np.argsort(-probs, kind="stable")[:5]
        return {
            "prompt": prompt,
            "promptTokens": [{"id": int(t), "text": self.tokenizer.piece(int(t))} for t in ids],
            "generated": [{"id": int(t), "text": self.tokenizer.piece(int(t))} for t in generated],
            "completion": self.tokenizer.continuation(ids, generated),
            "nextToken": [
                {"id": int(t), "text": self.tokenizer.piece(int(t)), "probability": float(probs[t])} for t in top
            ],
            "seconds": time.monotonic() - t0,
        }


def _named(path: Path, name: str) -> Path:
    """`read_layout` dispatches on the suffix, which run inputs do not carry."""
    if path.name.endswith(Path(name).suffix):
        return path
    link = path.parent / f"{path.name}{Path(name).suffix}"
    if not link.exists():
        link.symlink_to(path)
    return link


def run_graph(ctx: InvokeContext) -> Result:
    m = LoadedModel(ctx)
    steps = int(ctx.params.get("steps", 8))
    results = []
    prompts = m.prompts(ctx.params)
    for i, prompt in enumerate(prompts):
        ctx.check_cancelled()
        results.append(m.run(prompt, steps))
        ctx.progress((i + 1) / len(prompts), prompt)
    doc = {
        "layer": "L0",
        "implementation": "NumPy float32 interpreter of the recovered graph (full-sequence forward, greedy argmax)",
        "family": m.graph["family"],
        "steps": steps,
        "prompts": results,
    }
    Path(ctx.out_dir, "l0.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return Result(
        outputs=[Output("outputs", "l0.json", "l0g.outputs", "application/json")],
        metrics={
            "prompts": len(results),
            "tokens_generated": steps * len(results),
            "seconds": sum(r["seconds"] for r in results),
        },
        parse={"status": "complete", "parser": "llmchip-model/l0g"},
    )


def compare_upstream(ctx: InvokeContext) -> Result:
    """The full test suite of the recovered graph against the official implementation."""
    m = LoadedModel(ctx)
    steps = int(ctx.params.get("steps", 8))
    prompts = m.prompts(ctx.params)
    census = json.loads(Path(ctx.input("census").path).read_text())

    def reference() -> Reference:
        if m.kind == "llama2c":
            return RunCReference(m.paths[m.weight_files[0]], list(m.graph["coverage"]["boundTo"]))
        model_dir = Path(ctx.work_dir, "hf-model")
        model_dir.mkdir(exist_ok=True)
        for f in m.bundle["files"]:
            if f["role"] in ("config", "weights", "weights_index"):
                link = model_dir / f["name"]
                if not link.exists():
                    link.symlink_to(m.paths[f["name"]])
        return TransformersReference(model_dir)

    report = run_suite(
        m.graph,
        census,
        m.weights.get,
        m.weights.peek,
        m.weights.release,
        reference,
        m.tokenizer,
        prompts,
        steps,
        lambda fraction, message: ctx.progress(fraction, message),
    )
    report["prompts"] = prompts
    Path(ctx.out_dir, "upstream.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    summary = report["summary"]
    return Result(
        outputs=[Output("report", "upstream.json", "upstream.report", "application/json")],
        metrics={
            "backend": report["backend"],
            "prompts": len(prompts),
            "tests_total": summary["total"],
            "tests_passed": summary["passed"],
            "tests_failed": summary["failed"],
            "max_abs_logit_diff": summary["maxAbsLogitDiff"],
            "max_rel_logit_diff": summary["maxRelLogitDiff"],
            "top1_agreement": summary["top1Agreement"],
            "greedy_match": summary["greedyMatch"],
        },
        parse={"status": "complete", "parser": "llmchip-model/upstream-2"},
    )
