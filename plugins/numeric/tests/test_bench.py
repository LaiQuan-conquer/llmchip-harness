"""Model quality by open benchmarks: the comparison of L0 and L2, and the L2 answering lm-eval.

- `compare` pairs two reports question by question: drop and paired-bootstrap interval in
  percentage points, the questions only one model answered correctly; it refuses reports of
  different questions, sets or data.
- `L2LM` answers lm-eval's requests from the L2: log-likelihoods equal the host log-softmax of
  the reference L2's integer logits, and greedy generation equals the reference's greedy
  continuation, stopping at an end-of-sequence token or a stop string.
- `check_against_reference` (evidence of `spec.l2.fast`) finds no mismatch between the fast
  and the reference L2, and reports one when the fast L2 rounds differently (negative control).
"""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import bench, graph_l2

torch = pytest.importorskip("torch")
pytest.importorskip("lm_eval")
from llmchip_numeric.benchlm import L2LM, greedy_only, l0_model, log_softmax  # noqa: E402
from llmchip_numeric.l2_torch import TorchL2, check_against_reference  # noqa: E402

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


def report(layer: str, correct: dict[str, list[bool]], **head) -> dict:
    return {
        "format": bench.REPORT_FORMAT,
        "layer": layer,
        "set": "quick",
        "maxLength": 2048,
        "data": "d",
        **head,
        "benchmarks": {
            b: {"items": [{"id": f"{b}/{i}", "correct": c} for i, c in enumerate(cs)]} for b, cs in correct.items()
        },
    }


def test_compare_gives_the_drop_and_its_paired_interval():
    l0 = report("L0", {"mmlu": [True] * 60 + [False] * 40, "gsm8k": [True, True, False, False, False]})
    l2 = report("L2", {"mmlu": [True] * 55 + [False] * 45, "gsm8k": [True, False, True, False, False]})
    comparison, metrics = bench.compare(l0, l2)
    assert metrics["benchmarks"] == 2 and metrics["set"] == "quick"
    assert metrics["drop.mmlu"] == pytest.approx(5.0)
    # Five questions lost, none gained: the drop is at least 0 in every resample.
    assert metrics["drop_low.mmlu"] >= 0 and metrics["drop_high.mmlu"] > 5
    assert metrics["l0_only.mmlu"] == 5 and metrics["l2_only.mmlu"] == 0
    assert metrics["drop.gsm8k"] == 0 and metrics["drop_low.gsm8k"] < -3 < 3 < metrics["drop_high.gsm8k"]
    assert comparison["benchmarks"]["gsm8k"]["l0OnlyCorrect"] == ["gsm8k/1"]
    assert comparison["benchmarks"]["gsm8k"]["l2OnlyCorrect"] == ["gsm8k/2"]
    # The same interval every time (fixed seed).
    assert bench.compare(l0, l2)[1] == metrics


def test_compare_refuses_reports_that_do_not_match():
    l0 = report("L0", {"mmlu": [True, False]})
    with pytest.raises(ValueError, match="different questions"):
        bench.compare(l0, report("L2", {"mmlu": [True, False, True]}))
    with pytest.raises(ValueError, match="set"):
        bench.compare(l0, report("L2", {"mmlu": [True, False]}, set="full"))
    with pytest.raises(ValueError, match="data"):
        bench.compare(l0, report("L2", {"mmlu": [True, False]}, data="other"))
    with pytest.raises(ValueError, match="L0 report and an L2 report"):
        bench.compare(l0, report("L0", {"mmlu": [True, False]}))
    with pytest.raises(ValueError, match="different models"):
        bench.compare(
            report("L0", {"mmlu": [True]}, model={"model": "a"}), report("L2", {"mmlu": [True]}, model={"model": "b"})
        )
    settled = report("L2", {"mmlu": [True, False]})
    settled["benchmarks"]["mmlu"]["settings"] = {"mmlu_x": {"nShot": 5}}
    with pytest.raises(ValueError, match="different settings"):
        bench.compare(l0, settled)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """The tiny Qwen3 fixture with a tokenizer configuration (what lm-eval loads), and its L2."""
    d = tmp_path_factory.mktemp("tiny-qwen3")
    for f in TINY_QWEN3.iterdir():
        shutil.copy(f, d / f.name)
    (d / "tokenizer_config.json").write_text(
        json.dumps(
            {"tokenizer_class": "PreTrainedTokenizerFast", "eos_token": "<|endoftext|>", "pad_token": "<|endoftext|>"}
        )
    )
    path = d / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((d / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57, 65, 73]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int8", group_size=16, stats=stats, context=64)
    return d, graph_l2.GraphL2(graph, spec, arrays)


def test_l2_lm_answers_from_the_l2(tiny):
    directory, ref = tiny
    lm = L2LM(str(directory), TorchL2(ref), 64, 4)
    # Log-likelihoods: (context, continuation) pairs, two sharing a context (one pass for both).
    requests = [
        (("a", "b"), [5, 9, 13], [20]),
        (("a", "c"), [5, 9, 13], [21]),
        (("x", "yz"), [7, 8], [30, 31]),
    ]
    got = lm._loglikelihood_tokens(requests)
    fl = 2.0 ** -ref.spec["logits"]["frac"]
    for (_, context, cont), (ll, greedy) in zip(requests, got, strict=True):
        full = context + cont
        lp = log_softmax(ref.forward(full[:-1]) * fl)[len(context) - 1 :]
        assert ll == pytest.approx(float(sum(lp[i, t] for i, t in enumerate(cont))), abs=1e-9)
        assert greedy == all(int(np.argmax(lp[i])) == t for i, t in enumerate(cont))
    # Greedy generation: as the reference, token by token, for prompts of different lengths.
    prompts = [[5, 9, 13], [7, 8, 9, 10, 11]]
    context = torch.zeros((2, 5), dtype=torch.long)
    out = lm.generate_tokens(context, prompts, 6, stop=[])
    for b, prompt in enumerate(prompts):
        seq = list(prompt)
        for _ in range(6):
            nxt = int(np.argmax(ref.forward(seq)[-1]))
            seq.append(nxt)
            if nxt in lm.eos_ids:
                break
        generated = seq[len(prompt) :]
        assert out[b, 5 : 5 + len(generated)].tolist() == generated
    # A stop string ends the generation of that sequence.
    first = lm.tokenizer.decode(out[0, 5:6].tolist(), skip_special_tokens=False)
    stopped = lm.generate_tokens(context, prompts, 6, stop=[first])
    assert (stopped[0, 6:] == lm.tokenizer.pad_token_id).all()


def test_the_fast_check_finds_no_mismatch_and_reports_an_injected_one(tiny):
    _, ref = tiny
    prompts = [[5, 9, 13], [7, 8, 9, 10, 11], [40]]
    good = check_against_reference(ref, TorchL2(ref), prompts, 5)
    assert good["metrics"] | {"device": "-"} == {
        "prompts": 3,
        "steps": 5,
        "positions_compared": 15,
        "observation_vectors_compared": good["metrics"]["observation_vectors_compared"],
        "logit_mismatches": 0,
        "point_mismatches": 0,
        "batch_mismatches": 0,
        "device": "-",
    }
    # Negative control: a fast L2 that truncates where the spec rounds half up.
    wrong = TorchL2(ref)
    wrong.rounding = "truncate"
    bad = check_against_reference(ref, wrong, prompts, 5)
    assert bad["metrics"]["logit_mismatches"] > 0 and bad["metrics"]["point_mismatches"] > 0
    assert bad["metrics"]["batch_mismatches"] > 0


def test_greedy_only_replaces_the_models_generation_settings(tiny):
    directory, ref = tiny
    lm = L2LM(str(directory), TorchL2(ref), 64, 4)
    lm.model.generation_config.repetition_penalty = 1.1
    lm.model.generation_config.do_sample = True
    greedy_only(lm)
    config = lm.model.generation_config
    assert config.repetition_penalty != 1.1 and config.do_sample is not True
    l0 = l0_model(str(directory), 64, 4, "cpu")
    assert l0.model.generation_config.repetition_penalty != 1.1 and l0.model.generation_config.do_sample is not True


class WrongCounts(TorchL2):
    """A fast L2 whose logits are right and whose overflow counters are not."""

    @property
    def saturations(self):
        return {"score": 999}


def test_the_fast_check_compares_the_overflow_counters(tiny):
    _, ref = tiny
    prompts = [[5, 9, 13], [7, 8, 9, 10, 11]]
    bad = check_against_reference(ref, WrongCounts(ref), prompts, 5)
    assert bad["metrics"]["logit_mismatches"] == 0
    assert bad["metrics"]["point_mismatches"] > 0 and bad["metrics"]["batch_mismatches"] > 0


def test_compare_refuses_other_windows_and_tool_versions():
    base = {"mmlu": [True, False] * 10}
    l0 = report("L0", base)
    with pytest.raises(ValueError, match="maxLength"):
        bench.compare(l0, report("L2", base, maxLength=4096))
    with pytest.raises(ValueError, match="versions"):
        bench.compare(report("L0", base, versions={"torch": "1"}), report("L2", base, versions={"torch": "2"}))
    same = bench.compare(report("L0", base, versions={"torch": "1"}), report("L2", base, versions={"torch": "1"}))
    assert same[1]["drop.mmlu"] == 0
