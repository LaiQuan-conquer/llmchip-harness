"""A model on disk (a directory of Hugging Face files or a llama2.c checkpoint) as a recovered
graph plus its weights, outside the kernel: the same census and graph recovery as S1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_model.pipeline import _role


@dataclass
class ModelSource:
    path: Path
    graph: dict
    weights: WeightLoader
    census: dict
    config: dict | None
    tokenizer: Path | None  # tokenizer.json or a llama2.c tokenizer .bin beside the weights

    @property
    def family(self) -> str:
        return self.graph["family"]

    @property
    def vocab(self) -> int:
        return int(self.graph["hyper"]["vocab"])

    @property
    def params(self) -> int:
        return int(self.census["totals"]["parameters"])

    @property
    def max_positions(self) -> int | None:
        v = self.census["hyper"].get("maxPositions", {})
        return int(v["value"]) if isinstance(v, dict) and isinstance(v.get("value"), int) else None

    def stop_tokens(self) -> list[int]:
        """llama2.c generation stops at BOS (run.c); Hugging Face models at their eos_token_id."""
        if self.family == "llama2c":
            return [1]
        found: list[int] = []
        for name in ("generation_config.json", "config.json"):
            p = self.path / name
            if self.path.is_dir() and p.exists():
                eos = json.loads(p.read_text()).get("eos_token_id")
                found += eos if isinstance(eos, list) else [eos] if isinstance(eos, int) else []
        return sorted(set(found))


def load(path: Path, context: int) -> ModelSource:
    path = Path(path)
    files = [path] if path.is_file() else sorted(p for p in path.iterdir() if p.is_file())
    if path.is_file():  # a llama2.c checkpoint: its tokenizer is usually beside it
        files += sorted(p for p in path.parent.iterdir() if p.is_file() and p != path and _role(p)[0] == "tokenizer")
    weights = [p for p in files if _role(p)[0] == "weights"]
    if not weights:
        raise ValueError(f"no weight file (.safetensors, .gguf or llama2.c .bin) in {path}")
    formats = {_role(p)[1] for p in weights}
    if len(formats) > 1:
        raise ValueError(f"{path} mixes weight formats {sorted(formats)}; give one model")
    layouts = [read_layout(p) for p in weights]
    config_file = path / "config.json" if path.is_dir() else None
    config = json.loads(config_file.read_text()) if config_file and config_file.exists() else None
    paths = {p.name: p for p in weights}
    c = census(layouts, config, context, paths=paths)
    graph = recover(c)
    if not graph.get("supported"):
        raise ValueError(f"the graph of {path} cannot be recovered: {graph.get('reason')}")
    tokenizer = next((p for p in files if _role(p)[0] == "tokenizer"), None)
    return ModelSource(path, graph, WeightLoader(layouts, paths), c, config, tokenizer)
