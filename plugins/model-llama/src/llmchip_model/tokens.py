"""Tokenizers behind one interface: Hugging Face tokenizer.json, or llama2.c's tokenizer."""

from __future__ import annotations

from pathlib import Path

from . import BOS, Tokenizer


class TextTokenizer:
    """encode/decode/piece for either tokenizer kind."""

    def __init__(self, path: Path, kind: str, vocab_size: int) -> None:
        self.kind = kind
        if kind == "hf":
            from tokenizers import Tokenizer as HfTokenizer

            self._hf = HfTokenizer.from_file(str(path))
        else:
            self._l2 = Tokenizer(path.read_bytes(), vocab_size)

    def encodable(self, text: str) -> bool:
        """Whether every token of `text` is in the vocabulary (byte fallback can exceed a tiny vocabulary)."""
        if self.kind == "hf":
            return True
        try:
            return all(t < len(self._l2.vocab) for t in self._l2.encode(text))
        except IndexError:  # byte fallback beyond the vocabulary, raised while merging
            return False

    def encode(self, text: str) -> list[int]:
        return self._hf.encode(text).ids if self.kind == "hf" else self._l2.encode(text)

    def decode(self, ids: list[int]) -> str:
        if self.kind == "hf":
            return self._hf.decode(ids)
        # llama2.c decodes each token relative to the one before it.
        return self._l2.decode(ids if ids[:1] == [BOS] else [BOS, *ids])

    def continuation(self, prompt: list[int], generated: list[int]) -> str:
        """The text the generated tokens add after the prompt (spacing depends on what precedes them)."""
        before, after = self.decode(prompt), self.decode(prompt + generated)
        return after[len(before) :] if after.startswith(before) else self.decode(generated)

    def piece(self, token: int) -> str:
        """How one token reads on its own (spaces and newlines made visible)."""
        text = self._hf.decode([token]) if self.kind == "hf" else self._l2.vocab[token].decode("utf-8", "replace")
        return text.replace(" ", "␣").replace("\n", "↵") or f"<{token}>"
