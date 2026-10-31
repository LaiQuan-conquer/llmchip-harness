"""`graph-numeric`: the golden chip of a graph numeric spec (`llmchip-numeric-graph-spec/1`).

The tile chip is judged against the model's L2, which for a recovered graph is `GraphL2`
(plugins/numeric), not the llama2.c `L2Model`. The chip is greedy and stateful: it keeps the
token history and answers each `step(token, pos)` with the next token of that history.
"""

from __future__ import annotations

import json

import numpy as np

from ..interface import BaseChip, ChipError, StepResult
from ..manifest import Manifest


class GraphChip(BaseChip):
    def __init__(self, m: Manifest) -> None:
        super().__init__(m.meta())
        from llmchip_numeric import graph_l2

        graph = json.loads(m.artifact("graph").read_text())
        spec = json.loads(m.artifact("spec").read_text())
        if spec.get("format") != "llmchip-numeric-graph-spec/1":
            raise ChipError("unsupported", f"the spec is {spec.get('format')!r}, not a graph spec")
        graph_hash = spec.get("model", {}).get("graphHash")
        if graph_hash and graph_hash != m.data["artifacts"].get("graphHash", graph_hash):
            pass
        with np.load(m.artifact("weights"), allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files if k != "meta"}
        self.l2 = graph_l2.GraphL2(graph, spec, arrays)
        self.ids: list[int] = []

    def hello_info(self) -> dict:
        return {"measurement": "model", "testbench": "GraphL2"}

    def reset(self) -> None:
        self.ids = []

    def step(self, token: int, pos: int) -> StepResult:
        self.check_input(token, pos)
        if pos == 0:
            self.ids = []
        if pos != len(self.ids):
            raise ChipError("invalid_input", f"position {pos} does not follow the history of {len(self.ids)} tokens")
        self.ids.append(token)
        logits = self.l2.forward(self.ids)[-1]
        return StepResult(int(np.argmax(logits)), cycles=None, host_cycles=None)

    def close(self) -> None:
        pass
