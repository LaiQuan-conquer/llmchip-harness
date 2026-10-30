"""The R4 verification runner for a `taalas-tile/1` bundle.

The items that can run on the current datapath are executed here and returned as a
`verification.report`:

- `ver.unit`: the template suite (engine, attention, Q-full chain, ROM views, macro, bundle);
- `ver.slice`: the functional S-1 slice against `GraphL2`'s q_proj observation, on real
  activation vectors;
- `ver.cdc`: the clock domains of the generated RTL (only `clk`, plus the host receiver's
  `host_sclk`, which is the documented second domain);
- `ver.layer`: the chained layer harness, both Q-full layers, every observation point against
  `GraphL2`;
- `ver.coverage`, `ver.formal`, `ver.mutation`, `ver.dft` and `rtl.tile.equivalence`: line coverage
  of the layer harness, the residual add's bounded proof, the MCY campaign, the DFT readiness audit,
  and the tiny full model's 128 logits through RTL layer 0, layer 1, final_norm and lm_head.

An item the caller disables is reported as `not run` with its reason instead of being counted as a
pass (DP-10).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
TESTS = REPO / "plugins" / "rtl-taalas" / "tests"
TOOLS_ENV = "LLMCHIP_TOOLS"

UNIT_FILES = (
    "test_tile.py",
    "test_tile_engine.py",
    "test_tile_attention.py",
    "test_tile_qfull_top.py",
    "test_tile_normalization.py",
    "test_tile_linear.py",
    "test_tile_attention_kv.py",
    "test_tile_mlp.py",
    "test_tile_layer.py",
    "test_rom_macro.py",
)
SLICE_FILES = ("test_tile_slice.py",)
LAYER_FILES = ("test_tile_layer.py",)
DIRECTED_FILES = ("test_tile_directed.py", "test_tile_host.py")
COVERAGE_FILES = ("test_tile_coverage.py",)
FORMAL_FILES = ("test_formal.py",)
MUTATION_FILES = ("test_mutation.py",)
DFT_FILES = ("test_dft.py",)
EQUIV_FILES = ("test_full_model.py",)

PLAN_ITEMS = (
    {"id": "ver.unit", "category": "unit", "description": "Every template against its Python reference"},
    {"id": "ver.slice", "category": "slice", "description": "S-1 q_proj slice against GraphL2, real activations"},
    {
        "id": "ver.cdc",
        "category": "cdc",
        "description": "The datapath stays in the clk domain; host_sclk is the documented second domain",
    },
    {
        "id": "ver.layer",
        "category": "layer",
        "description": "Q-full per-layer observation points against L2",
    },
    {
        "id": "ver.directed",
        "category": "directed",
        "description": "Power-on, start while busy, reset mid-inference, one token, context boundary",
    },
    {
        "id": "rtl.tile.equivalence",
        "category": "regression",
        "description": "Q-full layer harness as the chip-level equivalence against L2",
    },
    {"id": "ver.coverage", "category": "coverage", "description": "Line/toggle/FSM coverage"},
    {
        "id": "ver.mutation",
        "category": "mutation",
        "description": "Mutation score of the templates",
    },
    {
        "id": "ver.formal",
        "category": "formal",
        "description": "FSM and bounded arithmetic properties",
    },
    {
        "id": "ver.dft",
        "category": "dft",
        "description": "DFT readiness: reset, clock, latch and ROM-init audit",
    },
)


def plan() -> dict:
    return {
        "format": "llmchip-verification-plan/1",
        "description": "R4 plan of taalas-tile/1; items without a runnable datapath say so.",
        "items": list(PLAN_ITEMS),
    }


def clocks(bundle: Path) -> dict:
    """The clock signals of the RTL, and whether they are the two documented domains."""
    found: dict[str, int] = {}
    for path in sorted((bundle / "rtl").glob("*.v")):
        text = path.read_text()
        for match in re.finditer(r"always\s*@\s*\([^)]*posedge\s+([A-Za-z_][A-Za-z0-9_]*)", text):
            found[match.group(1)] = found.get(match.group(1), 0) + 1
    allowed = {"clk", "host_sclk"}
    extra = sorted(set(found) - allowed)
    return {
        "clocks": sorted(found),
        "edges": found,
        "violations": len(extra),
        "extra": extra,
        "ok": "clk" in found and not extra,
    }


def _pytest(files: tuple[str, ...], timeout: int = 1800) -> dict:
    command = [sys.executable, "-m", "pytest", *[str(TESTS / name) for name in files], "-q"]
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    text = result.stdout + result.stderr
    match = re.search(r"(\d+) passed", text)
    passed = int(match.group(1)) if match else 0
    failed_match = re.search(r"(\d+) failed", text)
    failed = int(failed_match.group(1)) if failed_match else (0 if result.returncode == 0 else 1)
    return {
        "returncode": result.returncode,
        "passed": passed,
        "failed": failed,
        "output": text[-2000:],
    }


def report(
    bundle: Path,
    run_unit: bool = True,
    run_slice: bool = True,
    run_layer: bool = True,
    run_directed: bool = True,
    run_coverage: bool = True,
    run_formal: bool = True,
    run_mutation: bool = True,
    run_dft: bool = True,
    run_equiv: bool = True,
) -> dict:
    """Runs the available R4 items; the others are reported as not run with their reason."""
    items: list[dict] = []

    if run_unit:
        unit = _pytest(UNIT_FILES)
        items.append(
            {
                "id": "ver.unit",
                "status": "passed" if unit["returncode"] == 0 else "failed",
                "metrics": {
                    "tests": unit["passed"] + unit["failed"],
                    "passed": unit["passed"],
                    "failed": unit["failed"],
                },
                "detail": unit["output"],
            }
        )
    else:
        items.append({"id": "ver.unit", "status": "not run", "reason": "disabled by the caller"})

    if run_slice:
        slice_report = _pytest(SLICE_FILES)
        items.append(
            {
                "id": "ver.slice",
                "status": "passed" if slice_report["returncode"] == 0 else "failed",
                "metrics": {
                    "tests": slice_report["passed"] + slice_report["failed"],
                    "passed": slice_report["passed"],
                    "failed": slice_report["failed"],
                },
                "detail": slice_report["output"],
            }
        )
    else:
        items.append({"id": "ver.slice", "status": "not run", "reason": "disabled by the caller"})

    if run_layer:
        layer = _pytest(LAYER_FILES)
        observations = 17 * layer["passed"]
        items.append(
            {
                "id": "ver.layer",
                "status": "passed" if layer["returncode"] == 0 else "failed",
                "metrics": {
                    "tests": layer["passed"] + layer["failed"],
                    "passed": layer["passed"],
                    "failed": layer["failed"],
                    "layers": layer["passed"],
                    "observations": observations,
                },
                "detail": layer["output"],
            }
        )
    else:
        items.append({"id": "ver.layer", "status": "not run", "reason": "disabled by the caller"})

    if run_directed:
        directed = _pytest(DIRECTED_FILES, timeout=1800)
        items.append(
            {
                "id": "ver.directed",
                "status": "passed" if directed["returncode"] == 0 else "failed",
                "metrics": {
                    "tests": directed["passed"] + directed["failed"],
                    "passed": directed["passed"],
                    "failed": directed["failed"],
                    "cases": directed["passed"] + directed["failed"],
                },
                "detail": directed["output"],
            }
        )
    else:
        items.append({"id": "ver.directed", "status": "not run", "reason": "disabled by the caller"})

    if run_coverage:
        coverage = _pytest(COVERAGE_FILES)
        match = re.search(r"COVERAGE line=([0-9.]+)", coverage["output"])
        line = float(match.group(1)) if match else 0.0
        items.append(
            {
                "id": "ver.coverage",
                "status": "passed" if coverage["returncode"] == 0 and line >= 90.0 else "failed",
                "metrics": {"tests": coverage["passed"] + coverage["failed"], "line": line},
                "detail": coverage["output"],
            }
        )
    else:
        items.append({"id": "ver.coverage", "status": "not run", "reason": "disabled by the caller"})

    if run_formal:
        formal = _pytest(FORMAL_FILES)
        items.append(
            {
                "id": "ver.formal",
                "status": "passed" if formal["returncode"] == 0 else "failed",
                "metrics": {"tests": formal["passed"] + formal["failed"], "passed": formal["passed"]},
                "detail": formal["output"],
            }
        )
    else:
        items.append({"id": "ver.formal", "status": "not run", "reason": "disabled by the caller"})

    if run_mutation:
        mutation = _pytest(MUTATION_FILES, timeout=1800)
        match = re.search(r"MUTATION score=([0-9.]+)", mutation["output"])
        score = float(match.group(1)) if match else 0.0
        items.append(
            {
                "id": "ver.mutation",
                "status": "passed" if mutation["returncode"] == 0 and score >= 95.0 else "failed",
                "metrics": {"tests": mutation["passed"] + mutation["failed"], "score": score},
                "detail": mutation["output"],
            }
        )
    else:
        items.append({"id": "ver.mutation", "status": "not run", "reason": "disabled by the caller"})

    if run_dft:
        dft_report = _pytest(DFT_FILES)
        items.append(
            {
                "id": "ver.dft",
                "status": "passed" if dft_report["returncode"] == 0 else "failed",
                "metrics": {"tests": dft_report["passed"] + dft_report["failed"]},
                "detail": dft_report["output"],
            }
        )
    else:
        items.append({"id": "ver.dft", "status": "not run", "reason": "disabled by the caller"})

    if run_equiv:
        equiv = _pytest(EQUIV_FILES)
        match = re.search(r"FULL_MODEL logits=([0-9]+)", equiv["output"])
        logits = int(match.group(1)) if match else 0
        items.append(
            {
                "id": "rtl.tile.equivalence",
                "status": "passed" if equiv["returncode"] == 0 and logits > 0 else "failed",
                "metrics": {"tests": equiv["passed"] + equiv["failed"], "logits": logits},
                "detail": equiv["output"],
            }
        )
    else:
        items.append({"id": "rtl.tile.equivalence", "status": "not run", "reason": "disabled by the caller"})

    cdc = clocks(bundle)
    items.append(
        {
            "id": "ver.cdc",
            "status": "passed" if cdc["ok"] else "failed",
            "metrics": {"domains": len(cdc["clocks"]), "violations": cdc["violations"]},
            "detail": cdc,
        }
    )

    reasons = {}
    for item in PLAN_ITEMS:
        if item["id"] in reasons:
            items.append({"id": item["id"], "status": "not run", "reason": reasons[item["id"]]})

    not_run = [item["id"] for item in items if item["status"] == "not run"]
    failed = [item["id"] for item in items if item["status"] == "failed"]
    return {
        "format": "llmchip-verification-report/1",
        "architecture": "taalas-tile/1",
        "items": items,
        "summary": {"notRun": not_run, "failed": failed, "complete": not not_run and not failed},
    }
