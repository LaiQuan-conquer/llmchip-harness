"""The slice of a decoder layer (requirements FR-MDL-06, plan §4.1): a part of layer 0 whose
output exists in L2 exactly.

A slice must be aligned to the numeric spec's weight groups, otherwise its output is not one of
L2's observation points and cannot be compared bit for bit. The levels of the plan:

- S-1: one Q head (head_dim rows of `q_proj`, all input columns) - 57,344 weights on Qwen2.5-0.5B;
- S-2: S-1 plus the K and V rows of one KV head, and the O-projection columns of whole heads
  (the group size decides how many heads: 128 columns is two heads of 64);
- S-3: S-2 plus `channels` MLP channels of gate, up (rows) and down (columns).

The spec records which L2 observation points the slice reproduces.
"""

from __future__ import annotations

from llmchip_numeric.spec import WEIGHT_FORMATS

LEVELS = {
    "S-1": ("q",),
    "S-2": ("q", "k", "v", "o"),
    "S-3": ("q", "k", "v", "o", "gate", "up", "down"),
}
POINTS = {"q": "q", "k": "k", "v": "v", "o": "attn", "gate": "h1", "up": "h3", "down": "g"}


def _shape(h: dict, node_id: str) -> tuple[int, int] | None:
    """(rows, columns) of a matrix of the layer, from the model's shape."""
    hd, hidden, inter = h["headDim"], h["hidden"], h["intermediate"]
    kv = h["kvHeads"] * hd
    return {
        "q_proj": (hidden, hidden),
        "k_proj": (kv, hidden),
        "v_proj": (kv, hidden),
        "o_proj": (hidden, h["heads"] * hd),
        "gate_proj": (inter, hidden),
        "up_proj": (inter, hidden),
        "down_proj": (hidden, inter),
    }.get(node_id)


def _aligned(start: int, length: int, group: int) -> bool:
    return start % group == 0 and length % group == 0


def slice_spec(graph: dict, spec: dict, level: str, layer: int = 0, channels: int = 128) -> dict:
    """The slice's matrices with their row and column ranges, and the alignment proof."""
    if level not in LEVELS:
        raise ValueError(f"unknown slice level {level}")
    h = graph["hyper"]
    group = spec["weights"]["groupSize"]
    hd = h["headDim"]
    heads_for_o = -(-group // hd) * hd  # whole heads whose columns fill whole groups
    wanted = LEVELS[level]
    bits_of = spec["weights"].get("formats", {})
    default_bits = spec["weights"]["storageBits"]
    matrices: list[dict] = []
    for node_id, (rows, cols) in (
        ("q_proj", (hd, h["hidden"])),
        ("k_proj", (hd, h["hidden"])),
        ("v_proj", (hd, h["hidden"])),
        ("o_proj", (h["hidden"], heads_for_o)),
        ("gate_proj", (channels, h["hidden"])),
        ("up_proj", (channels, h["hidden"])),
        ("down_proj", (h["hidden"], channels)),
    ):
        key = node_id.removesuffix("_proj")
        if key not in wanted:
            continue
        shape = _shape(h, node_id)
        if shape is None:
            raise ValueError(f"the graph has no {node_id} in its blocks")
        full_rows, full_cols = shape
        if node_id in ("q_proj", "k_proj", "v_proj", "gate_proj", "up_proj"):
            r0, r1, c0, c1 = 0, min(rows, full_rows), 0, full_cols
        else:
            r0, r1, c0, c1 = 0, full_rows, 0, min(cols, full_cols)
        if not _aligned(c0, c1 - c0, group):
            raise ValueError(
                f"{node_id} columns {c0}..{c1} are not aligned to groups of {group}: a slice needs "
                f"whole heads ({heads_for_o} columns) or whole groups"
            )
        fmt = bits_of.get(f"{layer}.{node_id}")
        matrices.append(
            {
                "node": node_id,
                "rows": [r0, r1],
                "columns": [c0, c1],
                "weights": (r1 - r0) * (c1 - c0),
                "groups": -(-(c1 - c0) // group),
                "bits": WEIGHT_FORMATS[fmt]["bits"] if fmt else default_bits,
            }
        )
    if not matrices:
        raise ValueError(f"the graph has no matrix of level {level}")
    total = sum(m["weights"] for m in matrices)
    return {
        "format": "llmchip-arch-slice/1",
        "level": level,
        "layer": layer,
        "architecture": "taalas-tile/1",
        "model": {"family": graph["family"], "architecture": graph.get("architecture")},
        "hyper": {k: h[k] for k in ("hidden", "heads", "kvHeads", "headDim", "intermediate")},
        "groupSize": group,
        "matrices": matrices,
        "total": {"weights": total, "bits": sum(m["weights"] * m["bits"] for m in matrices)},
        "alignment": {
            "aligned": True,
            "rule": "every column range is the whole input or a whole number of weight groups",
            "groupSize": group,
        },
        "observationPoints": [POINTS[key] for key in wanted],
        "note": "the slice's outputs are L2's observation points of layer 0; its input is L2's real activation",
    }
