import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Model, Tokenizer, load_ir, load_llama2c, save_ir, structure_report

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "models" / "stories260K"
RUN_C = Path(__file__).parents[1] / "upstream" / "run.c"


@pytest.fixture(scope="module")
def stories():
    cfg, w = load_llama2c((FIXTURE / "stories260K.bin").read_bytes())
    tok = Tokenizer((FIXTURE / "tok512.bin").read_bytes(), cfg.vocab_size)
    return cfg, w, tok


def test_loads_stories260k_structure(stories):
    cfg, w, tok = stories
    shape = (cfg.dim, cfg.hidden_dim, cfg.n_layers, cfg.n_heads, cfg.n_kv_heads, cfg.vocab_size, cfg.seq_len)
    assert shape == (64, 172, 5, 8, 4, 512, 512)
    assert cfg.shared_classifier
    assert structure_report(cfg, w, "MIT")["parameters"]["total"] == 260_032
    assert len(tok.vocab) == 512


def test_ir_round_trip(stories, tmp_path):
    cfg, w, _ = stories
    path = tmp_path / "m.npz"
    path.write_bytes(save_ir(cfg, w))
    cfg2, w2 = load_ir(path)
    assert cfg2 == cfg
    for k in w:
        np.testing.assert_array_equal(w2[k], w[k])


def test_tokenizer_round_trip(stories):
    _, _, tok = stories
    ids = tok.encode("Once upon a time, Lily saw a big dog.")
    assert ids[0] == 1
    assert tok.decode(ids) == "Once upon a time, Lily saw a big dog."


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_l0_matches_compiled_run_c(stories, tmp_path):
    cfg, w, tok = stories
    binary = tmp_path / "run"
    subprocess.run(["gcc", "-O2", "-o", str(binary), str(RUN_C), "-lm"], check=True)
    model = Model(cfg, w)
    for prompt in ["Once upon a time", "The little dog", "Tom and Lily went to the park"]:
        steps = 48
        args = [str(binary), str(FIXTURE / "stories260K.bin"), "-z", str(FIXTURE / "tok512.bin")]
        out = subprocess.run(
            [*args, "-t", "0", "-n", str(steps), "-i", prompt], capture_output=True, text=True, check=True
        )
        text = out.stdout.rstrip("\n")
        assert tok.decode(model.generate(tok.encode(prompt), steps)) == text
