"""Benchmark data for the model quality evaluation (requirements FR-QNT-04/08).

The seven open benchmarks are read once from this machine (the Hugging Face cache that
lm-evaluation-harness filled when it ran with network, the CMMLU CSV files, the EvalPlus
releases and NLTK's `punkt_tab`) and packed into one artifact, `benchmark.data`: a tar of
parquet files per lm-eval task and split, the EvalPlus problem files and the NLTK data.
Its index (`benchmark.sets`) lists every task, its source dataset, the number of questions,
and the quick set: 5 questions per subject of every multiple-choice benchmark and 5 per
generation benchmark, drawn with the fixed seed 20261024 (owner decision 2026-10-24).

Evaluations read only this artifact: `serve` routes lm-eval's dataset loading to the packed
files, so the tasks keep lm-eval's own prompts, few-shot examples, filters and metrics, and a
verdict is bound to exactly the data it was computed on. CMMLU and C-Eval are licensed for
non-commercial use only; the artifact stays on this machine (label `internal`).
"""

from __future__ import annotations

import io
import json
import os
import random
import tarfile
from collections.abc import Iterator
from pathlib import Path

DATA_FORMAT = "llmchip-benchmark-data/1"
SEED = 20261024
PER_SUBJECT = 5
# benchmark id -> lm-eval group or task; multiple choice: 5 per subject, generation: 5 per benchmark.
CHOICE = {"mmlu": "mmlu", "cmmlu": "cmmlu", "ceval": "ceval-valid"}
GENERATION = {"gsm8k": "gsm8k", "ifeval": "ifeval"}
CODE = {"humaneval": "HumanEvalPlus.jsonl", "mbpp": "MbppPlus.jsonl"}
BENCHMARKS = [*CHOICE, *GENERATION, *CODE]
LICENSES = {
    "mmlu": "MIT",
    "cmmlu": "CC-BY-NC-4.0",
    "ceval": "CC-BY-NC-SA-4.0",
    "gsm8k": "MIT",
    "ifeval": "Apache-2.0",
    "humaneval": "Apache-2.0",
    "mbpp": "Apache-2.0",
}


def leaves(d: dict) -> Iterator:
    for v in d.values():
        if isinstance(v, dict):
            yield from leaves(v)
        else:
            yield v


def _cmmlu_loader(csv_dir: Path):
    """CMMLU from its CSV files, every field as text ("(1) (2) (3)" and "1.5" stay strings):
    the hub dataset needs a loading script that `datasets` no longer runs."""
    import datasets
    import pandas as pd

    def load(subject: str):
        splits = {}
        for split in ("test", "dev"):
            frame = pd.read_csv(csv_dir / split / f"{subject}.csv", dtype=str, keep_default_na=False)
            frame = frame.drop(columns=[c for c in frame.columns if c.startswith("Unnamed")])
            splits[split] = datasets.Dataset.from_pandas(frame, preserve_index=False)
        return datasets.DatasetDict(splits)

    return load


class _Route:
    """Replaces `datasets.load_dataset` while lm-eval builds its tasks."""

    def __init__(self, handler) -> None:
        import datasets

        self.datasets, self.handler, self.original = datasets, handler, datasets.load_dataset

    def __enter__(self):
        original, handler = self.original, self.handler

        def load_dataset(path=None, name=None, *args, **kwargs):
            got = handler(path, name)
            return got if got is not None else original(path, name, *args, **kwargs)

        self.datasets.load_dataset = load_dataset
        return self

    def __exit__(self, *exc) -> None:
        self.datasets.load_dataset = self.original


def _tasks(manager, group: str) -> list:
    from lm_eval.tasks import get_task_dict

    return list(leaves(get_task_dict([group], manager)))


def _docs(task) -> list:
    return list(task.test_docs() if task.has_test_docs() else task.validation_docs())


def pack(source: Path, out_tar: Path, progress=lambda message: None) -> dict:
    """Reads the benchmarks from `source` (the layout of the workbench's tools directory: `hf/`,
    `datasets/cmmlu`, `datasets/evalplus`, `nltk_data`) and writes the data artifact; returns its index."""
    os.environ["HF_HOME"] = str(source / "hf")
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["NLTK_DATA"] = str(source / "nltk_data")
    from lm_eval.tasks import TaskManager

    cmmlu = _cmmlu_loader(source / "datasets" / "cmmlu")

    def route(path, name):
        if path == "haonan-li/cmmlu":
            return cmmlu(name)
        return None

    manager = TaskManager()
    index = {"format": DATA_FORMAT, "seed": SEED, "perSubject": PER_SUBJECT, "tasks": {}, "benchmarks": {}}
    rng = random.Random(SEED)
    quick: dict[str, list[int]] = {}
    written: set[str] = set()
    with tarfile.open(out_tar, "w") as tar, _Route(route):

        def add(name: str, data: bytes) -> None:
            written.add(name)
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))

        for bench, group in {**CHOICE, **GENERATION}.items():
            progress(f"read {bench}")
            tasks = _tasks(manager, group)
            names = []
            for task in tasks:
                cfg = task.config
                files = {}
                for split, ds in task.dataset.items():
                    buf = io.BytesIO()
                    ds.to_parquet(buf)
                    file = (
                        f"tasks/{cfg.dataset_path.replace('/', '__')}/{cfg.dataset_name or 'default'}/{split}.parquet"
                    )
                    files[split] = file
                    if file not in written:
                        add(file, buf.getvalue())
                n = len(_docs(task))
                index["tasks"][cfg.task] = {
                    "benchmark": bench,
                    "datasetPath": cfg.dataset_path,
                    "datasetName": cfg.dataset_name,
                    "files": files,
                    "questions": n,
                }
                names.append(cfg.task)
                # The quick set: per subject (choice) or for the whole benchmark (generation).
                quick[cfg.task] = sorted(rng.sample(range(n), min(PER_SUBJECT, n)))
            index["benchmarks"][bench] = {
                "kind": "choice" if bench in CHOICE else "generate",
                "group": group,
                "tasks": names,
                "questions": sum(index["tasks"][t]["questions"] for t in names),
                "quick": sum(len(quick[t]) for t in names),
                "license": LICENSES[bench],
            }
        for bench, file in CODE.items():
            path = source / "datasets" / "evalplus" / file
            data = path.read_bytes()
            add(f"evalplus/{file}", data)
            ids = [json.loads(line)["task_id"] for line in data.decode().splitlines() if line.strip()]
            picked = sorted(rng.sample(ids, PER_SUBJECT), key=ids.index)
            index["benchmarks"][bench] = {
                "kind": "code",
                "file": f"evalplus/{file}",
                "questions": len(ids),
                "quick": len(picked),
                "license": LICENSES[bench],
            }
            quick[bench] = picked
        nltk = source / "nltk_data"
        for p in sorted(nltk.rglob("*")):
            if p.is_file():
                add(f"nltk/{p.relative_to(nltk)}", p.read_bytes())
        index["quick"] = quick
        add("index.json", json.dumps(index, indent=1, ensure_ascii=False).encode())
    return index


class Data:
    """An unpacked data artifact, and lm-eval tasks that read only from it."""

    def __init__(self, tar_path: Path, work: Path) -> None:
        self.root = work / "benchmark-data"
        if not (self.root / "index.json").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_path) as tar:
                tar.extractall(self.root, filter="data")
        self.index = json.loads((self.root / "index.json").read_text())
        if self.index.get("format") != DATA_FORMAT:
            raise ValueError("not a benchmark data artifact")
        os.environ["NLTK_DATA"] = str(self.root / "nltk")
        os.environ["HF_DATASETS_OFFLINE"] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_DATASETS_CACHE"] = str(work / "hf-datasets-cache")
        self._by_source = {(t["datasetPath"], t["datasetName"]): t["files"] for t in self.index["tasks"].values()}

    def route(self):
        """Routes lm-eval's dataset loading to the packed files (any other dataset is refused)."""
        import datasets

        def handler(path, name):
            files = self._by_source.get((path, name))
            if files is None:
                raise ValueError(f"dataset {path}/{name} is not in the benchmark data artifact")
            return original("parquet", data_files={split: str(self.root / f) for split, f in files.items()})

        route = _Route(handler)
        original = route.original
        del datasets
        return route

    def tasks(self, bench: str) -> list[str]:
        return self.index["benchmarks"][bench]["tasks"]

    def code_problems(self, bench: str) -> list[dict]:
        text = (self.root / self.index["benchmarks"][bench]["file"]).read_text()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def quick_questions(self) -> list[str]:
        """The text of every quick-set question of the generation and code benchmarks (20 in all),
        as a user would ask it: the prompts of `spec.l2.fast`."""
        import pyarrow.parquet as pq

        out = []
        for bench, field in (("gsm8k", "question"), ("ifeval", "prompt")):
            info = self.index["tasks"][bench]
            split = "test" if "test" in info["files"] else next(iter(info["files"]))
            rows = pq.read_table(self.root / info["files"][split]).to_pylist()
            out += [rows[i][field] for i in self.index["quick"][bench]]
        for bench in CODE:
            by_id = {p["task_id"]: p for p in self.code_problems(bench)}
            out += [by_id[i]["prompt"] for i in self.index["quick"][bench]]
        return out

    def code_file(self, bench: str) -> Path:
        return self.root / self.index["benchmarks"][bench]["file"]
