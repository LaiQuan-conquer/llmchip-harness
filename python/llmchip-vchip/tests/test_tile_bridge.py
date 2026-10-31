"""The tile pin-level bridge: the tiny tile chip driven over its parallel host pins, token by
token, against GraphL2 (B6). The vchip `TileSim` will speak this testbench's protocol."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import generator

TOOLS = Path("/root/projects/deepseek-harness/tools")
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TB_DIR = Path(__file__).resolve().parents[1] / "src/llmchip_vchip/bridges/tb"
TINY_QWEN3 = Path(__file__).parents[3] / "tests/fixtures/models/tiny-qwen3"
PROMPT, STEPS = 3, 8


def _l2():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [[3, 9, 17, 25]])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    return graph_l2.GraphL2(graph, spec, arrays)


def _greedy(l2, prompt: int, steps: int):
    ids = [prompt]
    out = []
    for _ in range(steps):
        nxt = int(np.argmax(l2.forward(ids)[-1]))
        ids.append(nxt)
        out.append(nxt)
    return out


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def test_the_tile_pin_bridge_generates_tokens(tmp_path):
    l2 = _l2()
    generator.generate(l2.spec, l2.a, macs=8, out=tmp_path)
    for name in ("tile_top.v", "tile_tb.cpp"):
        shutil.copyfile(TB_DIR / name, tmp_path / name)
    files = sorted(str(p) for p in (tmp_path / "rtl").glob("*.v"))
    build = subprocess.run(
        [
            str(VERILATOR),
            "--cc",
            "--exe",
            "--build",
            "-Wno-fatal",
            "--top-module",
            "tb_top",
            "tile_top.v",
            "tile_tb.cpp",
            *files,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (build.stdout + build.stderr)[-4000:]
    proc = subprocess.Popen(
        [str(tmp_path / "obj_dir" / "Vtb_top")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=tmp_path
    )
    proc.stdin.write("RESET\n")
    proc.stdin.flush()
    assert proc.stdout.readline().strip() == "OK"
    got = []
    for _ in range(STEPS):
        proc.stdin.write(f"STEP {got[-1] if got else PROMPT} {len(got)}\n")
        proc.stdin.flush()
        line = proc.stdout.readline().split()
        assert line[0] == "STEP" and "TIMEOUT" not in line, line
        got.append(int(line[1]))
    proc.stdin.write("QUIT\n")
    proc.stdin.flush()
    proc.wait(timeout=30)
    assert got == _greedy(l2, PROMPT, STEPS), got


def _image() -> str:
    import json

    manifest = json.loads((Path(__file__).parents[3] / "plugins/sim-verilator/llmchip-plugin.json").read_text())
    return manifest["entry"]["image"]


def _bundle_tar(tmp_path):
    import tarfile

    l2 = _l2()
    root = tmp_path / "bundle-root"
    generator.generate(l2.spec, l2.a, macs=8, out=root)
    tar = tmp_path / "tile.tar"
    with tarfile.open(tar, "w") as archive:
        archive.add(root, arcname="bundle")
    return tar


def test_the_tile_bundle_builds_in_the_simulator_image(tmp_path):
    from llmchip_vchip.bridges import verilator

    if not verilator.image_present(_image()):
        pytest.skip("the simulator image is not present")
    out = verilator.build_tile(_bundle_tar(tmp_path), _image(), tmp_path / "cache")
    assert (out / "bundle/rtl/obj_tile/Vtile").is_file()


def test_tilesim_steps_the_tiny_chip(tmp_path):
    import shutil

    from llmchip_vchip.bridges import verilator
    from llmchip_vchip.bridges.tile_bridge import TileSim
    from llmchip_vchip.manifest import load

    if not verilator.image_present(_image()):
        pytest.skip("the simulator image is not present")
    l2 = _l2()
    tar = _bundle_tar(tmp_path)
    build = verilator.build_tile(tar, _image(), tmp_path / "cache")
    chip = tmp_path / "chip"
    chip.mkdir()
    shutil.copy(tar, chip / "tile.tar")
    (chip / "numeric-spec.json").write_text(json.dumps(l2.spec))
    shutil.copy(TINY_QWEN3 / "tokenizer.json", chip / "tokenizer.json")
    (chip / "chip.json").write_text(
        json.dumps(
            {
                "format": "llmchip-vchip/1",
                "id": "tile-tiny",
                "backend": "rtl",
                "model": {
                    "family": "qwen3",
                    "params": 1000,
                    "dim": 32,
                    "layers": 2,
                    "heads": 4,
                    "kv_heads": 2,
                    "context": 16,
                    "vocab": 128,
                },
                "tokenizer": {"kind": "hf", "path": "tokenizer.json"},
                "artifacts": {"rtl": "tile.tar", "spec": "numeric-spec.json"},
                "clock_mhz": 100,
                "capabilities": ["greedy", "cycles"],
                "host": {"kind": "direct"},
                "measurement": {
                    "cycles": "testbench",
                    "host_cycles": "testbench",
                    "trusted": True,
                    "testbench": "tile pin-level testbench",
                },
                "simulator": {"image": _image(), "tool": "verilator"},
                "policy": {},
            }
        )
    )
    manifest = load(chip)
    assert not manifest.problems, manifest.problems
    sim = TileSim(chip, build)
    try:
        sim.reset()
        got = []
        for pos in range(8):
            got.append(sim.step(got[-1] if got else PROMPT, pos).token)
    finally:
        sim.close()
    assert got == _greedy(l2, PROMPT, 8), got


def test_the_rtl_adapter_dispatches_a_tile_bundle(tmp_path):
    import shutil

    from llmchip_vchip.adapters.rtl import RtlChip
    from llmchip_vchip.bridges import verilator
    from llmchip_vchip.manifest import load

    if not verilator.image_present(_image()):
        pytest.skip("the simulator image is not present")
    l2 = _l2()
    tar = _bundle_tar(tmp_path)
    chip = tmp_path / "chip"
    chip.mkdir()
    shutil.copy(tar, chip / "tile.tar")
    (chip / "numeric-spec.json").write_text(json.dumps(l2.spec))
    shutil.copy(TINY_QWEN3 / "tokenizer.json", chip / "tokenizer.json")
    (chip / "chip.json").write_text(
        json.dumps(
            {
                "format": "llmchip-vchip/1",
                "id": "tile-tiny",
                "backend": "rtl",
                "model": {
                    "family": "qwen3",
                    "params": 1000,
                    "dim": 32,
                    "layers": 2,
                    "heads": 4,
                    "kv_heads": 2,
                    "context": 16,
                    "vocab": 128,
                },
                "tokenizer": {"kind": "hf", "path": "tokenizer.json"},
                "artifacts": {"rtl": "tile.tar", "spec": "numeric-spec.json"},
                "clock_mhz": 100,
                "capabilities": ["greedy", "cycles"],
                "host": {"kind": "direct"},
                "measurement": {
                    "cycles": "testbench",
                    "host_cycles": "testbench",
                    "trusted": True,
                    "testbench": "tile pin-level testbench",
                },
                "simulator": {"image": _image(), "tool": "verilator"},
                "policy": {},
            }
        )
    )
    manifest = load(chip)
    assert not manifest.problems, manifest.problems
    sim = RtlChip(manifest)
    try:
        sim.reset()
        got = []
        for pos in range(4):
            got.append(sim.step(got[-1] if got else PROMPT, pos).token)
    finally:
        sim.close()
    assert got == _greedy(l2, PROMPT, 4), got


def test_the_graph_golden_answers_greedy_tokens(tmp_path):
    from llmchip_vchip.adapters.graph import GraphChip
    from llmchip_vchip.manifest import load

    l2 = _l2()
    (tmp_path / "graph.json").write_text(json.dumps(l2.graph))
    (tmp_path / "spec.json").write_text(json.dumps(l2.spec))
    np.savez(tmp_path / "weights.npz", meta=np.array(json.dumps({"format": "llmchip-graph-weights/1"})), **l2.a)
    chip = tmp_path / "chip"
    chip.mkdir()
    shutil = __import__("shutil")
    for name in ("graph.json", "spec.json", "weights.npz"):
        shutil.copy(tmp_path / name, chip / name)
    shutil.copy(TINY_QWEN3 / "tokenizer.json", chip / "tokenizer.json")
    (chip / "chip.json").write_text(
        json.dumps(
            {
                "format": "llmchip-vchip/1",
                "id": "graph-tiny",
                "backend": "graph-numeric",
                "model": {
                    "family": "qwen3",
                    "params": 1000,
                    "dim": 32,
                    "layers": 2,
                    "heads": 4,
                    "kv_heads": 2,
                    "context": 16,
                    "vocab": 128,
                },
                "tokenizer": {"kind": "hf", "path": "tokenizer.json"},
                "artifacts": {"graph": "graph.json", "spec": "spec.json", "weights": "weights.npz"},
                "capabilities": ["greedy"],
                "host": {"kind": "direct"},
                "measurement": {"cycles": "model", "host_cycles": "model"},
                "policy": {},
            }
        )
    )
    manifest = load(chip)
    assert not manifest.problems, manifest.problems
    golden = GraphChip(manifest)
    got = []
    for pos in range(8):
        got.append(golden.step(got[-1] if got else PROMPT, pos).token)
    assert got == _greedy(l2, PROMPT, 8), got


def test_graph_chips_build_a_dut_and_its_golden(tmp_path):

    from llmchip_vchip.adapters.graph import GraphChip
    from llmchip_vchip.adapters.rtl import RtlChip
    from llmchip_vchip.bridges import verilator
    from llmchip_vchip.build import graph_chips
    from llmchip_vchip.manifest import load

    if not verilator.image_present(_image()):
        pytest.skip("the simulator image is not present")
    l2 = _l2()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(l2.graph))
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(l2.spec))
    weights = tmp_path / "weights.npz"
    np.savez(weights, meta=np.array(json.dumps({"format": "llmchip-graph-weights/1"})), **l2.a)
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text(json.dumps({"id": "p0", "prompt": "hello"}) + "\n")
    tar = _bundle_tar(tmp_path)
    built = graph_chips(
        "tiny",
        tmp_path / "chips",
        graph=graph,
        spec=spec,
        weights=weights,
        tokenizer=TINY_QWEN3 / "tokenizer.json",
        prompts=prompts,
        rtl=tar,
        clock_mhz=100.0,
        image=_image(),
    )
    assert built["chips"] == {"golden": "tiny-golden", "dut": "tiny"}
    for name in ("tiny-golden", "tiny"):
        assert not load(tmp_path / "chips" / name).problems, load(tmp_path / "chips" / name).problems
    golden = GraphChip(load(tmp_path / "chips" / "tiny-golden"))
    dut = RtlChip(load(tmp_path / "chips" / "tiny"))
    try:
        golden.reset()
        dut.reset()
        got_golden, got_dut = [], []
        for pos in range(4):
            t = got_golden[-1] if got_golden else PROMPT
            got_golden.append(golden.step(t, pos).token)
            got_dut.append(dut.step(t, pos).token)
    finally:
        dut.close()
    assert got_dut == got_golden, (got_dut, got_golden)
