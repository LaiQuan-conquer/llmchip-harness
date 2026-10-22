"""`vchip`: the command line of the test harness.

    vchip list                                   chips found, and why unavailable ones are
    vchip inspect CHIP                           manifest, hashes, policy
    vchip run CHIP --prompt TEXT                 one generation: tokens, text, cycles
    vchip bench CHIP [--prompts FILE]            prompt set -> run directory with report
    vchip compare CHIP CHIP... [--prompts FILE]  the same prompts on several chips
    vchip conform CHIP                           interface conformance (T-01..T-09)
    vchip new reference|numeric --model PATH --id ID   baseline chips from a model

Chips are looked up in --chips DIR (repeatable), else $LLMCHIP_VCHIP_CHIPS, else
<LLMCHIP_HOME or ~/.llmchip>/vchip/chips. Exit status: 0 when the verdict is pass (or no
policy), 1 when it is fail or incomplete, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import prompts as pr
from .build import new_chip, new_rtl_chips
from .conform import conform
from .generator import generate
from .interface import ChipError
from .manifest import ManifestError
from .registry import Registry, default_dirs
from .runner import Session, bench, default_runs_dir


def _registry(args) -> Registry:
    return Registry([Path(d) for d in args.chips] if args.chips else None)


def _prompt_set(args, registry: Registry, chip_id: str) -> pr.PromptSet:
    if getattr(args, "prompt", None):
        return pr.inline(args.prompt)
    if getattr(args, "prompts", None):
        return pr.load(Path(args.prompts))
    m = registry.get(chip_id).manifest
    if "prompts" in m.data:
        return pr.load(m.path(m.data["prompts"]))
    raise SystemExit(f"chip {chip_id} declares no prompt set; give --prompts FILE or --prompt TEXT")


def cmd_list(args) -> int:
    reg = _registry(args)
    if not reg.all():
        print(f"no chips in {', '.join(str(d) for d in reg.dirs)}")
    for e in reg.all():
        d = e.manifest.data
        if e.available:
            m = d["model"]
            model = f"{m['family']:10} ctx {m['context']:<6} vocab {m['vocab']:<7}"
            print(f"{e.id:30} {d['backend']:10} {model} {e.manifest.dir}")
        else:
            print(f"{e.id:30} unavailable: {e.reason}  ({e.manifest.dir})")
    return 0


def cmd_inspect(args) -> int:
    e = _registry(args).get(args.chip)
    out = {
        "dir": str(e.manifest.dir),
        "available": e.available,
        "problems": e.manifest.problems,
        "manifest": e.manifest.data,
        "policy": e.manifest.policy() if e.available else None,
        "hashes": e.manifest.hashes() if e.available else None,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    reg = _registry(args)
    s = Session(reg.get(args.chip).manifest)
    try:
        ids = s.tokenizer.encode(args.prompt)
        g = generate(s.host, ids, args.max_tokens)
    finally:
        s.close()
    hz = s.meta.clock_mhz * 1e6 if s.meta.clock_mhz else None
    cycles = [st.cycles for st in g.decode_steps]
    host = [st.host_cycles for st in g.decode_steps]
    out = {
        "chip": s.meta.id,
        "prompt": args.prompt,
        "prompt_tokens": ids,
        "tokens": g.tokens,
        "text": s.tokenizer.continuation(ids, g.tokens),
        "stop_reason": g.stop_reason,
        "error": g.error,
        "cycles_per_token": cycles if "cycles" in s.meta.capabilities else None,
        "host_cycles_per_token": host if s.meta.host["kind"] != "direct" else None,
        "tok_s": hz * len(cycles) / sum(c + h for c, h in zip(cycles, host, strict=True))
        if hz and cycles and "cycles" in s.meta.capabilities and sum(cycles)
        else None,
        "simulated": True,
        "wall_s": g.wall_s,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 1 if g.error else 0


def _exit(verdict: str) -> int:
    return 0 if verdict in ("pass", "no_policy") else 1


def cmd_bench(args) -> int:
    reg = _registry(args)
    r = bench(
        reg,
        args.chip,
        _prompt_set(args, reg, args.chip),
        max_tokens=args.max_tokens,
        reference=args.reference,
        golden=args.golden,
        out_dir=Path(args.out) if args.out else None,
        traces=args.traces,
    )
    print(Path(r["run_dir"], "report.md").read_text())
    print(f"run directory: {r['run_dir']}")
    return _exit(r["verdict"]["verdict"])


def cmd_compare(args) -> int:
    reg = _registry(args)
    ps = _prompt_set(args, reg, args.chip[0])
    base = args.chip[0]
    rows, worst = [], "pass"
    for chip in args.chip:
        r = bench(
            reg,
            chip,
            ps,
            max_tokens=args.max_tokens,
            reference=None if chip == base else base,
            out_dir=Path(args.out) if args.out else None,
        )
        q, sp = r["quality"], r["speed"]
        rows.append(
            (
                chip,
                r["chip"]["backend"],
                q.get("next_token_agreement"),
                q.get("sequence_match"),
                q.get("perplexity"),
                sp.get("tok_s"),
                q["errors"],
                r["run_dir"],
            )
        )
        if _exit(r["verdict"]["verdict"]):
            worst = "fail"

    def f(v):
        return "—" if v is None else f"{v:.4g}" if isinstance(v, float) else str(v)

    print(f"| Chip | Backend | Agreement with {base} | Identical generations | Perplexity | tok/s | Errors | Run |")
    print("|---|---|---|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(f(v) for v in row) + " |")
    return _exit(worst)


def cmd_conform(args) -> int:
    reg = _registry(args)
    r = conform(reg, args.chip, _prompt_set(args, reg, args.chip), args.max_tokens, quick=args.quick)
    for c in r["checks"]:
        print(f"{c['id']:5} {c['verdict']:5} {c['title']}{' — ' + c['detail'] if c['detail'] else ''}")
    print(f"conformance of {args.chip}: {r['verdict']}")
    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=2, ensure_ascii=False))
    return _exit(r["verdict"])


def cmd_new(args) -> int:
    chips_dir = Path(args.chips[0]) if args.chips else default_dirs()[0]
    if args.backend == "rtl":
        if not args.model and not args.proxy:
            raise ValueError("give --model (a llama2.c checkpoint) or --proxy")
        made = new_rtl_chips(
            args.id,
            chips_dir,
            model=Path(args.model) if args.model else None,
            architecture=args.architecture,
            weight_format=args.weight_format or "ternary",
            group_size=args.group_size or 16,
            context=args.context or 64,
            dividers=args.dividers,
            clock_mhz=args.clock_mhz,
            progress=lambda m: print(m, file=sys.stderr),
        )
        for d in made:
            print(d)
        return 0
    if not args.model:
        raise ValueError("give --model")
    out = new_chip(
        args.backend,
        Path(args.model),
        args.id,
        chips_dir,
        context=args.context,
        weight_format=args.weight_format or "int8",
        group_size=args.group_size or 32,
        progress=lambda m: print(m, file=sys.stderr),
    )
    print(out)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vchip", description="Test harness for virtual chips")
    p.add_argument("--chips", action="append", metavar="DIR", help="directory of chips (repeatable)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list chips").set_defaults(fn=cmd_list)
    s = sub.add_parser("inspect", help="show a chip's manifest, hashes and policy")
    s.add_argument("chip")
    s.set_defaults(fn=cmd_inspect)
    s = sub.add_parser("run", help="one generation from one prompt")
    s.add_argument("chip")
    s.add_argument("--prompt", required=True)
    s.add_argument("--max-tokens", type=int, default=64)
    s.set_defaults(fn=cmd_run)
    for name, fn, help_ in (
        ("bench", cmd_bench, "run a prompt set, write a report"),
        ("compare", cmd_compare, "run a prompt set on several chips"),
        ("conform", cmd_conform, "check a chip against the interface"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("chip", nargs="+" if name == "compare" else None)
        s.add_argument("--prompts", help="JSONL prompt set or llmchip-evalset/1 JSON")
        s.add_argument("--prompt", action="append", help="a prompt (repeatable), instead of --prompts")
        s.add_argument("--max-tokens", type=int, default=16 if name == "conform" else None)
        if name == "conform":
            s.add_argument("--out", help="write the result as JSON to this file")
            s.add_argument("--quick", action="store_true", help="skip the acceptance run (T-18, T-19: not_run)")
        else:
            s.add_argument("--out", help=f"directory for run directories (default {default_runs_dir()})")
        if name == "bench":
            s.add_argument("--reference", help="chip to measure agreement with (default: the policy's)")
            s.add_argument("--golden", help="chip to compare bit for bit with (default: the policy's)")
            s.add_argument("--traces", action="store_true", help="keep observation points in outputs.jsonl")
        s.set_defaults(fn=fn)
    s = sub.add_parser("new", help="make a reference, numeric or rtl chip from a model")
    s.add_argument("backend", choices=["reference", "numeric", "rtl"])
    s.add_argument("--model", help="model directory or llama2.c checkpoint")
    s.add_argument("--proxy", action="store_true", help="rtl: make the 5K proxy instead of --model")
    s.add_argument("--id", required=True)
    s.add_argument("--context", type=int)
    s.add_argument("--weight-format", help="int8 (reference/numeric), ternary (rtl) by default")
    s.add_argument("--group-size", type=int, help="32 (numeric), 16 (rtl) by default")
    s.add_argument("--architecture", default="taalas-spatial/1", help="rtl: the engine to generate")
    s.add_argument("--dividers", type=int, default=16, help="rtl: softmax dividers of taalas-spatial/1")
    s.add_argument("--clock-mhz", type=float, default=8, help="rtl: the clock tok/s is computed at")
    s.set_defaults(fn=cmd_new)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except (KeyError, ValueError, ManifestError, ChipError) as e:
        print(f"vchip: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
