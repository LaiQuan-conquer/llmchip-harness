"""The Q-full chain of taalas-tile/1 (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md §12.1 B1/B3).

One `tile_layer` per decoder layer. Layer i+1's `start` is layer i's `done`, so the residual
buffer that carries `x2` from one layer to the next is also the handshake: a layer begins on the
cycle the previous one's last residual is registered. The module is generated because every layer
has its own ROM banks and norm weights; the phase schedule lives in the `tile_layer` template.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from . import rom
from .attention import att_scale_of

NODES = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")
KIND_OUT = {"q_proj": "q", "k_proj": "k", "v_proj": "v", "o_proj": "x",
            "gate_proj": "h1", "up_proj": "h3", "down_proj": "x"}
KIND_IN = {"q_proj": "xn", "k_proj": "xn", "v_proj": "xn", "o_proj": "v",
           "gate_proj": "xn", "up_proj": "xn", "down_proj": "g"}
EPS = 1e-6


def _pack(values: np.ndarray, bits: int = 16) -> str:
    mask = (1 << bits) - 1
    word = 0
    for lane, value in enumerate(np.asarray(values).ravel()):
        word |= (int(value) & mask) << (bits * lane)
    return f"{word:0{len(np.asarray(values).ravel()) * (bits // 4)}x}"


def _split(out: Path, key: str, words: list[int], macs: int, bits: int) -> int:
    addr_bits = max(2, (len(words) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    width = macs * bits // 4
    for bank in range(4):
        chunk = words[bank * per : (bank + 1) * per]
        chunk += [0] * (per - len(chunk))
        (out / f"{key}_bank{bank}.hex").write_text("\n".join(f"{w:0{width}x}" for w in chunk) + "\n")
    return addr_bits


def generate(out: Path, spec: dict, arrays: dict[str, np.ndarray], macs: int, layers: list[int] | None = None) -> dict:
    h = spec["hyper"]
    bits = int(spec["weights"]["storageBits"])
    res = int(spec["activations"]["bits"]["x"])
    frac = spec["activations"]["frac"]
    layer_frac = spec["activations"].get("layerFrac") or {}
    context = int(spec["context"]["maxTokens"])
    log2context = (context - 1).bit_length()
    group = int(spec["weights"]["groupSize"])
    cpg = group // macs
    if cpg < 1 or group % macs:
        raise ValueError(f"group {group} is not a multiple of {macs} MACs")
    chosen = layers if layers is not None else list(range(int(h["layers"])))
    for layer in chosen:
        if f"{layer}.q_norm.w" not in arrays:
            raise ValueError("tile_chain needs a model with QK norm (q_norm/k_norm); Qwen2.5 has none yet")
    rope_addr = (int(np.asarray(arrays["lut.rope_cos"]).size) - 1).bit_length()
    exp_addr = (int(np.asarray(arrays["lut.exp"]).size) - 1).bit_length()

    def wfile(name: str, values: np.ndarray, width_bits: int = 16) -> str:
        (out / name).write_text("\n".join(f"{int(v) & ((1 << width_bits) - 1):0{width_bits // 4}x}" for v in np.asarray(values).ravel()) + "\n")
        return name

    def wpack(name: str, values: np.ndarray, width_bits: int = 16) -> str:
        mask = (1 << width_bits) - 1
        word = 0
        flat = np.asarray(values).ravel()
        for lane, value in enumerate(flat):
            word |= (int(value) & mask) << (width_bits * lane)
        (out / name).write_text(f"{word:0{len(flat) * (width_bits // 4)}x}\n")
        return name

    if (out / "exp.hex").is_file():
        (out / "lut.exp.hex").write_text((out / "exp.hex").read_text())

    instances = []
    decls = []
    for index, layer in enumerate(chosen):
        fr = layer_frac.get(str(layer), frac)
        files: dict[str, str] = {}
        for node in NODES:
            key = f"{layer}.{node}"
            q = np.asarray(arrays[f"{key}.q"]).astype(np.int64)
            words = rom.to_words(q, macs, bits)
            _split(out, key, words, macs, bits)
            for bank in range(4):
                files[f"{node}_B{bank}"] = f"{key}_bank{bank}.hex"
            files[f"{node}_M"] = wfile(f"{key}_m.hex", arrays[f"{key}.m"], 16)
            b = arrays.get(f"{key}.b")
            b = np.zeros(q.shape[0], dtype=np.int64) if b is None else np.asarray(b).astype(np.int64)
            bias_bits = 32 if res > 16 and KIND_OUT[node] == "x" else 16
            files[f"{node}_BIAS"] = wfile(f"{key}_b.hex", b, bias_bits)
        files["attn_w"] = wpack(f"{layer}.attn_w.hex", arrays[f"{layer}.attn_norm.w"], 16)
        files["mlp_w"] = wpack(f"{layer}.mlp_w.hex", arrays[f"{layer}.mlp_norm.w"], 16)
        files["qnorm_w"] = wpack(f"{layer}.qnorm_w.hex", arrays[f"{layer}.q_norm.w"], 16)
        files["knorm_w"] = wpack(f"{layer}.knorm_w.hex", arrays[f"{layer}.k_norm.w"], 16)
        params = {
            "HIDDEN": int(h["hidden"]), "INTER": int(h["intermediate"]), "HEADS": int(h["heads"]),
            "KVHEADS": int(h["kvHeads"]), "HD": int(h["headDim"]), "LOG2CONTEXT": log2context,
            "MACS": macs, "BITS": bits, "ACC": 32, "GROUPS": 8, "CPG": cpg, "RES": res,
            "FRAC_H": int(fr["xn"]), "FRAC_QK": int(fr["qn"]), "NORM_FN": int(spec["normWeights"]["frac"]),
            "EPS_ATTN": round(EPS * 2.0 ** (2 * int(fr["x"]))),
            "EPS_MLP": round(EPS * 2.0 ** (2 * int(fr["x"]))),
            "EPS_HEAD": round(EPS * 2.0 ** (2 * int(fr["q"]))),
            "ROPE_ADDR": rope_addr, "EXP_ADDR": exp_addr,
            "ATTN_SCALE": att_scale_of(int(h["headDim"])),
            "SILU_F1": int(fr["h1"]), "SILU_F3": int(fr["h3"]), "SILU_FG": int(fr["g"]),
        }
        for node in NODES:
            key = f"{layer}.{node}"
            params[f"{node[:1].upper() if node != 'gate_proj' else 'G'}_FI"] = int(fr[KIND_IN[node]])
        # explicit, unambiguous parameter text
        lines = [f"  tile_layer #("]
        items = [f".{k}({v})" for k, v in params.items() if k not in ("Q_FI", "K_FI", "V_FI", "O_FI", "G_FI", "U_FI", "D_FI")]
        for start in range(0, len(items), 6):
            lines.append("    " + ", ".join(items[start:start + 6]) + ",")
        for node, tag in (("q_proj", "Q"), ("k_proj", "K"), ("v_proj", "V"), ("o_proj", "O"),
                          ("gate_proj", "G"), ("up_proj", "U"), ("down_proj", "D")):
            key = f"{layer}.{node}"
            lines.append(f"    .{tag}_FI({int(fr[KIND_IN[node]])}), .{tag}_FO({int(fr[KIND_OUT[node]])}), .{tag}_EXP({int(spec['weights']['exponents'][key])}),")
            lines.append(f"    .{tag}_B0(\"{files[node + '_B0']}\"), .{tag}_B1(\"{files[node + '_B1']}\"),")
            lines.append(f"    .{tag}_B2(\"{files[node + '_B2']}\"), .{tag}_B3(\"{files[node + '_B3']}\"),")
            lines.append(f"    .{tag}_M(\"{files[node + '_M']}\"), .{tag}_BIAS(\"{files[node + '_BIAS']}\"),")
        lines.append(f"    .ATTN_W_INIT(\"{files['attn_w']}\"), .MLP_W_INIT(\"{files['mlp_w']}\"),")
        lines.append(f"    .QNORM_W_INIT(\"{files['qnorm_w']}\"), .KNORM_W_INIT(\"{files['knorm_w']}\"),")
        lines.append(f"    .EXP_INIT(\"lut.exp.hex\"), .RSQRT_INIT(\"rsqrt.hex\"),")
        lines.append(f"    .ROPE_COS_INIT(\"rope_cos.hex\"), .ROPE_SIN_INIT(\"rope_sin.hex\"), .SIGMOID_INIT(\"sigmoid.hex\")")
        lines.append(f"  ) layer{index} (")
        lines.append(f"    .clk(clk), .rst_n(rst_n), .start(start_w[{index}]), .position(position),")
        lines.append(f"    .x(x_w[{index}*HIDDEN*RES +: HIDDEN*RES]), .x2(x_w[{index + 1}*HIDDEN*RES +: HIDDEN*RES]), .done(done_w[{index}]), .busy(busy_w[{index}]),")
        d = f"l{index}"
        lines.append(f"    .obs_h({d}_h), .obs_x1({d}_x1), .obs_h2({d}_h2), .obs_o({d}_o), .obs_f({d}_f),")
        lines.append(f"    .obs_q({d}_q), .obs_qn({d}_qn), .obs_qr({d}_qr), .obs_a({d}_a),")
        lines.append(f"    .obs_k({d}_k), .obs_v({d}_v), .obs_kn({d}_kn), .obs_kr({d}_kr),")
        lines.append(f"    .obs_g({d}_g), .obs_u({d}_u), .obs_m({d}_m), .obs_raw_sum({d}_raw), .obs_raw_valid({d}_rawv)")
        decls.append("\n".join([
            f"  wire signed [HIDDEN*16-1:0] {d}_h, {d}_h2, {d}_o, {d}_f;",
            f"  wire signed [HIDDEN*RES-1:0] {d}_x1;",
            f"  wire signed [HEADS*HD*16-1:0] {d}_q, {d}_qn, {d}_qr, {d}_a;",
            f"  wire signed [KVHEADS*HD*16-1:0] {d}_k, {d}_v, {d}_kn, {d}_kr;",
            f"  wire signed [INTER*16-1:0] {d}_g, {d}_u, {d}_m;",
            f"  wire [63:0] {d}_raw;",
            f"  wire {d}_rawv;",
            f"  wire [15:0] {d}_sig = {{|{d}_h, |{d}_x1, |{d}_h2, |{d}_o, |{d}_f, |{d}_q, |{d}_qn, |{d}_qr,",
            f"                         |{d}_a, |{d}_k, |{d}_v, |{d}_kn, |{d}_kr, |{d}_g, |{d}_u, |{d}_m}};",
        ]))
        lines.append("  );")
        instances.append("\n".join(lines))

    n = len(chosen)

    def wrap(terms: list[str], sep: str, per: int) -> str:
        groups = [sep.join(terms[i:i + per]) for i in range(0, len(terms), per)]
        pad = "\n                 " + sep.strip()
        return pad.join(groups)

    sig_assign = wrap([f"l{i}_sig" for i in range(n)], ", ", 8)
    sum_assign = wrap([f"l{i}_raw" for i in range(n)], " ^ ", 6)
    valid_assign = wrap([f"l{i}_rawv" for i in range(n)], " | ", 8)
    header = f"""// The Q-full taalas-tile/1 chain: one tile_layer per decoder layer, chained by the residual
// handshake (layer i+1 starts on layer i's done). Generated by llmchip_rtl.tile.chain.
`default_nettype none

module tile_chain #(
  parameter integer HIDDEN = {int(h['hidden'])},
  parameter integer RES = {res},
  parameter integer LAYERS = {n},
  localparam integer INTER = {int(h['intermediate'])},
  localparam integer HEADS = {int(h['heads'])},
  localparam integer KVHEADS = {int(h['kvHeads'])},
  localparam integer HD = {int(h['headDim'])}
) (
  input  wire                        clk,
  input  wire                        rst_n,
  input  wire                        start,
  input  wire [15:0]                 position,
  input  wire signed [HIDDEN*RES-1:0] x,
  output wire signed [HIDDEN*RES-1:0] x2,
  output wire                        done,
  output wire                        busy,
  output wire [LAYERS*16-1:0]        obs_sig,
  output wire [63:0]                 obs_sum,
  output wire                        obs_valid
);

  wire [LAYERS-1:0] start_w;
  wire [LAYERS-1:0] done_w;
  wire [LAYERS-1:0] busy_w;
  wire [(LAYERS+1)*HIDDEN*RES-1:0] x_w;

  assign x_w[HIDDEN*RES-1:0] = x;
  assign start_w[0] = start;
"""
    body = []
    for index in range(1, n):
        body.append(f"  assign start_w[{index}] = done_w[{index - 1}];")
    tail = f"""
  assign x2 = x_w[LAYERS*HIDDEN*RES +: HIDDEN*RES];
  assign done = done_w[LAYERS-1];
  assign busy = |busy_w;
  assign obs_sig = {{{sig_assign}}};
  assign obs_sum = {sum_assign};
  assign obs_valid = {valid_assign};
"""
    (out / "rtl" / "tile_chain.v").write_text(
        header + "\n".join(body) + "\n\n" + "\n".join(decls) + "\n\n"
        + "\n\n".join(instances) + "\n" + tail + "\nendmodule\n"
    )
    return {
        "layers": n,
        "layerList": chosen,
        "width": res,
        "macs": macs,
        "file": "rtl/tile_chain.v",
        "sha256": hashlib.sha256((out / "rtl" / "tile_chain.v").read_bytes()).hexdigest(),
    }
