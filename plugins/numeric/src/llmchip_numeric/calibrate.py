"""Calibration text and sequences for GPTQ (requirements FR-QNT-01).

The calibration text is packed once into an artifact (`calibration_data`) so that a run records
exactly which text its weights were calibrated on. `load_calibration` reads the packed text of
this machine's tools directory (WikiText-2, `tools/hf/datasets/Salesforce___wikitext`, as Arrow
files, or a plain text/JSON file); `sequences` turns it into the token windows `gptq.calibrate`
consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

MAX_CHARS = 8_000_000  # about 2 M tokens of English: enough for 64 windows of 2048 tokens


def load_calibration(path: Path, max_chars: int = MAX_CHARS) -> list[str]:
    """The calibration documents at `path`: a directory of Arrow files, a text file, or JSON."""
    if path.is_dir():
        files = sorted(path.rglob("*.arrow"))
        if not files:
            raise ValueError(f"no Arrow files under {path}")
        from datasets import Dataset

        texts: list[str] = []
        total = 0
        for file in files:
            column = Dataset.from_file(str(file))["text"]
            for text in column:
                texts.append(text)
                total += len(text)
                if total >= max_chars:
                    return texts
        return texts
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".json":
        data = json.loads(text)
        return data["texts"] if isinstance(data, dict) else data
    return [text[:max_chars]]


def sequences(tokenizer, texts: list[str], context: int, count: int) -> list[list[int]]:
    """`count` token windows of `context` tokens, back to back over the joined text."""
    ids = tokenizer("\n\n".join(texts), add_special_tokens=False)["input_ids"]
    windows = [ids[i : i + context] for i in range(0, len(ids) - context + 1, context)]
    if len(windows) < count:
        raise ValueError(f"the calibration text gives {len(windows)} windows of {context} tokens, need {count}")
    return windows[:count]
