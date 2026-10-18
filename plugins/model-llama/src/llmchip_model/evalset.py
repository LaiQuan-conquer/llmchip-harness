"""Evaluation sets: prompts for greedy generation and passages for perplexity."""

from __future__ import annotations

import json
from pathlib import Path


def load_evalset(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("format") != "llmchip-evalset/1" or not data.get("prompts"):
        raise ValueError("not an llmchip-evalset/1 file with prompts")
    data.setdefault("steps", 64)
    data.setdefault("passages", [])
    return data
