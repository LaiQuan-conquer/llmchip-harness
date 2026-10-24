"""lm-evaluation-harness models for the benchmarks (requirements FR-QNT-04): L0 and L2.

- L0 is the upstream implementation in its native precision (FR-MDL-06): lm-eval's own
  Hugging Face model (`HFLM`), bfloat16 for Qwen2.5, on the GPU when there is one.
- L2 is the bit-exact model of the numeric spec, run by the fast L2 (`l2_torch.TorchL2`).
  `L2LM` is the same `HFLM` (same tokenizer, chat template, few-shot prompts, request
  batching, stop sequences and post-processing) with its two model calls replaced:
  log-likelihoods come from the chip's integer logits (host log-softmax of logits * 2^-frac,
  in float64) and generation is greedy decoding with the KV cache, argmax with the lowest
  token id on ties (the decoding rule of the numeric spec).

Both get the same `max_length`, the context of the numeric spec, so that L0 and L2 see
exactly the same (left-truncated) prompts, and both decode greedily: the model's own generation
settings (Qwen2.5-Instruct sets a repetition penalty of 1.1 and sampling) are replaced by a
neutral configuration that keeps only its end-of-sequence tokens, so L0 is not penalized for
repeating where L2, plain argmax, is not.
"""

from __future__ import annotations

import numpy as np
import torch
from lm_eval.models.huggingface import HFLM
from lm_eval.models.utils import postprocess_generated_text
from transformers import GenerationConfig

from .l2_torch import TorchKVCache, TorchL2

TOKEN_BUDGET = 4096  # padded tokens per L2 prefill batch
ATTENTION_BUDGET = 1 << 23  # query x key positions per L2 prefill batch (one head: 64 MiB of int64)


def greedy_only(lm: HFLM) -> HFLM:
    """Replaces the model's generation settings with greedy decoding and its end tokens."""
    given = lm.model.generation_config
    lm.model.generation_config = GenerationConfig(
        bos_token_id=given.bos_token_id, eos_token_id=given.eos_token_id, pad_token_id=given.pad_token_id
    )
    return lm


def l0_model(model_dir: str, max_length: int, batch_size: int, device: str) -> HFLM:
    lm = HFLM(
        pretrained=model_dir,
        dtype="bfloat16",
        device=device,
        max_length=max_length,
        batch_size=batch_size,
        trust_remote_code=False,
    )
    return greedy_only(lm)


def log_softmax(logits: np.ndarray) -> np.ndarray:
    z = logits.astype(np.float64)
    m = z.max(axis=-1, keepdims=True)
    return z - m - np.log(np.exp(z - m).sum(axis=-1, keepdims=True))


class L2LM(HFLM):
    """lm-eval's Hugging Face model with the model calls answered by the L2."""

    def __init__(self, model_dir: str, l2: TorchL2, max_length: int, batch_size: int, progress=None) -> None:
        # The upstream model is loaded on the CPU only for lm-eval's bookkeeping (config,
        # tokenizer); no output of it is used.
        super().__init__(
            pretrained=model_dir,
            dtype="bfloat16",
            device="cpu",
            max_length=max_length,
            batch_size=batch_size,
            trust_remote_code=False,
        )
        greedy_only(self)
        self.l2 = l2
        self.scale = 2.0**-l2.fl
        self.progress = progress or (lambda message: None)
        eos = self.model.generation_config.eos_token_id
        self.eos_ids = set(eos if isinstance(eos, list) else [eos]) | {self.tokenizer.eos_token_id}

    def loglikelihood_rolling(self, requests, disable_tqdm: bool = False):
        raise NotImplementedError("rolling log-likelihoods (perplexity tasks) are not used by the benchmarks")

    def _loglikelihood_tokens(self, requests, disable_tqdm: bool = False, override_bs=None):
        out: list[tuple[float, bool] | None] = [None] * len(requests)
        groups: dict[tuple[int, ...], list[tuple[int, list[int]]]] = {}
        for i, (_, context, continuation) in enumerate(requests):
            full = (context + continuation)[-(self.max_length + 1) :]
            # A scored token needs a logit from the input window: at most len(full) - 1
            # continuation tokens survive a window that cuts into the continuation.
            keep = min(len(continuation), len(full) - 1)
            if keep < 1:
                raise ValueError("the continuation does not fit the context")
            groups.setdefault(tuple(full[:-1]), []).append((i, continuation[len(continuation) - keep :]))
        items = sorted(groups.items(), key=lambda kv: -len(kv[0]))
        done = 0
        while items:
            width = len(items[0][0])
            batch = items[: max(1, min(TOKEN_BUDGET // width, ATTENTION_BUDGET // (width * width)))]
            items = items[len(batch) :]
            seqs = [list(inp) for inp, _ in batch]
            need = [max(len(c) for _, c in reqs) for _, reqs in batch]
            rows = [list(range(len(s) - n, len(s))) for s, n in zip(seqs, need, strict=True)]
            logits = self.l2.forward_batch(seqs, rows=rows)
            for (_, reqs), lg, n in zip(batch, logits, need, strict=True):
                lp = log_softmax(lg * self.scale)
                for i, cont in reqs:
                    sel = lp[n - len(cont) :]
                    picked = sel[np.arange(len(cont)), cont]
                    out[i] = (float(picked.sum()), bool((sel.argmax(axis=-1) == np.asarray(cont)).all()))
            done += len(batch)
            self.progress(f"log-likelihoods: {done} inputs")
        return out

    def _model_generate(self, context, max_length: int, stop: list[str], **generation_kwargs):
        mask = generation_kwargs.get("attention_mask")
        ctx = context.tolist()
        rows = mask.tolist() if mask is not None else [[1] * len(r) for r in ctx]
        prompts = [[t for t, m in zip(r, mk, strict=True) if m] for r, mk in zip(ctx, rows, strict=True)]
        return self.generate_tokens(context, prompts, max_length - context.shape[1], stop)

    def generate_tokens(self, context: torch.Tensor, prompts: list[list[int]], max_new: int, stop: list[str]):
        """Greedy decoding of all prompts at once; each stops at an end-of-sequence token, a stop
        string or `max_new` tokens. Returns the context with the generated tokens appended (as
        `model.generate` does), padded with the pad token."""
        B = len(prompts)
        cache = TorchKVCache(slots=B)
        last = self.l2.forward_batch(prompts, cache, list(range(B)), [[len(p) - 1] for p in prompts])
        generated: list[list[int]] = [[] for _ in range(B)]
        active = list(range(B))
        nxt = {b: int(np.argmax(last[b][-1])) for b in range(B)}
        for step in range(max_new):
            still = []
            for b in active:
                generated[b].append(nxt[b])
                text = self.tokenizer.decode(generated[b], skip_special_tokens=False)
                if nxt[b] in self.eos_ids or any(s and s in text for s in stop) or step + 1 >= max_new:
                    continue
                still.append(b)
            active = still
            if not active:
                break
            out = self.l2.forward_batch([[nxt[b]] for b in active], cache, active)
            nxt = {b: int(np.argmax(o[-1])) for b, o in zip(active, out, strict=True)}
            if step % 32 == 0:
                self.progress(f"generation: step {step}, {len(active)} of {B} still running")
        pad = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else 0
        width = max(len(g) for g in generated)
        tail = torch.full((B, width), pad, dtype=context.dtype)
        for b, g in enumerate(generated):
            tail[b, : len(g)] = torch.as_tensor(g, dtype=context.dtype)
        return torch.cat([context.cpu(), tail], dim=1)


def generate_texts(lm: HFLM, prompts: list[str], until: list[str], max_gen_toks: int) -> list[str]:
    """Greedy continuations of raw prompts (EvalPlus code generation) through the same path as
    lm-eval's `generate_until`: one batch, cut at the first stop string."""
    out = []
    for i in range(0, len(prompts), lm.batch_size):
        chunk = prompts[i : i + lm.batch_size]
        enc, mask = lm.tok_batch_encode(chunk, left_truncate_len=lm.max_length - max_gen_toks)
        cont = lm._model_generate(
            context=enc.to(lm.device),
            attention_mask=mask.to(lm.device),
            stop=until,
            max_length=enc.shape[1] + max_gen_toks,
            do_sample=False,
        )
        for row in cont.tolist():
            text = lm.tok_decode(row[enc.shape[1] :])
            out.append(postprocess_generated_text(generation=text, stop=until, think_end_token=None))
    return out
