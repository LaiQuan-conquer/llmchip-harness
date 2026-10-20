"""Tapeout package (S8, requirements FR-PKG-01/03).

`build` assembles the package the process's template defines: the GDS with its
top cell and layer map, the signoff reports, the constraints and timing
exception registry, the chip spec, the pad list with a bonding diagram draft,
the measurement record and a test plan. Every file is listed in the manifest
with its hash and the artifact it came from, so each file can be traced back
(the export adds the full lineage, FR-PKG-02).

`check` is the integrity check before G4: every template file is present and
matches the manifest, the GDS is the snapshot's GDS with the expected top cell,
and every signoff report judged that very GDS.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tarfile

MANIFEST = "MANIFEST.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gds_structures(data: bytes) -> list[str]:
    """Structure (cell) names of a GDSII stream (STRNAME records)."""
    names, pos = [], 0
    while pos + 4 <= len(data):
        length, rtype = struct.unpack_from(">HH", data, pos)
        if length < 4:
            break
        if rtype == 0x0606:  # STRNAME, ASCII
            names.append(data[pos + 4 : pos + length].rstrip(b"\0").decode("ascii", "replace"))
        pos += length
    return names


def bonding_svg(pads: dict) -> str:
    """Bonding diagram draft: die outline, pads in placement order, package pin numbers
    counter-clockwise from the lower left (a proposal for the packaging house)."""
    die = pads["die"]
    w, h = die["xMm"] * 1000, die["yMm"] * 1000
    s = 0.3  # px per um
    names = {p["inst"]: p for p in pads["pads"]}
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w * s + 200:.0f}" height="{h * s + 200:.0f}" '
        'font-family="sans-serif" font-size="11">',
        f'<rect x="100" y="100" width="{w * s:.0f}" height="{h * s:.0f}" fill="#f6f7f9" stroke="#333"/>',
        f'<text x="100" y="90">chip_top {die["xMm"]} x {die["yMm"]} mm (draft; not to scale for bond wires)</text>',
    ]
    pin = 1
    for side in ("south", "east", "north", "west"):
        insts = pads["sides"].get(side, [])
        for k, inst in enumerate(insts if side in ("south", "east") else list(reversed(insts))):
            frac = (k + 1) / (len(insts) + 1)
            if side == "south":
                x, y = 100 + frac * w * s, 100 + h * s
            elif side == "east":
                x, y = 100 + w * s, 100 + (1 - frac) * h * s
            elif side == "north":
                x, y = 100 + (1 - frac) * w * s, 100
            else:
                x, y = 100, 100 + frac * h * s
            label = names.get(inst, {}).get("pin", inst)
            out.append(
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="6" fill="#2f6fed"/>'
                f'<text x="{x + 9:.0f}" y="{y - 6:.0f}">{pin}: {label}</text>'
            )
            pin += 1
    out.append("</svg>")
    return "\n".join(out)


def memory_setup(chip: dict) -> str:
    """How the external SRAM is wired, from the chip's pins."""
    names = {p["name"] for p in chip.get("pins", [])}
    if "kv_sio0" in names:
        return (
            "- External memory: a 23LC1024-style serial SRAM in quad-SPI (SQI) mode on kv_sclk / kv_cs_n / "
            "kv_sio0..3 (the chip puts it into SQI mode after reset; it drives SCK and CS only while busy and "
            "the data lines only while it sends; pull-up on kv_cs_n and on kv_sio0..3, pull-down on kv_sclk; "
            "a board reading the SRAM while the chip is idle uses SQI or sends RSTIO first)."
        )
    return (
        "- External memory: a 23LC1024-style SPI SRAM on kv_sclk / kv_cs_n / kv_mosi / kv_miso "
        "(the chip drives the bus only while busy; pull-up on kv_cs_n, pull-downs on kv_sclk and kv_mosi)."
    )


def test_plan(chip: dict, l2: dict, design: dict | None) -> str:
    """Bring-up and functional test plan (S9 input): supplies, clock, protocol, expected tokens."""
    t = chip["timing"]
    lines = [
        "# Test plan (draft)",
        "",
        f"Chip: {chip.get('architecture')} implementation of numeric spec {chip['numericSpec']['hash'][:16]}…",
        "",
        "## Setup",
        "- Supply: one 5 V domain (all dvdd/dvss pads).",
        f"- Clock: {round(1000 / t['clock']['periodNs'], 3)} MHz on `clk`; `rst_n` low for at least 4 clocks.",
        memory_setup(chip),
        f"- Host: {chip['interfaces']['host']['kind']}.",
        "",
        "## Protocol",
        *[f"- `{cmd}`: {text}" for cmd, text in chip["interfaces"]["host"]["commands"].items()],
        "",
        "## Functional test (silicon against L2, token for token)",
        "For each prompt: send START for every position (prompt tokens, then the token the chip returned), "
        "poll STATUS until not busy, read the token. Pass: every token equals the L2 token below.",
        "",
    ]
    for g in l2.get("generations", []):
        lines += [f"- prompt `{g['prompt']}`: tokens {g['tokens']}"]
    if design:
        lines += [
            "",
            "## Debug readback",
            "With START mode bit 0 set, the observation points are copied to these external-memory word addresses:",
            "",
        ]
        lines += [
            f"- point {tr['point']} layer {tr['layer']}: {tr['n']} words at {tr['addr']}"
            for tr in design.get("traceMap", [])
            if not tr.get("debug")
        ]
    return "\n".join(lines) + "\n"


def build(template: dict, inputs: dict[str, tuple[bytes, str]], generated: dict[str, bytes]) -> tuple[bytes, dict]:
    """The package archive and its manifest. inputs: name -> (content, artifact hash)."""
    files: dict[str, bytes] = {}
    manifest: dict = {
        "format": "llmchip-package-manifest/1",
        "process": template["process"],
        "topCell": template["topCell"],
        "files": [],
    }
    for entry in template["files"]:
        if "from" in entry:
            if entry["from"] not in inputs:
                raise ValueError(f"the template needs input {entry['from']} for {entry['path']}")
            data, source = inputs[entry["from"]]
            origin = {"input": entry["from"], "artifact": source}
        else:
            if entry["generated"] not in generated:
                raise ValueError(f"no generator for {entry['generated']}")
            data, origin = generated[entry["generated"]], {"generated": entry["generated"]}
        files[entry["path"]] = data
        manifest["files"].append({"path": entry["path"], "sha256": sha256(data), "size": len(data), **origin})
    files[MANIFEST] = json.dumps(manifest, indent=2).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(files[name])
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue(), manifest


def check(package: bytes, template: dict, gds_hash: str) -> dict:
    with tarfile.open(fileobj=io.BytesIO(package)) as tar:
        files = {m.name.removeprefix("package/"): tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    manifest = json.loads(files.get(MANIFEST, b"{}") or b"{}")
    listed = {f["path"]: f for f in manifest.get("files", [])}
    missing = [e["path"] for e in template["files"] if e["path"] not in files or e["path"] not in listed]
    mismatched = [p for p, f in listed.items() if p in files and sha256(files[p]) != f["sha256"]]
    gds_entry = next((e["path"] for e in template["files"] if e.get("from") == "gds"), None)
    gds = files.get(gds_entry or "", b"")
    structures = gds_structures(gds) if gds else []
    reports_bound = True
    for entry in template["files"]:
        if entry.get("bindsGds") and entry["path"] in files:
            try:
                reports_bound &= json.loads(files[entry["path"]]).get("gds") == gds_hash
            except ValueError:
                reports_bound = False
    return {
        "template_files": len(template["files"]),
        "missing_files": len(missing),
        "hash_mismatches": len(mismatched),
        "gds_matches_snapshot": bool(gds) and sha256(gds) == gds_hash,
        "top_cell_present": template["topCell"] in structures,
        "reports_bound_to_gds": reports_bound,
        "missing": missing,
        "mismatched": mismatched,
    }


# -- external signoff results (FR-SGN-06) ------------------------------------------------

BINDING_FIELDS = ("gdsHash", "pdkVersion", "ruleFileVersion", "tool", "toolVersion", "conditions", "topCell")


def import_external(report: dict, gds_hash: str) -> dict:
    """An external (foundry or commercial-tool) signoff report counts only when it is bound
    to the current design: the checked GDS hash, PDK and rule file versions, tool and
    version, and conditions (rule switches, corners, top cell)."""
    missing = [f for f in BINDING_FIELDS if not report.get(f)]
    return {
        "binding_complete": not missing,
        "missing_binding": missing,
        "matches_current_design": report.get("gdsHash") == gds_hash,
        "violations": int(report.get("violations", -1)),
        "check": report.get("check", "drc"),
    }
