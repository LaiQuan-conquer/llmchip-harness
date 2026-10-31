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
        import json
        import tarfile

        image = m.data["simulator"]["image"]
        timeout = float(m.data.get("build_timeout_s", 3600))
        with tarfile.open(m.artifact("rtl")) as tar:
            member = tar.extractfile("bundle/design.json")
            design = json.loads(member.read()) if member else {}
        if design.get("architecture") == "taalas-tile/1":
            # The tile chip has its own host pins and testbench (bridges/tile_bridge.py).
            build_dir = verilator.build_tile(m.artifact("rtl"), image, m.dir / "build", timeout)
            module = "llmchip_vchip.bridges.tile_bridge"
        else:
            build_dir = verilator.build(m.artifact("rtl"), image, m.dir / "build", timeout)
            module = "llmchip_vchip.bridges.rtl_bridge"
        super().__init__(m, [sys.executable, "-m", module, "{chip_dir}", str(build_dir)])
        if self.hello.get("measurement") != "testbench":
            self.close()
            raise ChipError("crash", "the RTL bridge does not report testbench measurement")
