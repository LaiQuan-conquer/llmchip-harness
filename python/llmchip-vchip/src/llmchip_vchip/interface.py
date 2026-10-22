"""The contract between the test harness and a virtual chip.

A virtual chip executes one step: given the token at a position, it returns the next
token (and, when it knows them, the cycles the step took). Tokenization, the generation
loop, stopping, metrics and reports belong to the harness, so every backend is driven
the same way and compared on the same terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Error codes a step or a run can end with. Chips raise `ChipError` with one of them;
# the harness adds the ones only it can detect (timeout, crash, invalid_output).
ERROR_CODES = (
    "invalid_input",  # token outside the vocabulary, position outside the context
    "context_overflow",  # the prompt does not fit the context
    "invalid_output",  # the chip returned something that is not a token of the vocabulary
    "timeout",  # the chip did not answer in time
    "crash",  # the chip (or its process) failed
    "unsupported",  # the chip lacks a capability the request needs
)

# Capabilities a chip may declare.
CAPABILITIES = (
    "greedy",  # step returns the greedy (argmax, lowest id on ties) next token
    "cycles",  # step reports the clock cycles of the step
    "logits",  # step returns the logits (perplexity, divergence)
    "traces",  # step returns observation points
)


class ChipError(Exception):
    """A failure with one of `ERROR_CODES`."""

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class StepResult:
    token: int  # the next token
    cycles: int = 0  # clock cycles of the step; 0 when unknown
    host_cycles: int | None = None  # cycles of the host frames, when measured on the chip's side
    logits: list[float] | None = None  # when the chip declares `logits`
    traces: dict[str, Any] | None = None  # when the chip declares `traces`
    # Who counted the cycles: "testbench" (a pin-level testbench around the chip, independent of
    # it), "chip" (the chip under test reports them), "model" (a formula); None: the chip.
    cycles_source: str | None = None
    host_cycles_source: str | None = None
    read_cycles: int | None = None  # observation READ frames, when not part of the host's operation


@dataclass
class ChipMeta:
    id: str
    backend: str
    model: dict  # family, params, context, vocab, ...
    tokenizer: dict  # kind, path, stop tokens
    clock_mhz: float | None
    capabilities: list[str]
    host: dict
    artifacts: dict[str, str] = field(default_factory=dict)
    measurement: dict = field(default_factory=dict)  # declared sources of cycles; L3 needs "testbench"

    @property
    def context(self) -> int:
        return int(self.model["context"])

    @property
    def vocab(self) -> int:
        return int(self.model["vocab"])


@runtime_checkable
class VirtualChip(Protocol):
    def metadata(self) -> ChipMeta: ...

    def reset(self) -> None: ...

    def prefill(self, tokens: list[int]) -> list[StepResult]:
        """Feeds `tokens` at positions 0..n-1 (KV filled); one result per position."""
        ...

    def step(self, token: int, pos: int) -> StepResult: ...

    def close(self) -> None: ...


class BaseChip:
    """Shared behavior of the bundled adapters: input checks and prefill as forced steps."""

    def __init__(self, meta: ChipMeta) -> None:
        self.meta = meta

    def metadata(self) -> ChipMeta:
        return self.meta

    def check_input(self, token: int, pos: int) -> None:
        if not 0 <= token < self.meta.vocab:
            raise ChipError("invalid_input", f"token {token} outside the vocabulary of {self.meta.vocab}")
        if not 0 <= pos < self.meta.context:
            raise ChipError("invalid_input", f"position {pos} outside the context of {self.meta.context}")

    def prefill(self, tokens: list[int]) -> list[StepResult]:
        return [self.step(t, pos) for pos, t in enumerate(tokens)]

    def close(self) -> None:
        pass
