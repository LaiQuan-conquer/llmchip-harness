"""The test suite of a recovered graph against the official implementation.

Every test item says what it checks, how, the threshold, the measured values and
whether it passed, so that the whole suite can be shown item by item:

- weights:  every tensor the graph uses equals the official model's tensor, bit for bit;
- config:   every hyper-parameter of the graph equals the official configuration;
- params:   the graph's parameter count equals the official model's;
- layers:   the residual stream after the embedding and after every decoder layer;
- logits:   the logits at every position of every prompt;
- top1:     the most likely next token at every position;
- greedy:   the tokens generated greedily from every prompt;
- single:   a one-token sequence (boundary case);
- long:     a long sequence (boundary case).

Backends: Hugging Face transformers (float32, eager attention) for Hugging Face
models; the NumPy port of run.c (itself checked against the compiled run.c in
the tests) for llama2.c checkpoints.
"""

from __future__ import annotations

import gc
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .execute import forward, greedy

LOGIT_REL = 1e-3
LAYER_REL = 1e-4
TOP1_MIN = 0.99
LONG_TOKENS = 128


def _rel(ours: np.ndarray, ref: np.ndarray) -> tuple[float, float]:
    diff = float(np.abs(ours - ref).max()) if ours.size else 0.0
    return diff, diff / max(float(np.abs(ref).max()), 1e-30)


class Reference:
    """What the official implementation provides to the suite."""

    backend: str
    version: str

    def config(self) -> dict: ...
    def parameters(self) -> int: ...
    def tensors(self) -> list[tuple[str, Callable[[], np.ndarray]]]: ...
    def logits(self, ids: list[int]) -> np.ndarray: ...
    def hidden(self, ids: list[int]) -> list[tuple[str, np.ndarray]]: ...
    def greedy(self, ids: list[int], steps: int) -> list[int]: ...


class TransformersReference(Reference):
    def __init__(self, model_dir: Path) -> None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import torch
        import transformers

        torch.set_num_threads(min(16, os.cpu_count() or 1))
        self.torch = torch
        self.backend, self.version = "transformers", transformers.__version__
        self.model = transformers.AutoModelForCausalLM.from_pretrained(
            str(model_dir), dtype=torch.float32, attn_implementation="eager"
        )
        self.model.eval()

    def config(self) -> dict:
        c = self.model.config
        rope = getattr(c, "rope_parameters", None) or {}
        return {
            "layers": c.num_hidden_layers,
            "hidden": c.hidden_size,
            "heads": c.num_attention_heads,
            "kvHeads": getattr(c, "num_key_value_heads", c.num_attention_heads),
            "headDim": getattr(c, "head_dim", None) or c.hidden_size // c.num_attention_heads,
            "intermediate": c.intermediate_size,
            "vocab": c.vocab_size,
            "ropeTheta": float(rope.get("rope_theta") or getattr(c, "rope_theta", 0.0)),
            "normEps": float(c.rms_norm_eps),
            "tied": bool(c.tie_word_embeddings),
        }

    def parameters(self) -> int:
        return sum(p.numel() for p in self.model.parameters())

    def tensors(self) -> list[tuple[str, Callable[[], np.ndarray]]]:
        state = self.model.state_dict()
        return [(name, lambda t=t: t.detach().numpy()) for name, t in state.items()]

    def logits(self, ids: list[int]) -> np.ndarray:
        with self.torch.no_grad():
            return self.model(self.torch.tensor([ids])).logits[0].numpy()

    def hidden(self, ids: list[int]) -> list[tuple[str, np.ndarray]]:
        """transformers returns the embedding, layers 0..L-2, and the last layer after the final norm."""
        with self.torch.no_grad():
            hs = self.model(self.torch.tensor([ids]), output_hidden_states=True).hidden_states
        n = len(hs) - 1
        out = [("embedding", hs[0][0].numpy())]
        out += [(f"layer:{i}", hs[i + 1][0].numpy()) for i in range(n - 1)]
        out.append((f"final:{n - 1}", hs[n][0].numpy()))
        return out

    def greedy(self, ids: list[int], steps: int) -> list[int]:
        seq = list(ids)
        with self.torch.no_grad():
            for _ in range(steps):
                seq.append(int(self.model(self.torch.tensor([seq])).logits[0, -1].argmax()))
        return seq[len(ids) :]


# llama2.c tensor names in the graph -> (run.c port name, layer slice or None)
def _llama2c_name(name: str) -> tuple[str, int | None]:
    parts = name.split(".")
    if name == "tok_embeddings.weight":
        return "token_embedding", None
    if name == "norm.weight":
        return "rms_final", None
    if name == "output.weight":
        return "wcls", None
    table = {
        "attention_norm": "rms_att",
        "attention.wq": "wq",
        "attention.wk": "wk",
        "attention.wv": "wv",
        "attention.wo": "wo",
        "ffn_norm": "rms_ffn",
        "feed_forward.w1": "w1",
        "feed_forward.w2": "w2",
        "feed_forward.w3": "w3",
    }
    return table[".".join(parts[2:-1])], int(parts[1])


class RunCReference(Reference):
    def __init__(self, checkpoint: Path, graph_tensors: list[str]) -> None:
        from . import Model, load_llama2c

        self.backend, self.version = "run.c (NumPy port)", "llama2.c"
        self.cfg, self.weights = load_llama2c(checkpoint.read_bytes())
        self.model = Model(self.cfg, self.weights)
        self.names = graph_tensors

    def config(self) -> dict:
        c = self.cfg
        return {
            "layers": c.n_layers,
            "hidden": c.dim,
            "heads": c.n_heads,
            "kvHeads": c.n_kv_heads,
            "headDim": c.head_size,
            "intermediate": c.hidden_dim,
            "vocab": c.vocab_size,
            "ropeTheta": float(c.rope_theta),
            "normEps": float(c.norm_eps),
            "tied": bool(c.shared_classifier),
        }

    def parameters(self) -> int:
        return sum(int(v.size) for k, v in self.weights.items() if not (k == "wcls" and self.cfg.shared_classifier))

    def tensors(self) -> list[tuple[str, Callable[[], np.ndarray]]]:
        out = []
        for name in self.names:
            key, layer = _llama2c_name(name)
            out.append((name, lambda k=key, i=layer: self.weights[k] if i is None else self.weights[k][i]))
        return out

    def _run(self, ids: list[int]) -> tuple[np.ndarray, dict]:
        self.model.reset()
        trace: dict = {}
        logits = np.stack([self.model.forward(t, pos, trace=trace) for pos, t in enumerate(ids)])
        return logits, trace

    def logits(self, ids: list[int]) -> np.ndarray:
        return self._run(ids)[0]

    def hidden(self, ids: list[int]) -> list[tuple[str, np.ndarray]]:
        _, trace = self._run(ids)
        L = self.cfg.n_layers
        per_layer = np.array(trace["ffn_residual"]).reshape(len(ids), L, -1)
        out = [("embedding", self.weights["token_embedding"][ids])]
        out += [(f"layer:{i}", per_layer[:, i, :]) for i in range(L)]
        return out

    def greedy(self, ids: list[int], steps: int) -> list[int]:
        self.model.reset()
        seq = list(ids)
        for pos, t in enumerate(seq[:-1]):
            self.model.forward(t, pos)
        for _ in range(steps):
            seq.append(int(np.argmax(self.model.forward(seq[-1], len(seq) - 1))))
        return seq[len(ids) :]


def run_suite(
    graph: dict,
    census: dict,
    load: Callable[[str], np.ndarray],
    peek: Callable[[str], np.ndarray],
    release: Callable[[], None],
    make_reference: Callable[[], Reference],
    tokenizer,
    prompts: list[str],
    steps: int,
    progress: Callable[[float, str], None] = lambda f, m: None,
) -> dict:
    """Runs every test item; our side first, then the reference (they may not fit in memory together).

    `load` returns a tensor and keeps it cached; `peek` returns it without caching.
    """
    h = graph["hyper"]
    encoded = [tokenizer.encode(p) for p in prompts]
    max_pos = census["hyper"].get("maxPositions", {}).get("value") or LONG_TOKENS
    long_ids = (encoded[0] * (LONG_TOKENS // max(1, len(encoded[0])) + 1))[: min(LONG_TOKENS, max_pos)]
    single_ids = encoded[0][:1]
    ours = []
    for i, ids in enumerate(encoded):
        hidden: list[np.ndarray] = []
        logits = forward(graph, load, ids, hidden if i == 0 else None)
        ours.append({"ids": ids, "logits": logits, "generated": greedy(graph, load, ids, steps)[0], "hidden": hidden})
        progress(0.1 + 0.3 * (i + 1) / len(encoded), prompts[i])
    single = forward(graph, load, single_ids)
    long = forward(graph, load, long_ids)
    release()
    gc.collect()

    ref = make_reference()
    progress(0.5, "reference loaded")
    items: list[dict] = []

    # Weights: what the graph binds against what the official model holds.
    bound = graph["coverage"]["boundTo"]
    tied = h["tied"]
    compared, mismatched, missing = 0, [], []
    for name, value in ref.tensors():
        if name not in bound:
            # A tied output layer exists in the official model under its own name.
            if tied and name in ("lm_head.weight", "output.weight"):
                name_ours = graph["nodes"]["epilogue"][1]["weight"]
            else:
                missing.append(name)
                continue
        else:
            name_ours = name
        a, b = peek(name_ours), value()
        compared += 1
        if a.shape != b.shape or not np.array_equal(a, b):
            mismatched.append({"tensor": name, "maxAbs": float(np.abs(a - b).max()) if a.shape == b.shape else None})
    items.append(
        {
            "id": "weights",
            "kind": "weights",
            "passed": not mismatched and not missing,
            "values": {"compared": compared, "mismatched": len(mismatched), "missing": len(missing)},
            "details": mismatched[:20] + [{"tensor": m, "missing": True} for m in missing[:20]],
        }
    )

    # Configuration and parameter count.
    rc = ref.config()
    rows = []
    for key in (
        "layers",
        "hidden",
        "heads",
        "kvHeads",
        "headDim",
        "intermediate",
        "vocab",
        "ropeTheta",
        "normEps",
        "tied",
    ):
        mine = h[key]
        same = (
            abs(float(mine) - float(rc[key])) <= 1e-12 * max(1.0, abs(float(rc[key])))
            if not isinstance(mine, bool)
            else mine == rc[key]
        )
        rows.append({"key": key, "graph": mine, "reference": rc[key], "same": bool(same)})
    items.append(
        {
            "id": "config",
            "kind": "config",
            "passed": all(r["same"] for r in rows),
            "values": {"compared": len(rows)},
            "details": rows,
        }
    )
    mine_params = census["totals"]["parameters"]
    ref_params = ref.parameters()
    items.append(
        {
            "id": "params",
            "kind": "params",
            "passed": mine_params == ref_params,
            "values": {"graph": mine_params, "reference": ref_params},
            "details": [],
        }
    )

    # Layer by layer, on the first prompt.
    ref_hidden = ref.hidden(encoded[0])
    ours_hidden = ours[0]["hidden"]
    layer_rows = []
    for name, value in ref_hidden:
        kind, _, index = name.partition(":")
        mine = (
            ours_hidden[0]
            if kind == "embedding"
            else ours_hidden[int(index) + 1]
            if kind == "layer"
            else ours_hidden[-1]
        )
        absd, rel = _rel(mine, value)
        layer_rows.append({"point": name, "maxAbs": absd, "maxRel": rel, "passed": rel <= LAYER_REL})
    items.append(
        {
            "id": "layers",
            "kind": "layers",
            "passed": all(r["passed"] for r in layer_rows),
            "threshold": LAYER_REL,
            "values": {"points": len(layer_rows), "worstRel": max(r["maxRel"] for r in layer_rows)},
            "details": layer_rows,
            "prompt": prompts[0],
        }
    )
    progress(0.6, "layers")

    # Every prompt: logits, top-1 and greedy generation.
    agree = positions = 0
    for i, (prompt, o) in enumerate(zip(prompts, ours, strict=True)):
        ref_logits = ref.logits(o["ids"])
        absd, rel = _rel(o["logits"], ref_logits)
        same_top = int((o["logits"].argmax(-1) == ref_logits.argmax(-1)).sum())
        agree, positions = agree + same_top, positions + len(o["ids"])
        items.append(
            {
                "id": f"logits:{i}",
                "kind": "logits",
                "passed": rel <= LOGIT_REL,
                "threshold": LOGIT_REL,
                "prompt": prompt,
                "values": {"tokens": len(o["ids"]), "maxAbs": absd, "maxRel": rel, "top1": same_top / len(o["ids"])},
                "details": [],
            }
        )
        ref_generated = ref.greedy(o["ids"], steps)
        items.append(
            {
                "id": f"greedy:{i}",
                "kind": "greedy",
                "passed": ref_generated == o["generated"],
                "prompt": prompt,
                "values": {
                    "steps": steps,
                    "ours": tokenizer.continuation(o["ids"], o["generated"]),
                    "reference": tokenizer.continuation(o["ids"], ref_generated),
                    "firstDifference": next(
                        (k for k, (a, b) in enumerate(zip(o["generated"], ref_generated, strict=True)) if a != b), None
                    ),
                },
                "details": [],
            }
        )
        progress(0.6 + 0.3 * (i + 1) / len(prompts), prompt)
    items.append(
        {
            "id": "top1",
            "kind": "top1",
            "passed": agree / positions >= TOP1_MIN,
            "threshold": TOP1_MIN,
            "values": {"positions": positions, "agreement": agree / positions},
            "details": [],
        }
    )

    # Boundary cases.
    for kind, ids, mine in (("single", single_ids, single), ("long", long_ids, long)):
        absd, rel = _rel(mine, ref.logits(ids))
        items.append(
            {
                "id": kind,
                "kind": kind,
                "passed": rel <= LOGIT_REL,
                "threshold": LOGIT_REL,
                "values": {"tokens": len(ids), "maxAbs": absd, "maxRel": rel},
                "details": [],
            }
        )

    logit_items = [x for x in items if x["kind"] == "logits"]
    return {
        "backend": ref.backend,
        "backendVersion": ref.version,
        "steps": steps,
        "precision": "float32 on both sides; weights converted from the files' dtype the same way",
        "items": items,
        "summary": {
            "total": len(items),
            "passed": sum(x["passed"] for x in items),
            "failed": sum(not x["passed"] for x in items),
            "maxAbsLogitDiff": max(x["values"]["maxAbs"] for x in logit_items),
            "maxRelLogitDiff": max(x["values"]["maxRel"] for x in logit_items),
            "top1Agreement": agree / positions,
            "greedyMatch": all(x["passed"] for x in items if x["kind"] == "greedy"),
        },
    }
