"""The float graph (L0) on PyTorch, one decoder layer at a time: for calibration (`gptq.py`).

The same operators as the NumPy interpreter `llmchip_model.execute` (rmsnorm, linear with
bias, per-head rmsnorm, RoPE in both layouts, causal grouped-query attention, SiLU gate,
residual adds), in float32, over a batch of sequences of equal length. Running layer by layer
lets the calibration see the input of every matrix product and continue with quantized
weights. Its outputs match the NumPy interpreter to float32 rounding (tests/test_gptq.py).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import torch

Observer = Callable[[dict, torch.Tensor], None]  # (linear node, its input (rows, in))


class FloatGraph:
    def __init__(self, graph: dict, weight: Callable[[str], np.ndarray], device: str) -> None:
        if not graph.get("supported"):
            raise ValueError("the graph was not recovered")
        self.graph, self.device = graph, torch.device(device)
        self._get = weight
        self.override: dict[str, torch.Tensor] = {}  # weight name -> replacement (quantized) weight

    def w(self, name: str) -> torch.Tensor:
        if name in self.override:
            return self.override[name]
        return torch.from_numpy(np.ascontiguousarray(self._get(name), dtype=np.float32)).to(self.device)

    @staticmethod
    def _name(node: dict, key: str, layer: int | None) -> str:
        return node[key] if layer is None else node[key].format(i=layer)

    def embed(self, ids: torch.Tensor) -> torch.Tensor:
        node = self.graph["nodes"]["prologue"][0]
        return self.w(node["weight"])[ids]

    @staticmethod
    def rmsnorm(x: torch.Tensor, w: torch.Tensor, eps: float) -> torch.Tensor:
        return w * (x / torch.sqrt((x * x).mean(dim=-1, keepdim=True) + eps))

    def rope(self, x: torch.Tensor, heads: int, hd: int, theta: float, style: str) -> torch.Tensor:
        B, T, _ = x.shape
        t = x.reshape(B, T, heads, hd)
        inv = 1.0 / torch.pow(
            torch.tensor(theta, dtype=torch.float32), torch.arange(0, hd, 2, dtype=torch.float32) / hd
        )
        freqs = torch.arange(T, dtype=torch.float32)[:, None] * inv[None, :]
        cos, sin = (
            torch.cos(freqs).to(self.device)[None, :, None, :],
            torch.sin(freqs).to(self.device)[None, :, None, :],
        )
        if style == "half":
            a, b = t[..., : hd // 2], t[..., hd // 2 :]
            out = torch.cat([a * cos - b * sin, b * cos + a * sin], dim=-1)
        else:
            a, b = t[..., 0::2], t[..., 1::2]
            out = torch.empty_like(t)
            out[..., 0::2], out[..., 1::2] = a * cos - b * sin, a * sin + b * cos
        return out.reshape(B, T, heads * hd)

    def attention(self, q, k, v, heads: int, kv_heads: int, hd: int, window) -> torch.Tensor:
        B, T, _ = q.shape
        group = heads // kv_heads
        q = q.reshape(B, T, heads, hd).transpose(1, 2)
        k = k.reshape(B, T, kv_heads, hd).transpose(1, 2).repeat_interleave(group, dim=1)
        v = v.reshape(B, T, kv_heads, hd).transpose(1, 2).repeat_interleave(group, dim=1)
        scores = q @ k.transpose(2, 3) / float(np.sqrt(hd))
        i, j = torch.arange(T, device=self.device)[:, None], torch.arange(T, device=self.device)[None, :]
        allowed = j <= i
        if window:
            allowed &= j > i - window
        scores = scores.masked_fill(~allowed, float("-inf"))
        return (torch.softmax(scores, dim=-1) @ v).transpose(1, 2).reshape(B, T, heads * hd)

    def run(self, nodes: list[dict], layer: int | None, env: dict, observe: Observer | None = None) -> None:
        for node in nodes:
            op, a = node["op"], node.get("attrs", {})
            ins = [env[x] for x in node["inputs"]]
            if op == "embedding":
                out = [self.embed(ins[0])]
            elif op == "rmsnorm":
                out = [self.rmsnorm(ins[0], self.w(self._name(node, "weight", layer)), a["eps"])]
            elif op == "linear":
                if observe is not None:
                    observe(node, ins[0].reshape(-1, ins[0].shape[-1]))
                y = ins[0] @ self.w(self._name(node, "weight", layer)).T
                if node.get("bias"):
                    y = y + self.w(self._name(node, "bias", layer))
                out = [y]
            elif op == "head_rmsnorm":
                x = ins[0]
                h = x.reshape(*x.shape[:-1], a["heads"], a["headDim"])
                out = [self.rmsnorm(h, self.w(self._name(node, "weight", layer)), a["eps"]).reshape(x.shape)]
            elif op == "rope":
                out = [
                    self.rope(ins[0], a["heads"], a["headDim"], a["theta"], a["style"]),
                    self.rope(ins[1], a["kvHeads"], a["headDim"], a["theta"], a["style"]),
                ]
            elif op == "attention":
                out = [self.attention(*ins, a["heads"], a["kvHeads"], a["headDim"], a.get("window"))]
            elif op == "add":
                out = [ins[0] + ins[1]]
            elif op == "silu":
                out = [torch.nn.functional.silu(ins[0])]
            elif op == "mul":
                out = [ins[0] * ins[1]]
            elif op == "argmax":
                continue
            else:
                raise ValueError(f"unknown op {op}")
            for name, value in zip(node["outputs"], out, strict=True):
                env[name] = value

    def logits(self, ids: torch.Tensor) -> torch.Tensor:
        """The whole graph on a batch (B, T): logits (B, T, vocab)."""
        n = self.graph["nodes"]
        env = {"tokens": ids}
        self.run(n["prologue"], None, env)
        for i in range(n["repeat"]):
            self.run(n["block"], i, env)
        self.run(n["epilogue"], None, env)
        return env["logits"]
