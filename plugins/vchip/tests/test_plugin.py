"""The `vchip` plugin's handlers on the artifacts of a project, without the kernel: chips built
from the artifacts, reports without verdicts, metrics for the flow's checks, refusals.

The RTL parts need the pinned EDA image and are skipped, visibly, without it.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import load_llama2c, save_ir
from llmchip_sdk import Input
from llmchip_vchip.bridges.verilator import image_present
from llmchip_vchip.build import _sim_image, chips_from_artifacts, new_rtl_chips
from llmchip_vchip.manifest import file_hash
from llmchip_vchip.prompts import inline
from llmchip_vchip.registry import Registry
from llmchip_vchip.runner import bench as run_bench
from llmchip_vchip_plugin.__main__ import bench, conform

HAVE_IMAGE = image_present(_sim_image())
needs_image = pytest.mark.skipif(not HAVE_IMAGE, reason=f"EDA image {_sim_image()} not present (podman pull it)")


class Ctx:
    """The part of the SDK's InvokeContext the handlers use."""

    def __init__(self, tmp: Path, inputs: dict[str, Path], params: dict | None = None) -> None:
        self.inputs = [Input(name, "0" * 64, "t", "public", str(p)) for name, p in inputs.items()]
        self.params = params or {}
        self.out_dir = str(tmp / "out")
        self.work_dir = str(tmp / "work")
        Path(self.out_dir).mkdir(parents=True)
        Path(self.work_dir).mkdir(parents=True)

    def input(self, name: str) -> Input:
        return next(i for i in self.inputs if i.name == name)

    def progress(self, *_args) -> None:
        pass

    def check_cancelled(self) -> None:
        pass


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> dict[str, Path]:
    """The artifacts of a project of the 5K proxy up to S4: tokenizer, evaluation set, Model IR,
    numeric spec and weights, the taalas-spatial/1 bundle (made the way `vchip new rtl` does)."""
    d = tmp_path_factory.mktemp("project")
    new_rtl_chips("p", d, progress=lambda _m: None)
    src = d / "p"
    cfg, w = load_llama2c((src / "model" / "proxy.bin").read_bytes())
    (d / "model.npz").write_bytes(save_ir(cfg, w))
    # The kernel hands inputs over as files without a suffix.
    (d / "1-evalset").write_bytes((src / "prompts.json").read_bytes())
    return {
        "tokenizer": src / "model" / "tokenizer.bin",
        "evalset": d / "1-evalset",
        "spec": src / "numeric-spec.json",
        "weights": src / "l2-weights.npz",
        "rtl": src / "rtl.tar",
        "ir": d / "model.npz",
        "chips": d,
    }


def _l2_inputs(p: dict[str, Path], *extra: str) -> dict[str, Path]:
    return {k: p[k] for k in ("tokenizer", "spec", "weights", *extra)}


def test_bench_without_rtl_runs_l2_against_the_reference_of_the_model_ir(project, tmp_path):
    ctx = Ctx(tmp_path, _l2_inputs(project, "ir", "evalset"), {"prompts": ["tom had a", "the dog"], "maxTokens": 8})
    r = bench(ctx)
    m = r.metrics
    assert m["chip_backend"] == "numeric" and m["prompts"] == 2 and m["max_tokens"] == 8 and m["errors"] == 0
    assert m["golden_compared"] is False and "bit_exact" not in m  # L2 is the golden itself
    assert 0 <= m["next_token_agreement"] <= 1 and m["measurement_trusted"] is False
    assert "tok_s" not in m and "design_cycles_per_token" not in m  # not measured: absent, not null
    assert {o.type for o in r.outputs} == {"vchip.report", "vchip.outputs"}
    report = json.loads((Path(ctx.out_dir) / "report.json").read_text())
    assert "verdict" not in report and "policy" not in report  # the flow's checks judge (AP-7)
    assert report["built"]["chips"] == {"golden": "chip-l2", "reference": "chip-ref"}
    rows = (Path(ctx.out_dir) / "outputs.jsonl").read_text().splitlines()
    assert [json.loads(row)["prompt"] for row in rows] == ["tom had a", "the dog"]


def test_the_reference_from_the_model_ir_equals_the_reference_of_the_checkpoint(project, tmp_path):
    """The plugin writes the Model IR back as a llama2.c checkpoint: the same float model."""
    ctx = Ctx(tmp_path, _l2_inputs(project, "ir"), {"prompts": ["once upon a time"], "maxTokens": 12})
    bench(ctx)
    plugin_ref = Registry([Path(ctx.work_dir) / "chips"])
    ours = run_bench(plugin_ref, "chip-ref", inline(["once upon a time"]), max_tokens=12, out_dir=tmp_path / "a")
    theirs = run_bench(
        Registry([project["chips"]]), "p-ref", inline(["once upon a time"]), max_tokens=12, out_dir=tmp_path / "b"
    )
    assert ours["samples"][0]["tokens"] == theirs["samples"][0]["tokens"]


def test_bench_needs_prompts_and_takes_the_evaluation_set(project, tmp_path):
    with pytest.raises(ValueError, match="evaluation set"):
        bench(Ctx(tmp_path / "a", _l2_inputs(project), {"maxTokens": 4}))
    r = bench(Ctx(tmp_path / "b", _l2_inputs(project, "evalset"), {"maxPrompts": 3, "maxTokens": 4}))
    assert r.metrics["prompts"] == 3


def test_an_rtl_bundle_of_another_spec_or_architecture_is_refused(project, tmp_path):
    with np.load(project["weights"]) as z:
        arrays = {k: z[k] for k in z.files}
    other = tmp_path / "other.npz"
    np.savez(other, **arrays, extra=np.zeros(1))
    inputs = {**_l2_inputs(project), "weights": other, "rtl": project["rtl"]}
    with pytest.raises(ValueError, match="another numeric spec or other weights"):
        bench(Ctx(tmp_path / "a", inputs, {"prompts": ["tom"]}))
    vector = tmp_path / "vector.tar"
    with tarfile.open(vector, "w") as tar:
        body = json.dumps({"architecture": "taalas/1"}).encode()
        info = tarfile.TarInfo("bundle/design.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    with pytest.raises(ValueError, match="only taalas-spatial/1"):
        bench(Ctx(tmp_path / "b", {**_l2_inputs(project), "rtl": vector}, {"prompts": ["tom"]}))


def _chip_spec(p: dict[str, Path], path: Path, **change) -> Path:
    """The fields of an S3 chip spec that bind it to a design (arch-taalas `design` writes them)."""
    chip = {
        "format": "llmchip-chip-spec/1",
        "architecture": "taalas-spatial/1",
        "numericSpec": {"hash": file_hash(p["spec"])},
        "weights": {"hash": file_hash(p["weights"])},
        "dataflow": {"dividers": 16},
        "performance": {"cyclesPerToken": 1327, "clockMHz": 10.0},
    }
    path.write_text(json.dumps({**chip, **change}))
    return path


def test_the_design_figures_come_from_the_chip_spec_of_exactly_this_design(project, tmp_path):
    """vchip.l3 accepts only the chip spec's cycles and clock: a run cannot choose its own clock."""
    args = {k: project[k] for k in ("tokenizer", "evalset", "spec", "weights", "rtl")}
    chip = _chip_spec(project, tmp_path / "chip.json")
    built = chips_from_artifacts("a", tmp_path / "a", **args, chip_spec=chip)
    assert built["sources"] == {"cycles": "chip.spec", "clock": "chip.spec"} and built["clock_mhz"] == 10.0
    built = chips_from_artifacts("b", tmp_path / "b", **args, chip_spec=chip, clock_mhz=1000)
    assert built["sources"]["clock"] == "params"  # vchip.l3 is then indeterminate
    assert chips_from_artifacts("c", tmp_path / "c", **args)["sources"] == {"cycles": "rtl.bundle", "clock": "default"}
    for name, change, problem in (
        ("weights", {"weights": {"hash": "0" * 64}}, "not designed from this numeric spec"),
        ("unbound", {"numericSpec": {}}, "not designed from this numeric spec"),
        ("dividers", {"dataflow": {"dividers": 8}}, "another design than the RTL bundle"),
        ("arch", {"architecture": "taalas/1"}, "another design than the RTL bundle"),
    ):
        chip = _chip_spec(project, tmp_path / f"{name}.json", **change)
        with pytest.raises(ValueError, match=problem):
            chips_from_artifacts(name, tmp_path / name, **args, chip_spec=chip)


def test_conformance_of_l2_reports_counts_not_a_verdict(project, tmp_path):
    ctx = Ctx(tmp_path, _l2_inputs(project, "evalset"), {"maxPrompts": 2, "maxTokens": 4})
    r = conform(ctx)
    m = r.metrics
    assert m["chip_backend"] == "numeric" and m["checks_failed"] == 0 and m["checks_not_run"] == 0
    assert m["prompts"] == 2 and m["clock_source"] == "none" and "acceptance_tok_s_min" not in m
    assert m["checks_passed"] + m["checks_not_applicable"] == m["checks"]
    report = json.loads((Path(ctx.out_dir) / "conformance.json").read_text())
    assert "verdict" not in report and {c["id"] for c in report["checks"]} >= {"T-01", "T-09", "T-18"}


@needs_image
def test_bench_of_the_rtl_is_bit_exact_with_l2_and_measured_by_the_testbench(project, tmp_path):
    ctx = Ctx(tmp_path, _l2_inputs(project, "rtl", "ir"), {"prompts": ["tom had a", "the cat"], "maxTokens": 10})
    m = bench(ctx).metrics
    assert m["chip_backend"] == "rtl" and m["golden_compared"] is True and m["bit_exact"] is True
    assert m["measurement_trusted"] is True and m["cycles_match_design"] is True
    assert m["design_cycles_per_token"] == m["cycles_per_token_min"] == m["cycles_per_token_max"] == 1327
    assert m["design_cycles_source"] == "rtl.bundle" and m["clock_source"] == "default" and m["clock_mhz"] == 8
    assert m["tok_s"] > 1000 and m["errors"] == 0 and m["comparison_errors"] == 0


@needs_image
def test_quick_conformance_of_the_rtl_leaves_the_acceptance_run_not_run(project, tmp_path):
    ctx = Ctx(tmp_path, _l2_inputs(project, "rtl", "evalset"), {"quick": True, "maxPrompts": 2, "maxTokens": 4})
    m = conform(ctx).metrics
    assert m["chip_backend"] == "rtl" and m["checks_failed"] == 0, m
    assert m["checks_not_run"] == 2  # T-18, T-19: the check `vchip.conformance` is then indeterminate
    # The standard of the acceptance part, for the check to hold it to L3 (not the plugin's word).
    assert m["acceptance_tok_s_min"] == 1000 and m["acceptance_bit_exact"] is True
    assert m["acceptance_max_tokens"] == 64 and m["clock_source"] == "default"
