"""`numeric`: the L2 bit-exact integer model of a numeric spec (S2). What the RTL must match
bit for bit, so it is the golden chip of a hardware backend.

Artifacts: `spec` (numeric-spec.json), `weights` (l2-weights.npz) and, for a graph numeric
spec (any recovered model), `model`.
"""

from __future__ import annotations

import json

import numpy as np
from llmchip_numeric.graph_l2 import GRAPH_WEIGHTS_FORMAT, GraphL2
from llmchip_numeric.l2 import L2Model
from llmchip_numeric.spec import GRAPH_SPEC_FORMAT, SPEC_FORMAT

from ..interface import BaseChip, ChipError, StepResult
from ..manifest import Manifest
from . import model_source


class NumericChip(BaseChip):
    def __init__(self, m: Manifest) -> None:
        super().__init__(m.meta())
        spec = json.loads(m.artifact("spec").read_text())
        fmt = spec.get("format")
        if fmt == SPEC_FORMAT:
            self.l2: L2Model | GraphL2 = L2Model.load(m.artifact("spec"), m.artifact("weights"))
            vocab, self.incremental = self.l2.vocab, True
        elif fmt == GRAPH_SPEC_FORMAT:
            graph = model_source.load(m.artifact("model"), self.meta.context).graph
            with np.load(m.artifact("weights"), allow_pickle=False) as z:
                if json.loads(str(z["meta"])).get("format") != GRAPH_WEIGHTS_FORMAT:
                    raise ChipError("crash", "the weights are not those of a graph numeric spec")
                arrays = {k: z[k] for k in z.files if k != "meta"}
            self.l2 = GraphL2(graph, spec, arrays)
            vocab, self.incremental = graph["hyper"]["vocab"], False
        else:
            raise ChipError("crash", f"unknown numeric spec format {fmt}")
        if vocab != self.meta.vocab:
            raise ChipError("crash", f"the numeric spec's vocabulary is {vocab}, the manifest says {self.meta.vocab}")
        if self.meta.context > self.l2.context:
            raise ChipError("crash", f"the manifest's context {self.meta.context} exceeds the spec's {self.l2.context}")
        self.scale = 2.0**-self.l2.fl
        self.logits = "logits" in self.meta.capabilities
        self.traces = "traces" in self.meta.capabilities and self.incremental
        self.reset()

    def reset(self) -> None:
        self.filled = 0
        self.seq: list[int] = []
        if self.incremental:
            self.l2.reset()

    def _result(self, row: np.ndarray, trace: dict | None = None) -> StepResult:
        return StepResult(
            int(np.argmax(row)),  # lowest id on ties
            logits=(row.astype(np.float64) * self.scale).tolist() if self.logits else None,
            traces={k: [v.astype(int).tolist() for v in vs] for k, vs in trace.items()} if trace else None,
        )

    def step(self, token: int, pos: int) -> StepResult:
        self.check_input(token, pos)
        if pos > self.filled:
            raise ChipError("invalid_input", f"position {pos} skips positions {self.filled}..{pos - 1}")
        self.filled = pos + 1
        if self.incremental:
            trace: dict | None = {} if self.traces else None
            row = self.l2.forward(token, pos, trace)
            return self._result(row, trace)
        self.seq = [*self.seq[:pos], token]
        return self._result(self.l2.forward(self.seq)[-1])

    def prefill(self, tokens: list[int]) -> list[StepResult]:
        self.reset()
        if self.incremental or not tokens:
            return [self.step(t, pos) for pos, t in enumerate(tokens)]
        for pos, t in enumerate(tokens):
            self.check_input(t, pos)
        self.seq, self.filled = list(tokens), len(tokens)
        return [self._result(row) for row in self.l2.forward(self.seq)]
