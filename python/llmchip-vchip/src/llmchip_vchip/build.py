"""`vchip new`: baseline chips made from a model on disk, to compare hardware chips with.

- `reference`: the float model (quality baseline);
- `numeric`: the L2 integer model of a graph numeric spec built here (calibrated on the
  bundled evaluation set of the tokenizer's kind), the bit-exact golden of a hardware chip
  built from the same spec.

The model's files are referenced by absolute path, not copied; their hashes are recorded
with every run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
from llmchip_model.tokens import TextTokenizer
from llmchip_numeric import graph_eval, graph_l2

from .adapters import model_source
from .manifest import FORMAT, file_hash


def _base(src: model_source.ModelSource, chip_id: str, backend: str, context: int) -> dict:
    if src.tokenizer is None:
        raise ValueError(f"no tokenizer (tokenizer.json or a llama2.c tokenizer .bin) beside {src.path}")
    kind = "hf" if src.tokenizer.suffix == ".json" else "llama2c"
    return {
        "format": FORMAT,
        "id": chip_id,
        "backend": backend,
        "description": f"{backend} chip of {src.path.name}, made by `vchip new`",
        "model": {
            "family": src.family,
            "params": src.params,
            "context": context,
            "vocab": src.vocab,
            "source": str(src.path),
        },
        "tokenizer": {"kind": kind, "path": str(src.tokenizer.resolve()), "stop": src.stop_tokens()},
        "artifacts": {"model": str(src.path.resolve())},
        "clock_mhz": None,
        "host": {"kind": "direct"},
        "capabilities": ["greedy", "logits"],
        "prompts": "prompts.json",
    }


def new_chip(
    backend: str,
    model: Path,
    chip_id: str,
    chips_dir: Path,
    *,
    context: int | None = None,
    weight_format: str = "int8",
    group_size: int = 32,
    progress=print,
) -> Path:
    model = Path(model).expanduser()
    src = model_source.load(model, context or 256)
    context = context or min(src.max_positions or 256, 256)
    if src.max_positions and context > src.max_positions:
        raise ValueError(f"context {context} exceeds the model's {src.max_positions} positions")
    chip = _base(src, chip_id, backend, context)
    out = chips_dir / chip_id
    if out.exists():
        raise ValueError(f"{out} exists")
    out.mkdir(parents=True)
    try:
        kind = chip["tokenizer"]["kind"]
        evalset = graph_eval.default_evalset(kind)
        (out / "prompts.json").write_text(json.dumps(evalset, indent=2, ensure_ascii=False))
        if backend == "numeric":
            tok = TextTokenizer(src.tokenizer, kind, src.vocab)
            progress("calibrating the float graph on the bundled evaluation set")
            stats = graph_l2.calibrate(
                src.graph, src.weights.get, graph_eval.calibration_sequences(evalset, tok, context)
            )
            progress(f"quantizing ({weight_format}, groups of {group_size})")
            spec, arrays = graph_l2.build(
                src.graph,
                src.weights.peek,
                weight_format=weight_format,
                group_size=group_size,
                context=context,
                per_layer=True,
                residual_bits=24,
                stats=stats,
            )
            (out / "numeric-spec.json").write_text(json.dumps(spec, indent=2))
            np.savez(
                out / "l2-weights.npz", meta=np.array(json.dumps({"format": graph_l2.GRAPH_WEIGHTS_FORMAT})), **arrays
            )
            chip["artifacts"] |= {"spec": "numeric-spec.json", "weights": "l2-weights.npz"}
            chip["description"] += f" ({weight_format}, groups of {group_size})"
        elif backend != "reference":
            raise ValueError(f"`vchip new` makes reference and numeric chips, not {backend}")
        (out / "chip.json").write_text(json.dumps(chip, indent=2, ensure_ascii=False) + "\n")
    except BaseException:
        shutil.rmtree(out)
        raise
    return out


# The 5K proxy of the reference model (the shape of tests/flow-helpers.ts PROXY): untrained,
# fixed seed, character tokenizer. Its outputs mean nothing; it proves the hardware path.
PROXY = {"dim": 16, "hidden_dim": 32, "n_layers": 2, "n_heads": 2, "n_kv_heads": 1, "seq_len": 64, "seed": 1}
SPATIAL = "taalas-spatial/1"


def _sim_image() -> str:
    """The EDA image pinned by the sim-verilator plugin of this repository."""
    manifest = Path(__file__).resolve().parents[4] / "plugins" / "sim-verilator" / "llmchip-plugin.json"
    return json.loads(manifest.read_text())["entry"]["image"]


def _proxy(out: Path) -> tuple[Path, Path, dict]:
    import llmchip_model
    from llmchip_model import Config
    from llmchip_model.evalset import load_evalset
    from llmchip_model.proxy import DEFAULT_CHARS, char_tokenizer, random_checkpoint

    p = PROXY
    cfg = Config(
        dim=p["dim"],
        hidden_dim=p["hidden_dim"],
        n_layers=p["n_layers"],
        n_heads=p["n_heads"],
        n_kv_heads=p["n_kv_heads"],
        vocab_size=len(DEFAULT_CHARS) + 3,
        seq_len=p["seq_len"],
    )
    out.mkdir(parents=True)
    (out / "proxy.bin").write_bytes(random_checkpoint(cfg, p["seed"]))
    (out / "tokenizer.bin").write_bytes(char_tokenizer(DEFAULT_CHARS))
    evalset = load_evalset(Path(llmchip_model.__file__).parents[2] / "data" / "evalset-proxy.json")
    return out / "proxy.bin", out / "tokenizer.bin", evalset


def new_rtl_chips(
    chip_id: str,
    chips_dir: Path,
    *,
    model: Path | None = None,
    architecture: str = SPATIAL,
    weight_format: str = "ternary",
    group_size: int = 16,
    context: int = 64,
    dividers: int = 16,
    clock_mhz: float = 8,
    image: str | None = None,
    progress=print,
) -> list[Path]:
    """Three chips of one model and one numeric spec: `<id>-ref` (float reference), `<id>-l2`
    (the L2 golden) and `<id>` (the RTL of `architecture`, judged against both by its policy).
    Without `model`, the 5K proxy is made. The numeric spec is the llama2.c one of S2, the one
    the RTL generators consume."""
    from llmchip_model import Tokenizer, load_llama2c
    from llmchip_numeric.l2 import L2Model
    from llmchip_numeric.spec import build as build_spec
    from llmchip_rtl.bundle import write_tar
    from llmchip_rtl.spatial.bundle import bundle_files
    from llmchip_rtl.spatial.generator import Spatial

    if architecture != SPATIAL:
        raise ValueError(f"`vchip new rtl` builds {SPATIAL} only (taalas-rom/1 has no RTL generator yet)")
    targets = [chips_dir / chip_id, chips_dir / f"{chip_id}-l2", chips_dir / f"{chip_id}-ref"]
    if any(t.exists() for t in targets):
        raise ValueError(f"one of {', '.join(str(t) for t in targets)} exists")
    rtl_dir = targets[0]
    rtl_dir.mkdir(parents=True)
    try:
        if model is None:
            progress("making the 5K proxy (untrained, fixed seed)")
            weights_path, tok_path, evalset = _proxy(rtl_dir / "model")
            proxy = True
        else:
            weights_path = Path(model).expanduser().resolve()
            tok_path = next((p for p in weights_path.parent.glob("tok*.bin")), None)
            if tok_path is None:
                raise ValueError(f"no llama2.c tokenizer (tok*.bin) beside {weights_path}")
            evalset = graph_eval.default_evalset("llama2c")
            proxy = False
        cfg, weights = load_llama2c(weights_path.read_bytes())
        tok = Tokenizer(tok_path.read_bytes(), cfg.vocab_size)
        progress(f"quantizing ({weight_format}, groups of {group_size}, context {context})")
        spec, npz = build_spec(
            cfg,
            weights,
            tok,
            evalset.get("calibration") or evalset["prompts"],
            weight_format=weight_format,
            group_size=group_size,
            context=context,
        )
        (rtl_dir / "numeric-spec.json").write_text(json.dumps(spec, indent=2))
        (rtl_dir / "l2-weights.npz").write_bytes(npz)
        l2 = L2Model.load(rtl_dir / "numeric-spec.json", rtl_dir / "l2-weights.npz")
        progress(f"generating the {SPATIAL} circuits ({dividers} dividers)")
        sp = Spatial(l2.spec, l2.a, dividers=dividers)
        source = {
            "numericSpec": file_hash(rtl_dir / "numeric-spec.json"),
            "weights": file_hash(rtl_dir / "l2-weights.npz"),
        }
        write_tar(bundle_files(sp, source), rtl_dir / "rtl.tar")
        cycles = sp.cycles_per_token()
        (rtl_dir / "prompts.json").write_text(json.dumps(evalset, indent=2, ensure_ascii=False))
        params = sum(v.size for k, v in weights.items() if k != "wcls")
        _write_chip_set(
            chip_id,
            targets,
            model_block=_model_block(cfg, int(params), context, proxy),
            tokenizer=tok_path,
            prompts=rtl_dir / "prompts.json",
            reference_model=weights_path,
            spec=rtl_dir / "numeric-spec.json",
            weights=rtl_dir / "l2-weights.npz",
            rtl="rtl.tar",
            cycles=cycles,
            clock_mhz=clock_mhz,
            image=image,
            description=f"{weights_path.name} ({weight_format}, groups of {group_size})",
        )
    except BaseException:
        for t in targets:
            shutil.rmtree(t, ignore_errors=True)
        raise
    return targets


def _model_block(cfg, params: int, context: int, proxy: bool) -> dict:
    return {
        "family": "llama2c",
        "params": params,
        "dim": cfg.dim,
        "layers": cfg.n_layers,
        "heads": cfg.n_heads,
        "kv_heads": cfg.n_kv_heads,
        "context": context,
        "vocab": cfg.vocab_size,
        "proxy": proxy,
    }


def _write_chip_set(
    chip_id: str,
    targets: list[Path],
    *,
    model_block: dict,
    tokenizer: Path,
    prompts: Path,
    reference_model: Path | None,
    spec: Path,
    weights: Path,
    rtl: str | None,
    cycles: int | None,
    clock_mhz: float,
    image: str | None,
    description: str,
) -> None:
    """The manifests of `<id>` (the RTL under the pin-level testbench, judged against the other
    two by its policy), `<id>-l2` (the L2 golden) and `<id>-ref` (the float reference). The RTL
    chip is left out without `rtl`, the reference without `reference_model`."""
    rtl_dir, l2_dir, ref_dir = targets
    base = {
        "format": FORMAT,
        "tokenizer": {"kind": "llama2c", "path": str(tokenizer.resolve()), "stop": [1]},
        "prompts": str(prompts.resolve()),
    }
    chips: dict[Path, dict] = {
        l2_dir: {
            **base,
            "id": f"{chip_id}-l2",
            "backend": "numeric",
            "description": f"L2 golden of {description}",
            "model": model_block,
            "artifacts": {"spec": str(spec.resolve()), "weights": str(weights.resolve())},
            "clock_mhz": None,
            "host": {"kind": "direct"},
            "capabilities": ["greedy", "logits", "traces"],
        }
    }
    if reference_model is not None:
        chips[ref_dir] = {
            **base,
            "id": f"{chip_id}-ref",
            "backend": "reference",
            "description": f"float reference of {reference_model.name}",
            "model": model_block,
            "artifacts": {"model": str(reference_model.resolve())},
            "clock_mhz": None,
            "host": {"kind": "direct"},
            "capabilities": ["greedy", "logits"],
        }
    if rtl is not None:
        chips[rtl_dir] = {
            **base,
            "id": chip_id,
            "backend": "rtl",
            "description": f"{SPATIAL} RTL of {description}, under the pin-level testbench",
            "model": {**model_block, "architecture": SPATIAL, "cycles_per_token": cycles},
            # Files inside the chip directory stay relative, so that the directory can move.
            "artifacts": {"rtl": rtl, "spec": _inside(spec, rtl_dir)},
            "clock_mhz": clock_mhz,
            "host": {"kind": "spi", "sck_div": 8, "read_words": 0},
            "capabilities": ["greedy", "cycles", "logits", "traces"],
            "simulator": {"image": image or _sim_image(), "tool": "verilator"},
            "measurement": {
                "cycles": "testbench",
                "host_cycles": "testbench",
                "trusted": True,
                "testbench": "llmchip-vchip pin-level testbench on Verilator",
            },
            "timeout_s": 120,
            "policy": {
                "max_tokens": 64,
                "golden": f"{chip_id}-l2",
                **({"reference": f"{chip_id}-ref"} if reference_model is not None else {}),
                "quality": {"bit_exact": True},
                "speed": {"tok_s_min": 1000, "measurement_trusted": True, "cycles_match_design": True},
            },
        }
    for d, manifest in chips.items():
        d.mkdir(parents=True, exist_ok=True)
        (d / "chip.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def _inside(path: Path, directory: Path) -> str:
    return path.name if path.resolve().parent == directory.resolve() else str(path.resolve())


def chips_from_artifacts(
    chip_id: str,
    chips_dir: Path,
    *,
    tokenizer: Path,
    evalset: Path,
    spec: Path,
    weights: Path,
    rtl: Path | None = None,
    ir: Path | None = None,
    chip_spec: Path | None = None,
    clock_mhz: float | None = None,
    image: str | None = None,
) -> dict:
    """The chips of a project's artifacts, as the kernel plugin `vchip` builds them: the L2
    golden from the numeric spec and its weights (S2), the float reference from the Model IR
    (S1) when given, the RTL chip from an `rtl.bundle` (S4) when given. The bundle and the chip
    spec must come from exactly this spec and these weights. The design's cycles and clock are
    the chip spec's (S3) when given, else the bundle's cycles and `clock_mhz` (or 8 MHz).
    Returns what was built and where each figure came from: a check that needs the design's
    figures accepts only `chip.spec` as their source."""
    import tarfile

    from llmchip_model import load_ir, save_llama2c
    from llmchip_numeric.spec import SPEC_FORMAT

    data = json.loads(spec.read_text())
    if data.get("format") != SPEC_FORMAT:
        raise ValueError(
            f"the numeric spec is {data.get('format')!r}; the plugin builds chips of an {SPEC_FORMAT} spec (S2)"
        )
    cfg = data["model"]["config"]
    targets = [chips_dir / chip_id, chips_dir / f"{chip_id}-l2", chips_dir / f"{chip_id}-ref"]
    chips_dir.mkdir(parents=True, exist_ok=True)
    built: dict = {"golden": f"{chip_id}-l2"}
    cycles = None
    sources: dict = {}
    design: dict = {}
    if rtl is not None:
        with tarfile.open(rtl) as tar:
            member = tar.extractfile("bundle/design.json")
            design = json.loads(member.read()) if member else {}
        if design.get("architecture") != SPATIAL:
            raise ValueError(
                f"the RTL bundle is {design.get('architecture')!r}; only {SPATIAL} runs under the "
                "pin-level testbench (the vector engine needs its external memory)"
            )
        source = design.get("source", {})
        if source.get("numericSpec") != file_hash(spec) or source.get("weights") != file_hash(weights):
            raise ValueError("the RTL bundle was generated from another numeric spec or other weights")
        cycles, sources["cycles"] = int(design["params"]["CYCLES_PER_TOKEN"]), "rtl.bundle"
        built["dut"] = chip_id
    clock, sources["clock"] = (clock_mhz, "params") if clock_mhz is not None else (8.0, "default")
    if chip_spec is not None:
        chip = json.loads(chip_spec.read_text())
        perf = chip.get("performance", {})
        designed_from = (chip.get("numericSpec", {}).get("hash"), chip.get("weights", {}).get("hash"))
        if designed_from != (file_hash(spec), file_hash(weights)):
            raise ValueError("the chip spec was not designed from this numeric spec and these weights")
        if rtl is not None and (
            chip.get("architecture") != design["architecture"]
            or chip.get("dataflow", {}).get("dividers") != design["params"].get("DIVIDERS")
        ):
            raise ValueError("the chip spec describes another design than the RTL bundle (architecture or dividers)")
        if rtl is not None and perf.get("cyclesPerToken") is not None:
            cycles, sources["cycles"] = int(perf["cyclesPerToken"]), "chip.spec"
        if clock_mhz is None and perf.get("clockMHz") is not None:
            clock, sources["clock"] = float(perf["clockMHz"]), "chip.spec"
    reference = None
    if ir is not None:
        ir_cfg, ir_weights = load_ir(ir)
        reference = chips_dir / "model" / "model.bin"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(save_llama2c(ir_cfg, ir_weights))
        built["reference"] = f"{chip_id}-ref"
    d, h, kv, layers = cfg["dim"], cfg["hidden_dim"], cfg["dim"] * cfg["n_kv_heads"] // cfg["n_heads"], cfg["n_layers"]
    vocab_rows = cfg["vocab_size"] * d * (1 if cfg.get("shared_classifier", True) else 2)
    params = vocab_rows + layers * (2 * d + 2 * d * d + 2 * kv * d + 3 * h * d) + d
    block = {
        "family": "llama2c",
        "params": params,
        "dim": cfg["dim"],
        "layers": cfg["n_layers"],
        "heads": cfg["n_heads"],
        "kv_heads": cfg["n_kv_heads"],
        "context": int(data["context"]["maxTokens"]),
        "vocab": cfg["vocab_size"],
    }
    _write_chip_set(
        chip_id,
        targets,
        model_block=block,
        tokenizer=tokenizer,
        prompts=evalset,
        reference_model=reference,
        spec=spec,
        weights=weights,
        rtl=str(rtl.resolve()) if rtl is not None else None,
        cycles=cycles,
        clock_mhz=clock,
        image=image,
        description="the project's numeric spec",
    )
    return {
        "chips": built,
        "cycles_per_token": cycles,
        "clock_mhz": clock if rtl is not None else None,
        "sources": sources,
    }
