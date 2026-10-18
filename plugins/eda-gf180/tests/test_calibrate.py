import importlib.util
import random
import re
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("calibrate", Path(__file__).parents[1] / "calibrate.py")
calibrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibrate)


def test_rom_verilog_has_one_entry_per_word_and_a_default():
    text = calibrate.rom_verilog(8, 12, random.Random(0))
    assert "input [2:0] addr" in text and "output reg [11:0] data" in text
    assert len(re.findall(r"^\s+\d+: data <= 12'h[0-9a-f]{3};$", text, re.M)) == 8
    assert "default: data <= 12'h0;" in text


def test_density_range_follows_the_size_trend():
    measurements = [{"densityUm2PerBit": 4.0}, {"densityUm2PerBit": 3.0}, {"densityUm2PerBit": 2.5}]
    r = calibrate.density_range(measurements)
    assert r["typ"] == 2.5 and r["high"] == 3.0
    assert r["low"] == pytest.approx(2.5 * 2.5 / 3.0)
    assert r["confidence"] == "calibrated"
