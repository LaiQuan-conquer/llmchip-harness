"""L3 of the test harness (design §8.6): a Taalas-like RTL chip (taalas-spatial/1, the 5K proxy)
under the pin-level testbench, against its L2 golden and float reference.

The simulator tests need the pinned EDA image and are skipped, visibly, without it. The full
acceptance run (20 prompts x 64 tokens, T-18) is opt-in: LLMCHIP_VCHIP_L3=1.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from llmchip_vchip.bridges.verilator import image_present
from llmchip_vchip.build import _sim_image, new_rtl_chips
from llmchip_vchip.conform import conform
from llmchip_vchip.interface import ChipError
from llmchip_vchip.prompts import inline
from llmchip_vchip.prompts import load as load_prompts
from llmchip_vchip.registry import Registry
from llmchip_vchip.runner import Session, bench

IMAGE = _sim_image()
HAVE_IMAGE = image_present(IMAGE)
needs_image = pytest.mark.skipif(not HAVE_IMAGE, reason=f"EDA image {IMAGE} not present (podman pull it)")
FULL = os.environ.get("LLMCHIP_VCHIP_L3") == "1"


@pytest.fixture(scope="module")
def spatial(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("l3")
    new_rtl_chips("spatial-5k", d, progress=lambda _m: None)
    return d


# -- always: the manifests and the trust rules ------------------------------------------------


def test_new_rtl_makes_reference_golden_and_rtl_chips(spatial: Path):
    reg = Registry([spatial])
    assert {e.id: e.available for e in reg.all()} == {"spatial-5k": True, "spatial-5k-l2": True, "spatial-5k-ref": True}
    m = reg.get("spatial-5k").manifest.data
    assert m["model"]["cycles_per_token"] == 1327 and m["model"]["params"] == 5248 and m["model"]["vocab"] == 35
    assert m["measurement"] == {**m["measurement"], "cycles": "testbench", "host_cycles": "testbench", "trusted": True}
    assert m["policy"]["golden"] == "spatial-5k-l2" and m["policy"]["speed"]["measurement_trusted"] is True
    assert "@sha256:" in m["simulator"]["image"]


def test_rtl_manifest_needs_a_pinned_image_and_valid_measurement(spatial: Path, tmp_path: Path):
    src = spatial / "spatial-5k" / "chip.json"
    data = json.loads(src.read_text())
    for name, change, problem in (
        ("unpinned", {"simulator": {"image": "iic-osic-tools:latest"}}, "pinned by digest"),
        ("no-rtl", {"artifacts": {"spec": "numeric-spec.json"}}, "artifact rtl"),
        ("bad-measure", {"measurement": {"cycles": "trust-me", "host_cycles": "testbench"}}, "measurement.cycles"),
    ):
        d = tmp_path / name
        d.mkdir()
        for f in ("rtl.tar", "numeric-spec.json", "prompts.json"):
            (d / f).symlink_to(spatial / "spatial-5k" / f)
        (d / "chip.json").write_text(json.dumps({**data, "id": name, **change}))
        reasons = {e.id: e.reason for e in Registry([tmp_path]).all()}
        assert problem in reasons[name]


def test_a_process_chip_claiming_a_testbench_is_not_trusted(tmp_path: Path, make_process):
    """Only the harness's own bridges count cycles independently of the chip."""
    make_process(
        tmp_path,
        "toy-claims",
        "--host-cycles",
        "800",
        measurement={"cycles": "testbench", "host_cycles": "testbench", "trusted": True},
    )
    reg = Registry([tmp_path])
    r = bench(reg, "toy-claims", inline(["once upon a time"]), max_tokens=4, out_dir=tmp_path / "runs")
    assert (
        r["measurement"]["trusted"] is False and "not run under the harness's testbench" in r["missing"]["measurement"]
    )
    checks = {c["id"]: c for c in conform(reg, "toy-claims", inline(["once upon a time"]), 4)["checks"]}
    assert checks["T-15"]["verdict"] == "n/a"
    assert checks["T-16"]["verdict"] == "fail"
    assert checks["T-17"]["verdict"] == "fail"  # declares trusted measurement but offers no pin-level check


def test_l3_checks_do_not_apply_to_chips_without_a_measurement(tmp_path: Path, make_process):
    make_process(tmp_path, "toy-plain")
    checks = {
        c["id"]: c["verdict"] for c in conform(Registry([tmp_path]), "toy-plain", inline(["tom had"]), 4)["checks"]
    }
    assert checks["T-15"] == checks["T-16"] == checks["T-17"] == "n/a"


@pytest.mark.skipif(HAVE_IMAGE, reason="checks the behaviour without the image; the image is present")
def test_without_the_image_the_rtl_chip_says_so(spatial: Path):
    with pytest.raises(ChipError, match="not present"):
        Session(Registry([spatial]).get("spatial-5k").manifest)


# -- with the EDA image: the RTL under the pin-level testbench ------------------------------


@needs_image
def test_conformance_of_the_rtl_chip_including_l3_checks(spatial: Path):
    reg = Registry([spatial])
    prompts = load_prompts(spatial / "spatial-5k" / "prompts.json")
    prompts.cases = prompts.cases[:3]
    r = conform(reg, "spatial-5k", prompts, max_tokens=8, quick=True)
    verdicts = {c["id"]: c["verdict"] for c in r["checks"]}
    for cid in ("T-01", "T-02", "T-03", "T-04", "T-05", "T-07", "T-08", "T-09", "T-12", "T-15", "T-16", "T-17"):
        assert verdicts[cid] == "pass", (cid, r["checks"])
    # --quick leaves the acceptance run out, visibly: the result is incomplete, not pass.
    assert verdicts["T-18"] == verdicts["T-19"] == "not_run" and r["verdict"] == "incomplete"
    t17 = next(c for c in r["checks"] if c["id"] == "T-17")
    assert "READ equals the golden" in t17["detail"]


@needs_image
def test_rtl_is_bit_exact_with_l2_and_measured_by_the_testbench(spatial: Path, tmp_path: Path):
    """T-10/T-16/T-19 on four prompts to the end of the context; T-18 (all 20) is the opt-in run."""
    prompts = load_prompts(spatial / "spatial-5k" / "prompts.json")
    if not FULL:
        prompts.cases = prompts.cases[:4]
    r = bench(Registry([spatial]), "spatial-5k", prompts, max_tokens=64, out_dir=tmp_path)
    golden = r["comparisons"]["golden"]
    assert golden["bit_exact"] is True, golden["differences"]
    assert golden["compared"]["logits"] > 0 and golden["compared"]["traces"] > 0
    m, sp = r["measurement"], r["speed"]
    assert m["trusted"] is True and m["cycles_source"] == ["testbench"] and m["host_cycles_source"] == ["testbench"]
    assert m["cycles_match_design"] is True and sp["cycles_per_token_min"] == sp["cycles_per_token_max"] == 1327
    assert sp["host_cycles_per_token"] >= 704 and sp["tok_s"] > 1000
    assert r["quality"]["errors"] == 0 and r["quality"]["comparison_errors"] == 0
    assert r["verdict"]["verdict"] == "pass", r["verdict"]["rules"]
    assert r["prompt_set"]["prompts"] == (20 if FULL else 4)


@needs_image
def test_a_hung_bridge_is_killed_with_its_container(spatial: Path):
    s = Session(Registry([spatial]).get("spatial-5k").manifest)
    try:
        name_filter = f"name=llmchip-vchip-{s.chip.proc.pid}"
        assert s.chip.proc.poll() is None
    finally:
        s.chip._kill()
    out = subprocess.run(["podman", "ps", "-a", "--filter", name_filter, "-q"], capture_output=True, text=True)
    assert out.stdout.strip() == "", "the simulator container outlived its bridge"
