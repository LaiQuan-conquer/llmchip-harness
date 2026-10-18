import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).parents[1]
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


constraints = load("constraints")
physical = load("physical")

CHIP = {
    "targets": {"die": {"xMm": 1.2, "yMm": 2}},
    "pins": [
        {"name": n, "dir": d, **({"enable": "busy"} if n.startswith("kv_") and d == "out" else {})}
        for n, d in (
            ("clk", "in"),
            ("rst_n", "in"),
            ("host_sclk", "in"),
            ("host_cs_n", "in"),
            ("host_mosi", "in"),
            ("host_miso", "out"),
            ("busy", "out"),
            ("kv_sclk", "out"),
            ("kv_cs_n", "out"),
            ("kv_mosi", "out"),
            ("kv_miso", "in"),
        )
    ],
    "timing": {
        "clock": {"port": "clk", "periodNs": 125.0},
        "inputs": {
            p: {"maxNs": 62.5, "minNs": 0.0} for p in ("rst_n", "host_sclk", "host_cs_n", "host_mosi", "kv_miso")
        },
        "outputs": {p: {"maxNs": 31.25, "minNs": 0.0} for p in ("host_miso", "busy", "kv_sclk", "kv_cs_n", "kv_mosi")},
    },
}


def test_pad_plan_fits_the_die_and_places_every_pin_and_supply():
    plan = physical.pad_plan(CHIP, 2)
    placed = [i for side in plan["sides"].values() for i in side]
    assert sorted(placed) == sorted([p["inst"] for p in plan["pads"]] + [s["inst"] for s in plan["supplies"]])
    assert all(len(plan["sides"][s]) <= plan["capacity"][s] for s in plan["sides"])
    top = physical.chip_top(plan)
    assert "gf180mcu_fd_io__in_c p_clk" in top and ".OE(c_kv_oe)" in top and "llmchip_chip u_chip" in top
    assert top.count("gf180mcu_fd_io__dvdd") == 2


def test_core_area_leaves_the_pad_ring():
    x0, _y0, x1, _y1 = physical.core_area(CHIP)
    assert x0 >= physical.EDGE + physical.PAD_H and x1 <= 1200 - physical.EDGE - physical.PAD_H


def test_sdc_comes_from_the_spec_and_has_no_exceptions():
    sdc = constraints.sdc_from_spec(CHIP)
    delayed = constraints.delayed_ports(sdc)
    assert delayed["input"] == set(CHIP["timing"]["inputs"]) and delayed["output"] == set(CHIP["timing"]["outputs"])
    assert constraints.exceptions(sdc) == []
    assert any("get_ports clk" in c for c in constraints.clocks(sdc))


def test_constraint_checks_find_a_removed_delay_and_an_unregistered_false_path():
    signoff = load("signoff")
    sdc = constraints.sdc_from_spec(CHIP)
    assert signoff.sdc_values_match(sdc, CHIP["timing"])
    removed = "\n".join(line for line in sdc.splitlines() if "get_ports kv_miso" not in line or "transition" in line)
    assert "kv_miso" not in constraints.delayed_ports(removed)["input"]
    # Only the setup bound removed: the port is not constrained either (AC-06).
    no_max = "\n".join(line for line in sdc.splitlines() if not ("-max" in line and "kv_miso" in line))
    assert "kv_miso" not in constraints.delayed_ports(no_max)["input"]
    changed = sdc.replace("-max 31.25 [get_ports busy]", "-max 10 [get_ports busy]")
    assert not signoff.sdc_values_match(changed, CHIP["timing"])
    extra = sdc + "set_false_path -from [get_ports host_mosi]\n"
    assert constraints.exceptions(extra) == ["set_false_path -from [get_ports host_mosi]"]
    assert json.dumps(constraints.exceptions(extra))
