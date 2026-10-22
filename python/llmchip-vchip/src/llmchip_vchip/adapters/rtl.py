"""`rtl`: an RTL bundle (S4) simulated under the harness's pin-level testbench.

Artifacts: `rtl` (rtl.bundle) and, for logits, `spec` (the numeric spec, for the logits'
fraction bits). `simulator.image` pins the EDA image (by digest). Opening the chip builds the
testbench binary once (cached under `<chip>/build/`), then starts the bridge
(bridges/rtl_bridge.py) as a `process` chip: the simulator runs in its own process and
container, so a crash or a hang cannot take the harness down.
"""

from __future__ import annotations

import sys

from ..bridges import verilator
from ..interface import ChipError
from ..manifest import Manifest
from .process import ProcessChip


class RtlChip(ProcessChip):
    def __init__(self, m: Manifest) -> None:
        image = m.data["simulator"]["image"]
        build_dir = verilator.build(
            m.artifact("rtl"), image, m.dir / "build", float(m.data.get("build_timeout_s", 3600))
        )
        super().__init__(m, [sys.executable, "-m", "llmchip_vchip.bridges.rtl_bridge", "{chip_dir}", str(build_dir)])
        if self.hello.get("measurement") != "testbench":
            self.close()
            raise ChipError("crash", "the RTL bridge does not report testbench measurement")
