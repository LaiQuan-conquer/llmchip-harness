"""The test harness itself (design T-01..T-14), on the repository's test models and on toy
process chips with injected faults."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Model, Tokenizer, load_llama2c
from llmchip_vchip import cli
from llmchip_vchip.conform import conform
from llmchip_vchip.generator import generate, teacher_force
from llmchip_vchip.hosts import SpiHost, make_host
from llmchip_vchip.interface import ChipError
from llmchip_vchip.prompts import inline
from llmchip_vchip.prompts import load as load_prompts
from llmchip_vchip.registry import Registry
from llmchip_vchip.runner import Session, bench

ROOT = Path(__file__).resolve().parents[3]
STORIES = ROOT / "tests" / "fixtures" / "models" / "stories260K"
PROMPTS = inline(["Once upon a time", "Tom had a big red ball", "The cat was sad because"])


def _registry(*dirs: Path) -> Registry:
    return Registry(list(dirs))


def _session(reg: Registry, chip_id: str) -> Session:
    return Session(reg.get(chip_id).manifest)


@pytest.fixture
def chips(baseline_dir: Path, tmp_path: Path) -> Path:
    """A writable chips directory with the baseline chips in it."""
    d = tmp_path / "chips"
    shutil.copytree(baseline_dir, d)
    return d


# -- T-01 manifests and registry -----------------------------------------------------


def test_invalid_manifests_are_unavailable_with_reasons_and_do_not_block_others(chips: Path, make_process):
    (chips / "broken-json").mkdir()
    (chips / "broken-json" / "chip.json").write_text("{not json")
    make_process(chips, "no-vocab", model={"family": "toy", "context": 32})
    make_process(chips, "bad-backend", backend="fpga-wishful")
    make_process(chips, "missing-artifact", artifacts={"rtl": "rtl/none.bundle"})
    make_process(chips, "no-cycles-cap", capabilities=["cycles"])
    make_process(chips, "big-vocab", model={"family": "toy", "context": 32, "vocab": 151936})
    dup = make_process(chips, "dup-a")
    manifest = json.loads((dup / "chip.json").read_text())
    (chips / "dup-b").mkdir()
    (chips / "dup-b" / "chip.json").write_text(json.dumps(manifest))
    reg = _registry(chips)
    reasons = {e.manifest.dir.name: e.reason for e in reg.all() if not e.available}
    assert "cannot be read" in reasons["broken-json"]
    assert "model.vocab must be a positive integer" in reasons["no-vocab"]
    assert "unknown backend fpga-wishful" in reasons["bad-backend"]
    assert "artifact rtl" in reasons["missing-artifact"]
    assert "must include greedy" in reasons["no-cycles-cap"]
    assert "does not fit the host's 16-bit token field" in reasons["big-vocab"]
    assert "duplicate id dup-a" in reasons["dup-b"]
    assert {"s260k-ref", "s260k-l2", "qwen3-ref", "dup-a"} <= {e.id for e in reg.all() if e.available}
    with pytest.raises(Exception, match=r"cannot be read|vocab"):
        reg.open("no-vocab")
    assert cli.main(["--chips", str(chips), "list"]) == 0


def test_hashes_cover_manifest_tokenizer_and_artifacts(chips: Path):
    h = _registry(chips).get("s260k-l2").manifest.hashes()
    assert set(h) == {"manifest", "tokenizer", "artifact.model", "artifact.spec", "artifact.weights"}
    assert h["artifact.model"] == "b0a507e7ad0f626624f17112325e66691f9076d622e1d3274d103d00299f2696"


# -- T-02 tokenizer --------------------------------------------------------------------


def test_tokenizer_round_trip_and_unrepresentable_text(chips: Path):
    reg = _registry(chips)
    for chip_id in ("s260k-ref", "qwen3-ref"):
        tok = _session(reg, chip_id).tokenizer
        assert tok.roundtrip("Once upon a time")
    with pytest.raises(ChipError, match="unknown token") as e:
        _session(reg, "qwen3-ref").tokenizer.encode("中国的首都是")
    assert e.value.code == "invalid_input"


# -- T-03/T-04 generation ----------------------------------------------------------------


def test_reference_chip_matches_the_l0_model_validated_against_run_c(chips: Path):
    """The harness loop on the reference chip gives the tokens of llmchip_model's run.c port."""
    s = _session(_registry(chips), "s260k-ref")
    cfg, weights = load_llama2c((STORIES / "stories260K.bin").read_bytes())
    tok = Tokenizer((STORIES / "tok512.bin").read_bytes(), cfg.vocab_size)
    model = Model(cfg, weights)
    for prompt in PROMPTS.cases:
        ids = tok.encode(prompt.prompt)
        g = generate(s.host, ids, 20)
        assert g.error is None and g.stop_reason == "max_tokens"
        model.reset()
        token, expected = ids[0], []
        for pos in range(len(ids) - 1 + 20):
            nxt = int(np.argmax(model.forward(token, pos)))
            token = ids[pos + 1] if pos < len(ids) - 1 else nxt
            if pos >= len(ids) - 1:
                expected.append(nxt)
        assert g.tokens == expected


def test_prefill_and_step_agree_on_both_baselines(chips: Path):
    reg = _registry(chips)
    for chip_id in ("s260k-ref", "s260k-l2"):
        s = _session(reg, chip_id)
        ids = s.tokenizer.encode("Once upon a time")
        g = generate(s.host, ids, 10)
        forced = teacher_force(s.host, ids + g.tokens[:-1])
        assert [st.output for st in forced.steps[len(ids) - 1 :]] == g.tokens


# -- T-05/T-07 context and invalid input ---------------------------------------------


def test_context_boundary_and_invalid_input(chips: Path):
    s = _session(_registry(chips), "s260k-l2")
    ids = s.tokenizer.encode("Once upon a time")
    g = generate(s.host, ids, 1000)
    assert g.stop_reason == "context" and g.decode_steps[-1].pos == 31 and len(g.tokens) == 32 - len(ids) + 1
    over = generate(s.host, [1] * 33, 4)
    assert over.error["code"] == "context_overflow"
    assert generate(s.host, [1, 600], 4).error["code"] == "invalid_input"
    s.chip.reset()
    for token, pos in ((512, 0), (-1, 0), (5, 32), (5, -1)):
        with pytest.raises(ChipError) as e:
            s.chip.step(token, pos)
        assert e.value.code == "invalid_input"
    assert generate(s.host, ids, 1000).tokens == g.tokens  # usable after invalid inputs


def test_conformance_of_the_baseline_chips(chips: Path):
    reg = _registry(chips)
    for chip_id in ("s260k-ref", "s260k-l2", "qwen3-ref"):
        r = conform(reg, chip_id, load_prompts(reg.get(chip_id).manifest.dir / "prompts.json"), max_tokens=8)
        assert r["verdict"] == "pass", r["checks"]
        verdicts = {c["id"]: c["verdict"] for c in r["checks"]}
        assert verdicts["T-08"] == "n/a" and verdicts["T-12"] == "n/a"  # direct host, no cycles: said, not hidden


# -- T-06 stop token -------------------------------------------------------------------


def test_stop_token_ends_generation_and_is_not_a_generated_token(tmp_path: Path, make_process):
    make_process(tmp_path, "toy-stop", "--stop-at", "6", "--sticky")
    s = _session(_registry(tmp_path), "toy-stop")
    try:
        ids = s.tokenizer.encode("Once upon a time")  # 5 tokens: decode starts at position 4
        g = generate(s.host, ids, 20)
        assert g.stop_reason == "stop_token" and len(g.tokens) == 2 and 1 not in g.tokens
        assert g.decode_steps[-1].output == 1
    finally:
        s.close()


# -- T-08 host frames and T-12 speed ----------------------------------------------------


def test_spi_host_counts_frames_and_speed_follows_clock(tmp_path: Path, make_process):
    make_process(tmp_path, "toy-spi", "--cycles", "1327")
    make_process(tmp_path, "toy-spi-read", host={"kind": "spi", "read_words": 10})
    make_process(tmp_path, "toy-measured", "--host-cycles", "900")
    reg = _registry(tmp_path)
    r = bench(reg, "toy-spi", PROMPTS, max_tokens=8, out_dir=tmp_path / "runs")
    sp = r["speed"]
    assert sp["host_cycles_per_token"] == (48 + 40) * 8 == 704
    assert sp["host_cycles_source"] == ["model"]
    steps = [json.loads(line)["steps"] for line in Path(r["run_dir"], "outputs.jsonl").read_text().splitlines()]
    decode = [st for rows in steps for st in rows if st["phase"] == "decode"]
    assert all(st["cycles"] == 1327 + st["pos"] for st in decode)  # the chip's cycles, as reported
    compute = [st["cycles"] for st in decode]
    assert sp["tok_s"] == pytest.approx(8e6 / (sum(compute) / len(compute) + 704))
    assert sp["tok_s_compute_only"] == pytest.approx(8e6 / (sum(compute) / len(compute)))
    s = _session(reg, "toy-spi-read")
    try:
        assert isinstance(s.host, SpiHost) and s.host.frame_cycles() == (48 + 40 + 24 + 32 * 10) * 8
    finally:
        s.close()
    r = bench(reg, "toy-measured", PROMPTS, max_tokens=4, out_dir=tmp_path / "runs")
    assert r["speed"]["host_cycles_per_token"] == 900 and r["speed"]["host_cycles_source"] == ["chip"]


def test_conformance_catches_a_chip_claiming_fewer_host_cycles_than_its_frames(tmp_path: Path, make_process):
    make_process(tmp_path, "toy-fast-host", "--host-cycles", "100")
    make_process(tmp_path, "toy-ok")
    reg = _registry(tmp_path)
    bad = {c["id"]: c for c in conform(reg, "toy-fast-host", PROMPTS, 4)["checks"]}
    assert bad["T-08"]["verdict"] == "fail" and "704" in bad["T-08"]["detail"]
    ok = conform(reg, "toy-ok", PROMPTS, 4)
    assert ok["verdict"] == "pass", ok["checks"]
    assert {c["id"]: c["verdict"] for c in ok["checks"]}["T-12"] == "pass"


# -- T-09 determinism --------------------------------------------------------------------


def test_two_runs_are_identical(chips: Path, tmp_path: Path):
    reg = _registry(chips)
    a = bench(reg, "s260k-l2", PROMPTS, max_tokens=12, out_dir=tmp_path / "runs")
    b = bench(reg, "s260k-l2", PROMPTS, max_tokens=12, out_dir=tmp_path / "runs")
    assert a["run_id"] != b["run_id"]
    assert [s["tokens"] for s in a["samples"]] == [s["tokens"] for s in b["samples"]]
    assert a["hashes"] == b["hashes"]


# -- T-10 golden (bit-exact) and T-11 quality vs reference --------------------------------


def test_process_chip_wrapping_l2_is_bit_exact_with_the_numeric_golden(chips: Path, tmp_path: Path, make_process):
    l2 = chips / "s260k-l2"
    make_process(chips, "hw-good", "--wrap", str(l2), model={"family": "llama2c", "context": 32, "vocab": 512})
    make_process(
        chips,
        "hw-bad",
        "--wrap",
        str(l2),
        "--wrong-at",
        "9",
        "--sticky",
        model={"family": "llama2c", "context": 32, "vocab": 512},
    )
    reg = _registry(chips)
    good = bench(reg, "hw-good", PROMPTS, max_tokens=12, golden="s260k-l2", reference="s260k-ref", out_dir=tmp_path)
    assert good["comparisons"]["golden"]["bit_exact"] is True
    assert good["quality"]["next_token_agreement"] > 0.8
    bad = bench(reg, "hw-bad", PROMPTS, max_tokens=12, golden="s260k-l2", out_dir=tmp_path)
    g = bad["comparisons"]["golden"]
    assert g["bit_exact"] is False
    assert any(d.get("pos") == 9 and d["what"] == "token" for d in g["differences"])
    assert g["next_token_agreement"] < 1


def test_policy_verdicts_pass_fail_and_not_measured(chips: Path, tmp_path: Path):
    reg = _registry(chips)
    policy = {
        "reference": "s260k-ref",
        "quality": {"next_token_agreement_min": 0.9, "perplexity_max": 100.0},
        "speed": {"tok_s_min": 1000},
    }
    m = chips / "s260k-l2" / "chip.json"
    data = json.loads(m.read_text())
    m.write_text(json.dumps({**data, "policy": policy}))
    prompts = load_prompts(chips / "s260k-l2" / "prompts.json")
    prompts.cases = prompts.cases[:3]
    r = bench(_registry(chips), "s260k-l2", prompts, max_tokens=12, out_dir=tmp_path)
    rules = {x["rule"]: x for x in r["verdict"]["rules"]}
    assert rules["next_token_agreement_min"]["verdict"] == "pass"
    assert rules["perplexity_max"]["verdict"] == "pass"
    assert rules["tok_s_min"]["verdict"] == "n/a" and "cycles" in str(r["missing"])
    assert r["verdict"]["verdict"] == "incomplete"
    m.write_text(json.dumps({**data, "policy": {**policy, "quality": {"next_token_agreement_min": 1.01}, "speed": {}}}))
    r = bench(_registry(chips), "s260k-l2", prompts, max_tokens=12, out_dir=tmp_path)
    assert r["verdict"]["verdict"] == "fail"
    report = Path(r["run_dir"], "report.md").read_text()
    assert "FAIL" in report and "next_token_agreement_min" in report
    for name in ("run.json", "prompts.jsonl", "outputs.jsonl", "report.json"):
        assert Path(r["run_dir"], name).is_file()
    del reg


def test_comparison_with_a_chip_of_another_tokenizer_is_refused_visibly(chips: Path, tmp_path: Path):
    r = bench(_registry(chips), "s260k-l2", PROMPTS, max_tokens=4, reference="qwen3-ref", out_dir=tmp_path)
    assert "different tokenizer" in r["comparisons"]["reference"]["error"]
    assert r["quality"]["next_token_agreement"] is None


# -- T-13 timeouts and crashes ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("--crash-at", "crash"),
        ("--garbage-at", "crash"),
        ("--hang-at", "timeout"),
        ("--bad-token-at", "invalid_output"),
    ],
)
def test_faults_are_recorded_and_the_next_prompt_still_runs(tmp_path: Path, make_process, fault: str, code: str):
    make_process(tmp_path, "toy-fault", fault, "6", timeout_s=1)
    t0 = time.monotonic()
    r = bench(_registry(tmp_path), "toy-fault", PROMPTS, max_tokens=8, out_dir=tmp_path / "runs")
    assert time.monotonic() - t0 < 20
    first, rest = r["samples"][0], r["samples"][1:]
    assert first["error"]["code"] == code and first["stop_reason"] == "error"
    assert all(s["error"] is None and len(s["tokens"]) == 8 for s in rest)
    assert r["quality"]["errors"] == 1 and r["verdict"]["verdict"] == "fail"


def test_a_process_that_does_not_start_fails_to_open(tmp_path: Path, make_process):
    make_process(tmp_path, "toy-gone", command=["/nonexistent/simulator"])
    with pytest.raises(Exception, match=r"failed to load|No such file"):
        _session(_registry(tmp_path), "toy-gone")


# -- T-14 several models -----------------------------------------------------------------


def test_chips_of_different_models_load_and_generate(chips: Path):
    reg = _registry(chips)
    for chip_id, prompt in (("s260k-ref", "Once upon a time"), ("qwen3-ref", "The capital of France is")):
        s = _session(reg, chip_id)
        g = generate(make_host(s.chip), s.tokenizer.encode(prompt), 6)
        assert g.error is None and len(g.tokens) == 6 and all(0 <= t < s.meta.vocab for t in g.tokens)


# -- CLI ------------------------------------------------------------------------------------


def test_cli_bench_compare_conform_exit_codes(chips: Path, tmp_path: Path, capsys):
    base = ["--chips", str(chips)]
    assert cli.main([*base, "run", "s260k-l2", "--prompt", "Once upon a time", "--max-tokens", "4"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["tokens"]) == 4 and out["tok_s"] is None
    assert (
        cli.main(
            [*base, "bench", "s260k-l2", "--prompt", "Once upon a time", "--max-tokens", "4", "--out", str(tmp_path)]
        )
        == 0
    )
    assert "NO POLICY" in capsys.readouterr().out
    assert (
        cli.main(
            [
                *base,
                "compare",
                "s260k-ref",
                "s260k-l2",
                "--prompt",
                "Tom had",
                "--max-tokens",
                "4",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert "| s260k-l2 | numeric |" in capsys.readouterr().out
    assert cli.main([*base, "conform", "s260k-l2", "--prompt", "Once upon a time"]) == 0
    assert cli.main([*base, "inspect", "nope"]) == 2


# -- review findings -----------------------------------------------------------------------


def test_a_chip_declaring_cycles_but_reporting_none_gets_no_speed(tmp_path: Path, make_process):
    make_process(tmp_path, "toy-zero", "--cycles", "0")
    r = bench(_registry(tmp_path), "toy-zero", PROMPTS, max_tokens=4, out_dir=tmp_path / "runs")
    assert r["speed"].get("tok_s") is None and "reported none" in r["missing"]["cycles"]
    checks = {c["id"]: c["verdict"] for c in conform(_registry(tmp_path), "toy-zero", PROMPTS, 4)["checks"]}
    assert checks["T-12"] == "fail"


def test_conformance_without_a_usable_prompt_is_incomplete_not_pass(chips: Path):
    r = conform(_registry(chips), "s260k-l2", inline(["once upon a time " * 20]), 4)
    assert r["verdict"] == "incomplete"
    assert {c["verdict"] for c in r["checks"][1:]} == {"not_run"}


def test_reference_with_a_longer_context_is_replayed_within_the_chip_context(chips: Path, tmp_path: Path):
    from llmchip_vchip.build import new_chip

    new_chip("reference", STORIES / "stories260K.bin", "s260k-ref64", chips, context=64, progress=lambda _m: None)
    r = bench(_registry(chips), "s260k-l2", PROMPTS, max_tokens=40, reference="s260k-ref64", out_dir=tmp_path)
    ref = r["comparisons"]["reference"]
    assert ref["forced_error_count"] == 0 and ref["agreement_positions"] > 0
    assert r["quality"]["comparison_errors"] == 0


def test_timeout_kills_the_whole_process_group(tmp_path: Path, make_process):
    """A simulator started by a shell wrapper must not survive the timeout as an orphan."""
    import subprocess
    import sys

    toy = Path(__file__).parent / "chips" / "toy_chip.py"
    tag = f"--cycles 4242{time.monotonic_ns() % 1000}"
    d = make_process(tmp_path, "toy-wrapped", timeout_s=1)
    script = f"{sys.executable} {toy} {d} {tag} --hang-at 6; true"  # sh stays the parent
    manifest = json.loads((d / "chip.json").read_text())
    (d / "chip.json").write_text(json.dumps({**manifest, "command": ["sh", "-c", script]}))
    r = bench(_registry(tmp_path), "toy-wrapped", PROMPTS, max_tokens=8, out_dir=tmp_path / "runs")
    assert r["samples"][0]["error"]["code"] == "timeout"
    time.sleep(0.2)
    left = subprocess.run(["pgrep", "-f", tag.split()[1]], capture_output=True, text=True).stdout.split()
    assert left == [], f"orphaned simulator processes: {left}"
