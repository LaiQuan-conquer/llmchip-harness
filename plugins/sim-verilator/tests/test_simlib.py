import importlib.util
import json
import struct
import sys
from pathlib import Path

import numpy as np
from llmchip_model import Tokenizer
from llmchip_model.evalset import load_evalset

HERE = Path(__file__).parents[1]
ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "models" / "stories260K"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


simlib = load("simlib", HERE / "simlib.py")
main = load("sim_verilator_main", HERE / "__main__.py")


def test_tokenizer_port_equals_the_model_tokenizer():
    data = (FIXTURE / "tok512.bin").read_bytes()
    ours, theirs = simlib.Tokenizer(data, 512), Tokenizer(data, 512)
    prompts = load_evalset(ROOT / "plugins" / "numeric" / "data" / "evalset-tinystories.json")["prompts"]
    for text in [*prompts, "", "héllo wörld 123", "Tom and Lily."]:
        ids = ours.encode(text)
        assert ids == theirs.encode(text), text
        assert ours.decode([*ids, 42, 7]) == theirs.decode([*ids, 42, 7])


def log_record(rid, pos, token_in, token_out, nxt, groups):
    words = [0x53544550, rid, pos, token_in, token_out, nxt, 100, 0, 0, len(groups)]
    for point, layer, start, values in groups:
        words += [point, layer, len(values), start, *values]
    return struct.pack(f"<{len(words)}i", *words)


def test_log_parsing_and_l2_comparison_find_the_first_divergence():
    trace = {
        "p0.prompt": np.array([1, 5]),
        "p0.tokens": np.array([5, 9]),
        "p0.attn_residual": np.array([[[1, 2]], [[3, 4]]]),
        "p0.ffn_residual": np.array([[[5, 6]], [[7, 8]]]),
        "p0.logits": np.array([[0, 1, 2], [2, 1, 0]]),
    }
    good = log_record(0, 0, 1, 7, 5, [(1, 0, 10, [1, 2]), (2, 0, 20, [5, 6]), (3, 0, 30, [0, 1, 2])])
    good += log_record(0, 1, 5, 9, 9, [(1, 0, 10, [3, 4]), (2, 0, 20, [7, 8]), (3, 0, 30, [2, 1, 0])])
    records = simlib.parse(good)
    assert [r["pos"] for r in records] == [0, 1] and records[0]["groups"][2]["start"] == 30
    result = simlib.compare_with_l2(records, trace, 0, "p0", 1)
    assert result["mismatches"] == 0 and result["values"] == 2 * (2 + 2 + 3 + 1)
    bad = good[: -4 * 3] + struct.pack("<3i", 2, 1, 1)
    result = simlib.compare_with_l2(simlib.parse(bad), trace, 0, "p0", 1)
    assert result["first"] == {"pos": 1, "point": "logits", "layer": 0, "index": 2}
    missing = simlib.compare_with_l2(simlib.parse(good[: len(good) // 2]), trace, 0, "p0", 1)
    assert missing["mismatches"] == 1 and missing["first"]["point"] == "missing steps"
    assert simlib.tokens_in(trace, "p0") == [1, 5]


def test_qualification_vectors_saturate_both_ways():
    expected = main.qual_expected()
    accs = [acc for _, acc, _ in expected]
    assert len(expected) == 96 and 2**31 - 1 in accs and -(2**31) in accs


def test_every_item_of_the_default_plan_is_implemented():
    plan = json.loads((HERE / "data" / "verification-plan.json").read_text())
    ids = [i["id"] for i in plan["items"]]
    categories = {i["category"] for i in plan["items"]}
    # The six categories of FR-VER-04.
    assert categories == {"regression", "directed", "boundary", "exhaustive", "random", "coverage"}
    source = (HERE / "__main__.py").read_text()
    for item in ids:
        assert f'"{item}"' in source, item
    tests = (HERE / "cocotb_alu.py").read_text()
    for item in ("boundary.alu", "exhaustive.silu_index", "exhaustive.exp_index", "exhaustive.rsqrt", "random.alu"):
        assert f'"{item}"' in tests
