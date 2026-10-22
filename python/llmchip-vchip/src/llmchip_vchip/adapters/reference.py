"""`reference`: the float (L0) model, executed by the recovered graph of any supported model.

The quality baseline. No cycles: it is not a chip, it is what a chip is compared with.
"""

from __future__ import annotations

import numpy as np
from llmchip_model.execute import forward

from ..interface import BaseChip, ChipError, StepResult
from ..manifest import Manifest
from . import model_source


class ReferenceChip(BaseChip):
    def __init__(self, m: Manifest) -> None:
        super().__init__(m.meta())
        src = model_source.load(m.artifact("model"), self.meta.context)
        self.graph, self.weights = src.graph, src.weights
        vocab = src.vocab
        if vocab != self.meta.vocab:
            raise ChipError("crash", f"the model's vocabulary is {vocab}, the manifest says {self.meta.vocab}")
        self.logits = "logits" in self.meta.capabilities
        self.reset()

    def reset(self) -> None:
        self.seq: list[int] = []

    def _results(self, logits: np.ndarray) -> list[StepResult]:
        # argmax takes the lowest id on ties: the decoding rule of the numeric spec.
        return [
            StepResult(int(np.argmax(row)), logits=row.astype(float).tolist() if self.logits else None)
            for row in logits
        ]

    def _advance(self, tokens: list[int], pos: int) -> np.ndarray:
        for i, t in enumerate(tokens):
            self.check_input(t, pos + i)
        if pos > len(self.seq):
            raise ChipError("invalid_input", f"position {pos} skips positions {len(self.seq)}..{pos - 1}")
        # A step at an earlier position overwrites it, as a chip overwrites that row of its KV cache.
        self.seq = [*self.seq[:pos], *tokens]
        return forward(self.graph, self.weights.get, self.seq)

    def prefill(self, tokens: list[int]) -> list[StepResult]:
        self.reset()
        return self._results(self._advance(tokens, 0)) if tokens else []

    def step(self, token: int, pos: int) -> StepResult:
        return self._results(self._advance([token], pos)[-1:])[0]
