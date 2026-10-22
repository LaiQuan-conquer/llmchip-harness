"""Host protocols: how the host hands a token and position to the chip and reads the result back.

Taalas-like chips talk to the host over SPI; the frames cost cycles that compute-only
numbers leave out (tok/s = clock / (compute + host frame cycles)). `SpiHost` counts them
per step from the frame sizes the chip declares, unless the chip measures them itself
(an RTL simulation playing the host does).
"""

from __future__ import annotations

from .interface import ChipError, StepResult, VirtualChip


class DirectHost:
    """A function call: no frames, no host cycles (reference and numeric backends)."""

    kind = "direct"

    def __init__(self, chip: VirtualChip, host: dict) -> None:
        self.chip = chip

    def frame_cycles(self) -> int:
        return 0

    def check(self, token: int, pos: int) -> None:
        pass

    def step(self, token: int, pos: int) -> tuple[StepResult, int, str]:
        """(result, host cycles, where the host cycles come from)."""
        r = self.chip.step(token, pos)
        return r, 0, "none"

    def prefill(self, tokens: list[int]) -> list[tuple[StepResult, int, str]]:
        return [(r, 0, "none") for r in self.chip.prefill(tokens)]


class SpiHost(DirectHost):
    """START (token, position) and one STATUS read when the step is done; optionally a READ of
    `read_words` observation words. Every bit costs `sck_div` clock cycles."""

    kind = "spi"

    def __init__(self, chip: VirtualChip, host: dict) -> None:
        super().__init__(chip, host)
        self.h = host

    def frame_cycles(self) -> int:
        h = self.h
        bits = h["start_bits"] + h["status_bits"]
        if h["read_words"]:
            bits += h["read_header_bits"] + h["read_word_bits"] * h["read_words"]
        return bits * h["sck_div"]

    def check(self, token: int, pos: int) -> None:
        if not 0 <= token < 2 ** self.h["token_bits"]:
            raise ChipError("invalid_input", f"token {token} does not fit the {self.h['token_bits']}-bit START field")
        if not 0 <= pos < 2 ** self.h["pos_bits"]:
            raise ChipError("invalid_input", f"position {pos} does not fit the {self.h['pos_bits']}-bit START field")

    def _host(self, r: StepResult) -> tuple[StepResult, int, str]:
        if r.host_cycles is not None:
            return r, r.host_cycles, r.host_cycles_source or "chip"
        return r, self.frame_cycles(), "model"

    def step(self, token: int, pos: int) -> tuple[StepResult, int, str]:
        self.check(token, pos)
        return self._host(self.chip.step(token, pos))

    def prefill(self, tokens: list[int]) -> list[tuple[StepResult, int, str]]:
        for pos, t in enumerate(tokens):
            self.check(t, pos)
        return [self._host(r) for r in self.chip.prefill(tokens)]


HOSTS = {"direct": DirectHost, "spi": SpiHost}


def make_host(chip: VirtualChip) -> DirectHost:
    host = chip.metadata().host
    return HOSTS[host["kind"]](chip, host)
