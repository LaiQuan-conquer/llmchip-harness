"""Run one B5 prompt on the tile chip and compare every emitted token to the golden."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path("/root/projects/deepseek-harness/llmchip-harness")
sys.path.insert(0, str(REPO / "python/llmchip-vchip/src"))
TOOLS = Path("/root/projects/deepseek-harness/tools")
MODEL = TOOLS / "models/Qwen2.5-0.5B-Instruct"
RUN = Path("/root/llmchip-runs/inc001")
def find_build() -> Path:
    import hashlib

    for q in sorted(Path("/tmp/qwen_build_gated").glob("*/"), key=lambda q: (q / "build.json").stat().st_mtime if (q / "build.json").is_file() else 0, reverse=True):
        ident = q / "build.json"
        binary = q / "bundle/rtl/obj_tile/Vtile"
        if not ident.is_file() or not binary.is_file():
            continue
        data = json.loads(ident.read_text())
        if data.get("binary") == hashlib.sha256(binary.read_bytes()).hexdigest():
            return q
    raise SystemExit("no build whose binary matches its build.json; rebuild it")


BUILD = find_build()
IMAGE = json.loads((REPO / "plugins/sim-verilator/llmchip-plugin.json").read_text())["entry"]["image"]


def chip_dir() -> Path:
    chip = RUN / "chip"
    chip.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": "llmchip-vchip/1", "id": "qwen-tile", "backend": "rtl",
        "model": {"family": "qwen2.5", "params": 500000000, "dim": 896, "layers": 24,
                  "heads": 14, "kv_heads": 2, "context": 512, "vocab": 151936},
        "tokenizer": {"kind": "hf", "path": str(MODEL / "tokenizer.json")},
        "artifacts": {"rtl": str(BUILD / "bundle/rtl/tile_chip.v")},
        "capabilities": ["greedy"], "host": {"kind": "direct"},
        "measurement": {"cycles": "testbench", "host_cycles": "testbench", "trusted": True},
        "simulator": {"image": IMAGE}, "policy": {},
    }
    (chip / "chip.json").write_text(json.dumps(manifest, indent=2))
    return chip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--tokens", type=int, default=2)
    ap.add_argument("--prompt-tokens", type=int, default=0)
    args = ap.parse_args()
    from llmchip_vchip.bridges.tile_bridge import TileSim

    prompts = json.loads((RUN / "prompts.json").read_text())["prompts"]
    golden = {g["id"]: g for g in json.loads((RUN / "golden.json").read_text())["golden"]}
    prompt = prompts[args.index]
    want = golden[prompt["id"]]["expected"][: args.tokens]
    sim = TileSim(chip_dir(), BUILD)
    try:
        sim.reset()
        ids = list(prompt["tokens"])[: args.prompt_tokens or None]
        out = []
        for pos, tok in enumerate(ids):
            out = [sim.step(tok, pos).token]
        got = []
        nxt = out[-1]
        for k in range(args.tokens):
            r = sim.step(nxt, len(ids) + k)
            got.append(r.token)
            nxt = r.token
    finally:
        sim.close()
    ok = got == want
    shard = {"id": prompt["id"], "tokens": prompt["tokens"], "expected": want, "got": got,
             "pass": ok, "build": json.loads((BUILD / "build.json").read_text()),
             "prepare": json.loads((RUN / "prepare-identity.json").read_text())}
    out_dir = RUN / "shards"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{prompt['id']}.json").write_text(json.dumps(shard, indent=2))
    print("PASS" if ok else f"FAIL expected {want} got {got}", flush=True)


if __name__ == "__main__":
    main()
