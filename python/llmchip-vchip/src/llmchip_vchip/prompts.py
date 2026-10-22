"""Prompt sets: JSONL (`{"id", "prompt", "expected"?}` per line) or an `llmchip-evalset/1` JSON file
(prompts, plus passages for perplexity)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PromptCase:
    id: str
    prompt: str
    expected: str | None = None


@dataclass
class PromptSet:
    cases: list[PromptCase]
    passages: list[str] = field(default_factory=list)  # held-out text for perplexity
    source: str = "inline"

    def to_jsonl(self) -> str:
        return "".join(json.dumps({k: v for k, v in c.__dict__.items() if v is not None}) + "\n" for c in self.cases)


def inline(prompts: list[str]) -> PromptSet:
    return PromptSet([PromptCase(f"p{i}", p) for i, p in enumerate(prompts)])


def load(path: Path) -> PromptSet:
    text = path.read_text()
    if path.suffix == ".json":
        data = json.loads(text)
        if data.get("format") != "llmchip-evalset/1":
            raise ValueError(f"{path}: a .json prompt set must be an llmchip-evalset/1 file")
        cases = [PromptCase(f"p{i}", p) for i, p in enumerate(data["prompts"])]
        return PromptSet(cases, list(data.get("passages", [])), str(path))
    cases, passages = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{n}: {e}") from e
        if "passage" in row:
            passages.append(str(row["passage"]))
            continue
        if not isinstance(row.get("prompt"), str):
            raise ValueError(f"{path}:{n}: each line needs a string `prompt` (or `passage`)")
        cases.append(PromptCase(str(row.get("id", f"p{len(cases)}")), row["prompt"], row.get("expected")))
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: prompt ids are not unique")
    return PromptSet(cases, passages, str(path))
