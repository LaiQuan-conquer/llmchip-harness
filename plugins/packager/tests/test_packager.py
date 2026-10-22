import io
import json
import struct
import tarfile
from pathlib import Path

from llmchip_packager import bonding_svg, build, check, gds_structures, import_external, sha256

TEMPLATE = json.loads((Path(__file__).parents[2] / "process-library" / "data" / "package-gf180mcu.json").read_text())


def gds(top: str) -> bytes:
    name = top.encode() + (b"\0" if len(top) % 2 else b"")
    return struct.pack(">HH", 6, 0x0002) + b"\x02\x58" + struct.pack(">HH", 4 + len(name), 0x0606) + name


def package(gds_bytes: bytes, gds_hash: str):
    inputs = {
        k: (json.dumps({"gds": gds_hash}).encode() if k in ("drc", "lvs") else b"{}", f"{k}-hash")
        for k in ("chip", "pads", "drc", "lvs", "sta", "constraints", "exceptions", "measurements")
    }
    inputs["gds"] = (gds_bytes, gds_hash)
    generated = {g: g.encode() for g in ("layer map", "bonding diagram", "test plan", "readme")}
    return build(TEMPLATE, inputs, generated)


def test_gds_structure_names():
    assert gds_structures(gds("chip_top")) == ["chip_top"]


def test_a_complete_package_passes_and_a_changed_file_does_not():
    data = gds("chip_top")
    tar, manifest = package(data, sha256(data))
    assert {f["path"] for f in manifest["files"]} == {e["path"] for e in TEMPLATE["files"]}
    result = check(tar, TEMPLATE, sha256(data))
    assert result["missing_files"] == 0 and result["hash_mismatches"] == 0
    assert result["gds_matches_snapshot"] and result["top_cell_present"] and result["reports_bound_to_gds"]
    # A different GDS in the snapshot: the package no longer belongs to it.
    other = gds("chip_top2")
    assert not check(tar, TEMPLATE, sha256(other))["gds_matches_snapshot"]
    # Tampering with a file after the manifest was written is found.
    files = {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        for m in t.getmembers():
            files[m.name] = t.extractfile(m).read()
    files["package/README.md"] = b"changed"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            t.addfile(info, io.BytesIO(content))
    assert check(buf.getvalue(), TEMPLATE, sha256(data))["hash_mismatches"] == 1


def test_external_reports_count_only_for_the_current_design():
    report = {
        "gdsHash": "a" * 64,
        "pdkVersion": "1.0",
        "ruleFileVersion": "r3",
        "tool": "calibre",
        "toolVersion": "2025.1",
        "conditions": {"switches": ["DENSITY"]},
        "topCell": "chip_top",
        "violations": 0,
    }
    assert import_external(report, "a" * 64) | {"missing_binding": []} == import_external(report, "a" * 64)
    assert import_external(report, "a" * 64)["matches_current_design"]
    assert not import_external(report, "b" * 64)["matches_current_design"]
    assert not import_external({k: v for k, v in report.items() if k != "toolVersion"}, "a" * 64)["binding_complete"]


def test_bonding_diagram_numbers_every_pad():
    pads = {
        "die": {"xMm": 1.2, "yMm": 2},
        "sides": {"south": ["p_clk"], "west": ["p_vdd0"]},
        "pads": [{"inst": "p_clk", "pin": "clk"}, {"inst": "p_vdd0", "pin": "VDD"}],
    }
    svg = bonding_svg(pads)
    assert "1: clk" in svg and "2: VDD" in svg


def test_the_test_plan_names_the_memory_the_chip_has():
    from llmchip_packager import memory_setup

    pins = lambda *names: {"pins": [{"name": n} for n in names]}  # noqa: E731
    assert "quad-SPI" in memory_setup(pins("clk", "kv_sclk", "kv_cs_n", "kv_sio0"))
    assert "SPI SRAM" in memory_setup(pins("clk", "kv_sclk", "kv_cs_n", "kv_mosi", "kv_miso"))
    # The spatial engine has no memory pins: the plan must not ask for an SRAM.
    none = memory_setup(pins("clk", "rst_n", "host_sclk", "host_cs_n", "host_mosi", "host_miso", "busy"))
    assert "none" in none and "SRAM" not in none
