"""Test harness for virtual chips (llmchip-vchip).

A virtual chip is a model plus a backend that executes it one step at a time (`interface.py`);
the harness is the model-independent host around it: it loads chips from their manifests
(`manifest.py`, `registry.py`), tokenizes prompts, drives the generation loop
(`generator.py`) through the chip's host protocol (`hosts.py`), measures quality and speed
(`metrics.py`), applies the chip's own policy (`policy.py`) and writes reports
(`runner.py`, `report.py`). `conform.py` checks a chip against the interface itself.
"""

__version__ = "0.1.0"
