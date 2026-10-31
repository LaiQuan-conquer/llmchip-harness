"""Finds virtual chips (directories with a `chip.json`) and opens them through their adapter."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import manifest as mf
from .interface import ChipError, VirtualChip

# Bundled backends: name -> "module:Class". A chip may instead name its own `entrypoint`.
BACKENDS = {
    "reference": "llmchip_vchip.adapters.reference:ReferenceChip",
    "numeric": "llmchip_vchip.adapters.numeric:NumericChip",
    "process": "llmchip_vchip.adapters.process:ProcessChip",
    "rtl": "llmchip_vchip.adapters.rtl:RtlChip",
    "graph-numeric": "llmchip_vchip.adapters.graph:GraphChip",
}


def default_dirs() -> list[Path]:
    """`$LLMCHIP_VCHIP_CHIPS` (os.pathsep-separated), else `<LLMCHIP_HOME or ~/.llmchip>/vchip/chips`."""
    env = os.environ.get("LLMCHIP_VCHIP_CHIPS")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    home = Path(os.environ.get("LLMCHIP_HOME", "~/.llmchip")).expanduser()
    return [home / "vchip" / "chips"]


@dataclass
class Entry:
    manifest: mf.Manifest

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def available(self) -> bool:
        return not self.manifest.problems

    @property
    def reason(self) -> str:
        return "; ".join(self.manifest.problems)


class Registry:
    def __init__(self, dirs: list[Path] | None = None) -> None:
        self.dirs = [Path(d) for d in (dirs if dirs is not None else default_dirs())]
        self.entries: dict[str, Entry] = {}
        self.duplicates: list[Entry] = []
        self.scan()

    def scan(self) -> None:
        self.entries.clear()
        self.duplicates.clear()
        for d in self.dirs:
            if (d / "chip.json").is_file():
                candidates = [d]
            elif d.is_dir():
                candidates = sorted(p for p in d.iterdir() if (p / "chip.json").is_file())
            else:
                candidates = []
            for chip_dir in candidates:
                entry = Entry(mf.load(chip_dir, BACKENDS))
                if entry.id in self.entries:
                    entry.manifest.problems.append(
                        f"duplicate id {entry.id} (also {self.entries[entry.id].manifest.dir})"
                    )
                    self.duplicates.append(entry)
                else:
                    self.entries[entry.id] = entry

    def all(self) -> list[Entry]:
        return [*self.entries.values(), *self.duplicates]

    def get(self, chip_id: str) -> Entry:
        if chip_id not in self.entries:
            raise KeyError(f"no chip {chip_id} in {', '.join(str(d) for d in self.dirs)}")
        return self.entries[chip_id]

    def open(self, chip_id: str) -> VirtualChip:
        return open_chip(self.get(chip_id).manifest)


def adapter_class(m: mf.Manifest) -> type:
    target = m.data.get("entrypoint") or BACKENDS[m.data["backend"]]
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def open_chip(m: mf.Manifest) -> VirtualChip:
    """Instantiates the chip's adapter; an invalid manifest or a failing adapter raises."""
    if m.problems:
        raise mf.ManifestError(m.problems)
    try:
        chip = adapter_class(m)(m)
    except ChipError:
        raise
    except Exception as e:
        raise ChipError("crash", f"the adapter of chip {m.id} failed to load: {e}") from e
    if not isinstance(chip, VirtualChip):
        raise ChipError("crash", f"the adapter of chip {m.id} does not implement VirtualChip")
    return chip
