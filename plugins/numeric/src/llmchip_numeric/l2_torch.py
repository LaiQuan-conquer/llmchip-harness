"""The fast L2: the graph L2 (`graph_l2.GraphL2`) on PyTorch, on the GPU when there is one.

The same integer arithmetic as the reference, bit for bit: every value is an int64 tensor,
and the rounding, overflow, table and accumulator rules are those of `arith.py`. Matrix
products are the only floating-point operations, and they are exact: the operands are split
into 8-bit limbs (x = 256 * hi + lo; hi signed, lo in [0, 255]) and the reduction is cut into
blocks of at most 256 terms, so that no partial sum reaches 2^24, which float32 represents
exactly whatever the order of summation. TF32 is switched off: it keeps only 11 significant
bits of each float32 input (tools/selftest/gpu_exact.py measured 32,733 of 32,768 products of
16-bit operands wrong with it on). The check `spec.l2.fast` compares this model with the
reference on every observation point.

Several sequences run in one batch (`forward_batch`), right-padded to the same length, each
with its own position in a slotted KV cache. Every row is computed exactly as it would be
alone: a padded position comes after every real position of its sequence, so the causal mask
already hides it, and the overflow counts skip it. Batching is what makes a benchmark of
thousands of generated answers affordable: the cost of a step is dominated by launching the
kernels, not by the batch size.
"""

from __future__ import annotations

import numpy as np
import torch

from . import graph_l2
from .graph_l2 import EXACT_GROUP_MAX, KIND_OF, GraphL2, _key, _name, _nodes
from .spec import EXP_RANGE, SCORE_FRAC, SIG_RANGE, SIG_STEPS

BLOCK = 256  # terms per exact float32 partial sum: 256 * 255 * 255 < 2^24
ROW_CHUNK_ELEMENTS = 1 << 24


def exact_mode() -> None:
    """Float32 matrix products in full precision (no TF32, no reduced-precision reductions)."""
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
    torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
    torch.set_float32_matmul_precision("highest")


def default_device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def mm16(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    """Exact a @ b of int64 tensors whose values lie in [-32768, 65535] (batched over leading axes)."""
    ah, al = (a >> 8).float(), (a & 255).float()
    bh, bl = (b >> 8).float(), (b & 255).float()
    out = None
    for k0 in range(0, a.shape[-1], BLOCK):
        k = slice(k0, k0 + BLOCK)
        hh = torch.matmul(ah[..., k], bh[..., k, :]).long()
        mid = torch.matmul(ah[..., k], bl[..., k, :]).long() + torch.matmul(al[..., k], bh[..., k, :]).long()
        ll = torch.matmul(al[..., k], bl[..., k, :]).long()
        part = (hh << 16) + (mid << 8) + ll
        out = part if out is None else out + part
    return out


class TorchKVCache:
    """Keys (after RoPE) and values per decoder layer for `slots` sequences, on the model's
    device: `k[layer]` is (slots, context, kv_heads * head_dim); `lengths[s]` positions of slot
    s are filled. With one slot it is used as `graph_l2.KVCache`."""

    def __init__(self, slots: int = 1) -> None:
        self.slots = slots
        self.k: dict[int, torch.Tensor] = {}
        self.v: dict[int, torch.Tensor] = {}
        self.lengths = [0] * slots

    @property
    def length(self) -> int:
        return self.lengths[0]

    def reset(self, slot: int) -> None:
        """Frees a slot for a new sequence (its old entries are hidden by the causal mask)."""
        self.lengths[slot] = 0


class TorchL2:
    """Executes a graph numeric spec as `GraphL2` does, with PyTorch tensors on `device`."""

    def __init__(self, ref: GraphL2, device: str | None = None) -> None:
        exact_mode()
        self.ref = ref
        self.device = torch.device(device or default_device())
        self.f, self.layer_frac, self.bits = ref.f, ref.layer_frac, ref.bits
        self.fn, self.fl, self.group, self.exp = ref.fn, ref.fl, ref.group, ref.exp
        self.context, self.rounding, self.overflow_mode = ref.context, ref.rounding, ref.overflow
        self.acc_bits, self.att_scale, self.tied = ref.acc_bits, ref.att_scale, ref.tied
        if self.group > EXACT_GROUP_MAX:
            raise ValueError(f"groups of more than {EXACT_GROUP_MAX} weights are not supported")
        self.graph = ref.graph
        dev = self.device
        self.t: dict[str, torch.Tensor] = {}
        self.w: dict[str, torch.Tensor] = {}  # float32 weights per group: (groups, group size, rows)
        for name, a in ref.a.items():
            if name.endswith(".q"):
                key = name[:-2]
                out, inp = a.shape
                ngroups = -(-inp // self.group)
                padded = np.zeros((out, ngroups * self.group), np.float32)
                padded[:, :inp] = a
                grouped = padded.reshape(out, ngroups, self.group).transpose(1, 2, 0).copy()
                self.w[key] = torch.from_numpy(grouped).to(dev)
                if key == "embed":
                    self.t[name] = torch.from_numpy(a.astype(np.int64)).to(dev)
            else:
                self.t[name] = torch.from_numpy(a.astype(np.int64)).to(dev)
        self._counts: dict[str, list[torch.Tensor]] = {}
        self._sat: dict[str, int] = {}
        self._peaks: list[torch.Tensor] = []  # |input| of every product, checked once per forward
        self._rows: torch.Tensor | None = None  # (N,) real rows of the current batch

    @property
    def vocab(self) -> int:
        return self.ref.spec["hyper"]["vocab"]

    @property
    def saturations(self) -> dict[str, int]:
        return dict(self._sat)

    # -- arithmetic (arith.py on tensors) ------------------------------------------

    def _narrow(self, v: torch.Tensor, bits: int, kind: str, where: torch.Tensor | None = None) -> torch.Tensor:
        """`arith.overflow` with the events counted; `where` (broadcast to v) limits the count, and
        defaults to the real rows when v is row-major."""
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        clamped = v.clamp(lo, hi)
        outside = clamped != v
        if where is None and self._rows is not None and v.shape[0] == self._rows.shape[0]:
            where = self._rows.reshape(-1, *([1] * (v.dim() - 1)))
        self._counts.setdefault(kind, []).append((outside if where is None else outside & where).sum())
        if self.overflow_mode == "saturate":
            return clamped
        return ((v - lo) & ((1 << bits) - 1)) + lo

    def _rsh(self, v: torch.Tensor, s) -> torch.Tensor:
        if isinstance(s, torch.Tensor):
            right = self._right(v, s.clamp(min=1))
            return torch.where(s > 0, right, v << (-s).clamp(min=0))
        if s < 0:
            return v << (-s)
        if s == 0:
            return v
        return self._right(v, s)

    def _right(self, v: torch.Tensor, s):
        one = torch.ones_like(s) if isinstance(s, torch.Tensor) else 1
        if self.rounding == "half-up":
            return (v + (one << (s - 1))) >> s
        if self.rounding == "half-even":
            return (v + ((one << (s - 1)) - 1) + ((v >> s) & 1)) >> s
        if self.rounding == "truncate":
            return v >> s
        raise ValueError(f"unknown rounding mode {self.rounding}")

    # -- operators -------------------------------------------------------------------

    def linear(self, key: str, x: torch.Tensor, f_in: int, f_out: int, kind: str, bits: int = 16) -> torch.Tensor:
        w = self.w[key]
        m = self.t[f"{key}.m"]
        ngroups, g, out = w.shape
        T, inp = x.shape
        self._peaks.append(x.abs().amax())  # the limb split is exact only for 16-bit inputs
        if inp == ngroups * g:
            xp = x
        else:
            xp = torch.zeros((T, ngroups * g), dtype=torch.int64, device=self.device)
            xp[:, :inp] = x
        # hi and lo limbs side by side: one batched product per row chunk.
        limbs = torch.cat([xp >> 8, xp & 255]).float().reshape(2 * T, ngroups, g).transpose(0, 1)
        rows = max(1, ROW_CHUNK_ELEMENTS // max(1, ngroups * T))
        y = torch.empty((T, out), dtype=torch.int64, device=self.device)
        shift = f_in + self.exp[key] - f_out
        for r0 in range(0, out, rows):
            r1 = min(out, r0 + rows)
            both = torch.bmm(limbs, w[:, :, r0:r1]).long()  # (G, 2T, rows)
            terms = (both[:, :T] * 256 + both[:, T:]) * m[r0:r1].T[:, None, :]
            if self.acc_bits is None:
                total = terms.sum(dim=0)
            else:
                total = torch.zeros_like(terms[0])
                for gi in range(ngroups):
                    total = self._narrow(total + terms[gi], self.acc_bits, "accumulator")
            y[:, r0:r1] = self._rsh(total, shift)
        if f"{key}.b" in self.t:
            y = y + self.t[f"{key}.b"]
        return self._narrow(y, bits, kind)

    def rmsnorm(self, x: torch.Tensor, weight: torch.Tensor, f_in: int, f_out: int, eps: float) -> torch.Tensor:
        eps_q = round(eps * 2.0 ** (2 * f_in))
        v = torch.clamp(torch.div((x * x).sum(dim=-1), x.shape[-1], rounding_mode="floor") + eps_q, min=1)
        k = torch.frexp(v.double()).exponent.long() - 1
        e2 = k - (k % 2)
        t = torch.where(e2 >= 6, v >> (e2 - 6).clamp(min=0), v << (6 - e2).clamp(min=0))
        r = self.t["lut.rsqrt"][t - 64]
        shift = (self.fn + 15 + torch.div(e2, 2, rounding_mode="floor") - f_out)[:, None]
        return self._narrow(self._rsh(x * weight[None, :] * r[:, None], shift), 16, "norm")

    def rope(self, v: torch.Tensor, heads: int, hd: int, style: str, kind: str, pos: torch.Tensor) -> torch.Tensor:
        """Rows of v (N, heads * hd) rotated at their positions `pos` (N,)."""
        N = v.shape[0]
        t = v.reshape(N, heads, hd)
        c = self.t["lut.rope_cos"][pos][:, None, :]
        s = self.t["lut.rope_sin"][pos][:, None, :]
        out = torch.empty_like(t)
        if style == "half":
            a, b = t[..., : hd // 2], t[..., hd // 2 :]
            out[..., : hd // 2] = self._narrow(self._rsh(a * c - b * s, 14), 16, kind)
            out[..., hd // 2 :] = self._narrow(self._rsh(b * c + a * s, 14), 16, kind)
        else:
            a, b = t[..., 0::2], t[..., 1::2]
            out[..., 0::2] = self._narrow(self._rsh(a * c - b * s, 14), 16, kind)
            out[..., 1::2] = self._narrow(self._rsh(a * s + b * c, 14), 16, kind)
        return out.reshape(N, heads * hd)

    def attention(
        self, q, k, v, fq: int, fk: int, heads: int, kv_heads: int, hd: int, window, qpos, valid
    ) -> torch.Tensor:
        """Queries q (B, T, heads * hd) at positions `qpos` (B, T) against keys and values
        (B, S, kv_heads * hd) of positions 0..S-1; `valid` (B, T) marks the real queries."""
        B, T, S = q.shape[0], q.shape[1], k.shape[1]
        group = heads // kv_heads
        qh = q.reshape(B, T, heads, hd).transpose(1, 2)
        kh = k.reshape(B, S, kv_heads, hd).transpose(1, 2).repeat_interleave(group, dim=1)
        vh = v.reshape(B, S, kv_heads, hd).transpose(1, 2).repeat_interleave(group, dim=1)
        j = torch.arange(S, device=self.device)[None, None, :]
        i = qpos[:, :, None]
        allowed = j <= i
        if window:
            allowed &= j > i - window
        allowed = allowed[:, None].expand(B, heads, T, S)
        counted = allowed & valid[:, None, :, None]
        raw = self._rsh(mm16(qh, kh.transpose(2, 3)) * self.att_scale, fq + fk + 15 - SCORE_FRAC)
        s = self._narrow(raw, 32, "score", counted)
        smax = torch.where(allowed, s, torch.iinfo(torch.int64).min).amax(dim=-1, keepdim=True)
        d = torch.where(allowed, torch.clamp(smax - s, max=EXP_RANGE * 2**SCORE_FRAC - 1), 0)
        e = torch.where(allowed, self.t["lut.exp"][d >> (SCORE_FRAC - 5)], 0)
        p = torch.div(e << 15, e.sum(dim=-1, keepdim=True), rounding_mode="floor")
        out = self._narrow(self._rsh(mm16(p, vh), 15), 16, "att", valid[:, None, :, None])
        return out.transpose(1, 2).reshape(B * T, heads * hd)

    def silu_gate(self, h1: torch.Tensor, f1: int, h3: torch.Tensor, f3: int, fg: int) -> torch.Tensor:
        z = torch.clamp(h1, -SIG_RANGE * 2**f1, SIG_RANGE * 2**f1 - 1)
        idx = (z >> (f1 - 4) if f1 >= 4 else z << (4 - f1)) + SIG_RANGE * SIG_STEPS
        s = self._rsh(h1 * self.t["lut.sigmoid"][idx], 15)
        return self._narrow(self._rsh(s * h3, f1 + f3 - fg), 16, "g")

    # -- forward -----------------------------------------------------------------------

    def frac(self, kind: str, layer: int | None) -> int:
        if kind != "x" and layer is not None and self.layer_frac:
            return self.layer_frac[layer][kind]
        return self.f[kind]

    def forward(
        self,
        tokens: list[int],
        hidden: list[np.ndarray] | None = None,
        cache: TorchKVCache | None = None,
        rows: list[int] | None = None,
    ) -> np.ndarray:
        """As `GraphL2.forward`: integer logits of the positions of `tokens` (or of `rows`)."""
        if cache is not None and cache.slots != 1:
            raise ValueError("a single sequence needs a cache with one slot")
        out = self.forward_batch([tokens], cache, [0] if cache is not None else None, [rows], hidden)
        return out[0]

    def forward_batch(
        self,
        sequences: list[list[int]],
        cache: TorchKVCache | None = None,
        slots: list[int] | None = None,
        rows: list[list[int] | None] | None = None,
        hidden: list[np.ndarray] | None = None,
    ) -> list[np.ndarray]:
        """Integer logits of several sequences at once: for sequence b, the positions `rows[b]`
        (indices into `sequences[b]`; all of them when None). With `cache`, sequence b continues
        slot `slots[b]`. `hidden` (one sequence only) receives the observation points as in
        `GraphL2.forward`."""
        dev = self.device
        B = len(sequences)
        lengths = [len(s) for s in sequences]
        if min(lengths) < 1:
            raise ValueError("every sequence needs at least one token")
        if hidden is not None and B != 1:
            raise ValueError("observation points are collected for one sequence at a time")
        T = max(lengths)
        p0 = [cache.lengths[s] for s in slots] if cache is not None else [0] * B
        for b in range(B):
            if p0[b] + lengths[b] > self.context:
                raise ValueError(f"{p0[b] + lengths[b]} positions exceed the context of {self.context}")
        ids = torch.zeros((B, T), dtype=torch.int64)
        for b, s in enumerate(sequences):
            ids[b, : len(s)] = torch.as_tensor(s, dtype=torch.int64)
        ids = ids.to(dev)
        steps = torch.arange(T, device=dev)[None, :]
        valid = steps < torch.as_tensor(lengths, device=dev)[:, None]
        qpos = torch.as_tensor(p0, device=dev)[:, None] + steps
        pos = torch.where(valid, qpos, 0).reshape(-1)  # padded rows read position 0 of the tables
        self._rows = valid.reshape(-1)
        flat_ids = ids.reshape(-1)
        bx = self.bits.get("x", 16)
        env: dict[str, torch.Tensor] = {}
        fr: dict[str, int] = {}
        lazy: dict[str, str] = {}
        keep: list[torch.Tensor] = []
        for node, layer in _nodes(self.graph):
            op, a, key = node["op"], node.get("attrs", {}), _key(node, layer)
            ins, outs = node["inputs"], node["outputs"]
            if op == "embedding":
                q = self.t[f"{key}.q"][flat_ids]
                m = self.t[f"{key}.m"][flat_ids]
                idx = torch.arange(q.shape[1], device=dev) // self.group
                env[outs[0]] = self._narrow(self._rsh(q * m[:, idx], self.exp[key] - self.f["x"]), bx, "x")
                fr[outs[0]] = self.f["x"]
            elif op == "rmsnorm":
                fo = self.frac("xn", layer)
                env[outs[0]] = self.rmsnorm(env[ins[0]], self.t[f"{key}.w"], fr[ins[0]], fo, a["eps"])
                fr[outs[0]] = fo
            elif op == "linear":
                wkey = self.tied[_name(node, "weight", layer)]
                if node["id"] == "lm_head":
                    picks = [
                        b * T + t
                        for b in range(B)
                        for t in (rows[b] if rows and rows[b] is not None else range(lengths[b]))
                    ]
                    h = env[ins[0]][torch.as_tensor(picks, device=dev)]
                    self._rows = None
                    env[outs[0]] = self.linear(wkey, h, fr[ins[0]], self.fl, "logits", bits=32)
                    fr[outs[0]] = self.fl
                else:
                    kind = KIND_OF[node["id"]]
                    fo = self.frac(kind, layer)
                    env[outs[0]] = self.linear(wkey, env[ins[0]], fr[ins[0]], fo, kind, self.bits.get(kind, 16))
                    fr[outs[0]] = fo
            elif op == "head_rmsnorm":
                x = env[ins[0]]
                kind = KIND_OF[node["id"]]
                fo = self.frac(kind, layer)
                heads = x.reshape(-1, a["headDim"])
                self._rows, rows_of = self._rows.repeat_interleave(a["heads"]), self._rows
                out = self.rmsnorm(heads, self.t[f"{key}.w"], fr[ins[0]], fo, a["eps"])
                self._rows = rows_of
                env[outs[0]] = out.reshape(x.shape)
                fr[outs[0]] = fo
            elif op == "rope":
                hd = a["headDim"]
                env[outs[0]] = self.rope(env[ins[0]], a["heads"], hd, a["style"], "q", pos)
                env[outs[1]] = self.rope(env[ins[1]], a["kvHeads"], hd, a["style"], "k", pos)
                fr[outs[0]], fr[outs[1]] = fr[ins[0]], fr[ins[1]]
            elif op == "attention":
                q, k, v = (env[n].reshape(B, T, -1) for n in ins)
                if cache is not None:
                    if layer not in cache.k:
                        shape = (cache.slots, self.context, k.shape[-1])
                        cache.k[layer] = torch.zeros(shape, dtype=torch.int64, device=dev)
                        cache.v[layer] = torch.zeros(shape, dtype=torch.int64, device=dev)
                    sl = torch.as_tensor(slots, device=dev)[:, None].expand(B, T)
                    cache.k[layer][sl[valid], qpos[valid]] = k[valid]
                    cache.v[layer][sl[valid], qpos[valid]] = v[valid]
                    S = max(p + n for p, n in zip(p0, lengths, strict=True))
                    k = cache.k[layer][torch.as_tensor(slots, device=dev), :S]
                    v = cache.v[layer][torch.as_tensor(slots, device=dev), :S]
                env[outs[0]] = self.attention(
                    q,
                    k,
                    v,
                    fr[ins[0]],
                    fr[ins[1]],
                    a["heads"],
                    a["kvHeads"],
                    a["headDim"],
                    a.get("window"),
                    qpos,
                    valid,
                )
                fr[outs[0]] = fr[ins[2]]
            elif op == "add":
                env[outs[0]] = self._narrow(env[ins[0]] + env[ins[1]], bx, "x")
                fr[outs[0]] = fr[ins[0]]
            elif op == "silu":
                lazy[outs[0]] = ins[0]
                env[outs[0]] = env[ins[0]]
                fr[outs[0]] = fr[ins[0]]
            elif op == "mul":
                gate, up = ins
                if gate not in lazy:
                    raise ValueError("a multiplication without a preceding silu is not supported")
                fo = self.frac("g", layer)
                env[outs[0]] = self.silu_gate(env[lazy.pop(gate)], fr[gate], env[up], fr[up], fo)
                fr[outs[0]] = fo
            elif op == "argmax":
                continue
            else:
                raise ValueError(f"unknown op {op}")
            if hidden is not None:
                if node["id"] == "embed" or (node["id"] == "residual_mlp" and layer is not None):
                    keep.append(env["x"][: lengths[0]])
                elif node["id"] == "final_norm":
                    keep.append(env[outs[0]][: lengths[0]])
        self._rows = None
        self._finish()
        if cache is not None:
            for b, s in enumerate(slots):
                cache.lengths[s] = p0[b] + lengths[b]
        if hidden is not None:
            hidden.extend(t.cpu().numpy() for t in keep)
        logits = env["logits"].cpu().numpy()
        out, at = [], 0
        for b in range(B):
            n = len(rows[b]) if rows and rows[b] is not None else lengths[b]
            out.append(logits[at : at + n])
            at += n
        return out

    def _finish(self) -> None:
        """One synchronization per forward: the overflow counts and the product input check."""
        peaks, self._peaks = self._peaks, []
        counts, self._counts = self._counts, {}
        if peaks and int(torch.stack(peaks).max()) > 32768:
            raise ValueError("a matrix product input does not fit 16 bits")
        for kind, parts in counts.items():
            n = int(torch.stack(parts).sum())
            if n:
                self._sat[kind] = self._sat.get(kind, 0) + n


def check_against_reference(
    ref: GraphL2, fast: TorchL2, prompts: list[list[int]], steps: int, progress=lambda message: None
) -> dict:
    """Evidence for `spec.l2.fast`: the fast L2 generates `steps` tokens greedily from every prompt
    (all prompts in one batch, through the KV cache); the reference then computes each whole
    sequence in one pass. Compared bit for bit: the logits of every generated step, and, against
    a whole-sequence pass of the fast L2, every observation point (residual stream after the
    embedding and each layer, final norm) and the overflow counts; and the path that scores
    answers (all whole sequences in one right-padded batch, no cache): the logits of the
    scoring positions and the overflow counts, which must equal the sum over the sequences alone."""
    B = len(prompts)
    cache = TorchKVCache(slots=B)
    last = fast.forward_batch(prompts, cache, list(range(B)), [[len(p) - 1] for p in prompts])
    logits: list[list[np.ndarray]] = [[x[-1]] for x in last]
    tokens: list[list[int]] = [[int(np.argmax(x[-1]))] for x in last]
    for _ in range(steps - 1):
        out = fast.forward_batch([[t[-1]] for t in tokens], cache, list(range(B)))
        for b, o in enumerate(out):
            logits[b].append(o[-1])
            tokens[b].append(int(np.argmax(o[-1])))
    rows = []
    alone: list[tuple[str, dict]] = []  # (hash of the generated rows' logits, overflow counts)
    mismatched_logits = mismatched_points = points = 0
    seqs = [p + t[:-1] for p, t in zip(prompts, tokens, strict=True)]
    # Logits only at the generated steps' positions, in every pass, so that the counts compare.
    picks = [list(range(len(p) - 1, len(p) - 1 + steps)) for p in prompts]
    for b, prompt in enumerate(prompts):
        seq = seqs[b]
        n = len(prompt)
        ref.saturations, fast._sat = {}, {}
        h_ref: list = []
        whole = ref.forward(seq, h_ref, rows=picks[b])
        h_fast: list = []
        fast.forward(seq, h_fast, rows=picks[b])
        bad_logits = sum(not np.array_equal(whole[i], logits[b][i]) for i in range(steps))
        bad_points = sum(not np.array_equal(x, y) for x, y in zip(h_ref, h_fast, strict=True))
        same_saturations = ref.saturations == fast.saturations
        mismatched_logits += bad_logits
        mismatched_points += bad_points + (not same_saturations)
        points += len(h_ref) * len(seq)
        rows.append(
            {
                "promptTokens": n,
                "steps": steps,
                "tokens": tokens[b],
                "logitMismatches": bad_logits,
                "pointMismatches": bad_points,
                "saturationsAgree": same_saturations,
                "saturations": ref.saturations,
            }
        )
        progress(f"prompt {b + 1} of {B}: {bad_logits} logit and {bad_points} point mismatches")
        alone.append((graph_l2.array_hash(whole), dict(ref.saturations)))
    # The scoring path: the whole sequences as one padded batch.
    fast._sat = {}
    batched = fast.forward_batch(seqs, rows=picks)
    mismatched_batch = sum(graph_l2.array_hash(x) != h for x, (h, _) in zip(batched, alone, strict=True))
    expected: dict[str, int] = {}
    for _, counts in alone:
        for kind, n in counts.items():
            expected[kind] = expected.get(kind, 0) + n
    batch_saturations_agree = fast.saturations == expected
    return {
        "device": str(fast.device),
        "prompts": rows,
        "batch": {"sequenceMismatches": mismatched_batch, "saturationsAgree": batch_saturations_agree},
        "metrics": {
            "prompts": B,
            "steps": steps,
            "positions_compared": B * steps,
            "observation_vectors_compared": int(points),
            "logit_mismatches": mismatched_logits,
            "point_mismatches": mismatched_points,
            "batch_mismatches": mismatched_batch + (not batch_saturations_agree),
            "device": str(fast.device),
        },
    }
