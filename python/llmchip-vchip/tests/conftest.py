"""Shared fixtures: baseline chips made from the repository's test models, and process chips
around tests/chips/toy_chip.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from llmchip_vchip.build import new_chip
from llmchip_vchip.manifest import FORMAT

ROOT = Path(__file__).resolve().parents[3]
MODELS = ROOT / "tests" / "fixtures" / "models"
STORIES = MODELS / "stories260K"
TOY = Path(__file__).parent / "chips" / "toy_chip.py"


@pytest.fixture(scope="session")
def baseline_dir(tmp_path_factory) -> Path:
    """reference and numeric chips of stories260K (context 32), a reference chip of tiny-qwen3."""
    d = tmp_path_factory.mktemp("chips")
    quiet = lambda _m: None  # noqa: E731
    new_chip("reference", STORIES / "stories260K.bin", "s260k-ref", d, context=32, progress=quiet)
    new_chip("numeric", STORIES / "stories260K.bin", "s260k-l2", d, context=32, progress=quiet)
    new_chip("reference", MODELS / "tiny-qwen3", "qwen3-ref", d, context=32, progress=quiet)
    return d


def process_chip(chips: Path, chip_id: str, *args: str, **fields) -> Path:
    """A `process` chip running toy_chip.py with `args`; `fields` override chip.json."""
    d = chips / chip_id
    d.mkdir(parents=True)
    manifest = {
        "format": FORMAT,
        "id": chip_id,
        "backend": "process",
        "model": {"family": "toy", "context": 32, "vocab": 512},
        "tokenizer": {"kind": "llama2c", "path": str(STORIES / "tok512.bin"), "stop": [1]},
        "clock_mhz": 8,
        "host": {"kind": "spi"},
        "capabilities": ["greedy", "cycles"],
        "command": [sys.executable, str(TOY), "{chip_dir}", *args],
        "timeout_s": 5,
        **fields,
    }
    (d / "chip.json").write_text(json.dumps(manifest))
    return d


@pytest.fixture
def make_process():
    return process_chip
