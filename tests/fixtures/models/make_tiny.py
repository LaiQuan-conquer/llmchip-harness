"""Generates tests/fixtures/models/tiny-qwen3: a tiny Qwen3 model with random weights (fixed seed).

It has the real file layout of a downloaded Hugging Face model (config.json,
bf16 model.safetensors, tokenizer.json), so the import, census, graph recovery
and comparison with transformers can be tested end to end in seconds.

Run with `.venv/bin/python tests/fixtures/models/make_tiny.py`.
"""

from pathlib import Path

import torch
import transformers
from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers

OUT = Path(__file__).parent / "tiny-qwen3"
TEXT = [
    "The capital of France is Paris.",
    "Once upon a time, there was a little dog.",
    "def fibonacci(n): return n if n < 2 else fibonacci(n - 1) + fibonacci(n - 2)",
    "1, 2, 3, 4, 5, 6, 7, 8, 9, 10",
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    tok = Tokenizer(models.BPE(unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.Metaspace()
    tok.decoder = decoders.Metaspace()
    tok.train_from_iterator(TEXT, trainers.BpeTrainer(vocab_size=120, special_tokens=["<unk>", "<|endoftext|>"]))
    tok.save(str(OUT / "tokenizer.json"))
    config = transformers.Qwen3Config(
        vocab_size=128,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=8,
        max_position_embeddings=64,
        rope_theta=10000.0,
        tie_word_embeddings=True,
    )
    torch.manual_seed(0)
    model = transformers.AutoModelForCausalLM.from_config(config).to(torch.bfloat16)
    model.save_pretrained(OUT)


if __name__ == "__main__":
    main()
