"""Calibrated weight quantization (GPTQ) of a recovered graph, on PyTorch (requirements FR-QNT-01).

GPTQ (Frantar et al., 2023) quantizes a weight matrix one input column at a time and moves
each column's rounding error onto the columns not yet quantized, weighted by the inverse of
the Hessian H = X^T X of the matrix's inputs X on calibration text, so that the product XW
changes as little as possible. Here it produces exactly the numeric spec's weight format:
integer levels q in [-qmax, qmax], one 15-bit mantissa per (row, group of input columns) and
one exponent per matrix, w ~ q * m * 2^-e. A group's scale is taken from its columns as they
are when GPTQ reaches the group (absmax / qmax), as in GPTQ with groups.

`calibrate` runs the float graph layer by layer over the calibration sequences: the inputs of
each layer's matrix products give their Hessians; the layer's weights are quantized; the layer
runs again with the quantized weights, and its outputs feed the next layer. The activation
ranges recorded on that second pass (the model as it will be quantized) set the fraction bits.
The tied output layer is quantized last, from the Hessian of the final norm's outputs; the
embedding uses the same integers (a shared table).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

import numpy as np
import torch

from .graph_l2 import KIND_OF
from .spec import WEIGHT_FORMATS
from .torch_float import FloatGraph

HEADROOM = 2.0  # the matrix exponent leaves room for group scales to grow during GPTQ


def gptq(
    w: torch.Tensor, h: torch.Tensor, fmt: str, group: int, damp: float = 0.01, block: int = 128
) -> tuple[np.ndarray, np.ndarray, int, torch.Tensor]:
    """(q, m, e, dequantized weights) of w (out, in) for inputs with Hessian h (in, in)."""
    spec = WEIGHT_FORMATS[fmt]
    if spec["method"] != "rtn-absmax":
        raise ValueError(f"GPTQ needs an absmax format, not {fmt}")
    qmax = spec["qmax"]
    W = w.clone().float()
    H = h.clone().float()
    out, inp = W.shape
    dead = torch.diag(H) == 0
    H[dead, dead] = 1
    W[:, dead] = 0
    H += damp * torch.mean(torch.diag(H)) * torch.eye(inp, device=H.device)
    hinv = torch.linalg.cholesky(torch.cholesky_inverse(torch.linalg.cholesky(H)), upper=True)
    ngroups = -(-inp // group)
    first = torch.stack([W[:, g * group : (g + 1) * group].abs().amax(dim=1) for g in range(ngroups)], dim=1) / qmax
    e = math.floor(math.log2(32767.0 / (HEADROOM * max(float(first.max()), 1e-30))))
    q = torch.zeros((out, inp), dtype=torch.int64, device=W.device)
    m = torch.zeros((out, ngroups), dtype=torch.int64, device=W.device)
    eff = torch.zeros(out, device=W.device)
    if block % group and group % block:
        raise ValueError("the block and the group size must divide one another")
    for b0 in range(0, inp, block):
        b1 = min(b0 + block, inp)
        wb = W[:, b0:b1].clone()
        err = torch.zeros_like(wb)
        hb = hinv[b0:b1, b0:b1]
        for j in range(b1 - b0):
            col = b0 + j
            if col % group == 0:
                g = col // group
                scale = W[:, col : col + group].abs().amax(dim=1) / qmax
                scale = torch.where(scale > 0, scale, torch.full_like(scale, 1e-12))
                m[:, g] = torch.clamp(torch.round(scale * 2.0**e), 1, 32767).long()
                eff = m[:, g].float() * 2.0**-e
            x = wb[:, j]
            qc = torch.clamp(torch.round(x / eff), -qmax, qmax)
            q[:, col] = qc.long()
            e_j = (x - qc * eff) / hb[j, j]
            wb[:, j:] -= e_j[:, None] * hb[j, j:][None, :]
            err[:, j] = e_j
        W[:, b1:] -= err @ hinv[b0:b1, b1:]
    deq = q.float() * (m.float() * 2.0**-e).repeat_interleave(group, dim=1)[:, :inp]
    return q.cpu().numpy(), m.cpu().numpy(), e, deq


def _format_of(key: str, fmt: str, overrides: dict[str, str]) -> str:
    """The weight format of a matrix: the first override whose pattern matches its key
    (`<layer>.<node>` or a top-level node id, e.g. `lm_head`), else `fmt`."""
    for pattern, f in overrides.items():
        if re.fullmatch(pattern, key):
            return f
    return fmt


def calibrate(
    graph: dict,
    weight: Callable[[str], np.ndarray],
    sequences: list[list[int]],
    fmt: str,
    group: int,
    overrides: dict[str, str] | None = None,
    device: str = "cuda",
    batch: int = 8,
    damp: float = 0.01,
    progress: Callable[[str], None] = lambda message: None,
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray, int, str]], dict[str, float]]:
    """({weight tensor name: (q, m, e, format)}, activation maxima as `graph_l2.calibrate`)."""
    overrides = overrides or {}
    fg = FloatGraph(graph, weight, device)
    n = graph["nodes"]
    ids = torch.as_tensor(np.asarray(sequences, dtype=np.int64), device=fg.device)
    envs = []
    for b0 in range(0, len(ids), batch):
        env = {"tokens": ids[b0 : b0 + batch]}
        with torch.no_grad():
            fg.run(n["prologue"], None, env)
        envs.append({"x": env["x"]})
    quantized: dict[str, tuple[np.ndarray, np.ndarray, int, str]] = {}
    stats: dict[str, float] = {}

    def seen(kind: str, layer: int | None, value: torch.Tensor) -> None:
        mx = float(value.abs().max())
        stats[kind] = max(stats.get(kind, 0.0), mx)
        if layer is not None:
            stats[f"{layer}.{kind}"] = max(stats.get(f"{layer}.{kind}", 0.0), mx)

    def run_layer(nodes: list[dict], layer: int | None, hessians: dict | None) -> None:
        def observe(node, x):
            if hessians is not None:
                hessians[node["id"]] = hessians.get(node["id"], 0) + (x.T @ x).double()

        for k, env in enumerate(envs):
            local = dict(env)
            with torch.no_grad():
                if hessians is None:
                    _run_with_stats(fg, nodes, layer, local, seen)
                else:
                    fg.run(nodes, layer, local, observe)
            if hessians is None:
                envs[k] = {"x": local["x"]}

    def quantize_nodes(nodes: list[dict], layer: int | None, hessians: dict) -> None:
        for node in nodes:
            if node["op"] != "linear" or node["id"] not in hessians:
                continue
            name = FloatGraph._name(node, "weight", layer)
            key = node["id"] if layer is None else f"{layer}.{node['id']}"
            f = _format_of(key, fmt, overrides)
            q, m, e, deq = gptq(fg.w(name), hessians[node["id"]], f, group, damp)
            quantized[name] = (q, m, e, f)
            fg.override[name] = deq

    for i in range(n["repeat"]):
        hessians: dict = {}
        run_layer(n["block"], i, hessians)
        quantize_nodes(n["block"], i, hessians)
        del hessians
        run_layer(n["block"], i, None)
        progress(f"layer {i + 1} of {n['repeat']} quantized")
    # The output layer: final norm, then lm_head (tied to the embedding or its own matrix).
    head = n["epilogue"]
    norm = [node for node in head if node["op"] == "rmsnorm"]
    hessians = {}
    for env in envs:
        local = dict(env)
        with torch.no_grad():
            fg.run(norm, None, local)
            out = local[norm[0]["outputs"][0]]
            seen("xn", None, out)
            h = out.reshape(-1, out.shape[-1])
            hessians["lm_head"] = hessians.get("lm_head", 0) + (h.T @ h).double()
    quantize_nodes([node for node in head if node["op"] == "linear"], None, hessians)
    lm_head = next(node for node in head if node["op"] == "linear")
    for env in envs:
        local = dict(env)
        with torch.no_grad():
            fg.run([*norm, lm_head], None, local)
        seen("logits", None, local["logits"])
    progress("output layer quantized")
    return quantized, stats


def _run_with_stats(fg: FloatGraph, nodes: list[dict], layer: int | None, env: dict, seen) -> None:
    """Runs nodes one by one and records the largest magnitude of every tensor kind."""
    kinds_in: dict[str, str] = {}
    for node in nodes:
        fg.run([node], layer, env)
        if node["op"] == "rope":
            for name in node["inputs"]:
                seen(kinds_in[name], layer, env[name])
            continue
        kind = KIND_OF.get(node["id"])
        if kind:
            seen(kind, layer, env[node["outputs"][0]])
            kinds_in[node["outputs"][0]] = kind
