"""The harness's tokenizer: encode/decode by the kind the chip declares (llama2.c or Hugging Face)."""

from __future__ import annotations

from llmchip_model.tokens import TextTokenizer

from .interface import ChipError
from .manifest import Manifest


class HarnessTokenizer:
    def __init__(self, m: Manifest) -> None:
        tok = m.data["tokenizer"]
        self.vocab = int(m.data["model"]["vocab"])
        self.kind = tok["kind"]
        self.bos: int | None = tok.get("bos")
        self.stop: set[int] = set(tok.get("stop", []))
        self._t = TextTokenizer(m.path(tok["path"]), self.kind, self.vocab)
        self._unk: int | None = None
        if self.kind == "hf":
            from tokenizers import Tokenizer

            hf = Tokenizer.from_file(str(m.path(tok["path"])))
            unk = getattr(hf.model, "unk_token", None)
            self._unk = hf.token_to_id(unk) if unk else None

    def encode(self, text: str) -> list[int]:
        """Token ids of `text`; `invalid_input` when the vocabulary cannot represent it."""
        try:
            ok = self._t.encodable(text)
            ids = list(self._t.encode(text)) if ok else []
        except Exception as e:
            raise ChipError("invalid_input", f"the tokenizer failed on {text!r}: {e}") from e
        if not ok:
            raise ChipError("invalid_input", f"the tokenizer cannot encode {text!r} within {self.vocab} tokens")
        if self.bos is not None and ids[:1] != [self.bos]:
            ids.insert(0, self.bos)
        if self._unk is not None and self._unk in ids:
            raise ChipError(
                "invalid_input", f"the tokenizer has no token for part of {text!r} (it became the unknown token)"
            )
        bad = [t for t in ids if not 0 <= t < self.vocab]
        if bad:
            raise ChipError("invalid_input", f"{text!r} encodes to tokens outside the vocabulary: {bad[:5]}")
        return ids

    def decode(self, ids: list[int]) -> str:
        return self._t.decode(ids)

    def continuation(self, prompt: list[int], generated: list[int]) -> str:
        return self._t.continuation(prompt, generated)

    def roundtrip(self, text: str) -> bool:
        """encode -> decode gives `text` back (llama2.c drops the dummy-prefix space, as run.c)."""
        ids = self.encode(text)
        return self.decode(ids).strip() == text.strip()
