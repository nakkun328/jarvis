"""Explicit, optional CPU encoder trial; no application default or API client."""

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

from backend.memory.embedding import EmbeddingSpace

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


class LocalEmbeddingError(RuntimeError):
    """Safe failure without model paths, input text or raw dependency errors."""


@dataclass(frozen=True)
class LocalEmbeddingContract:
    model: str = MODEL
    revision: str = REVISION
    dimension: int = 384
    query_prefix: str = "query: "
    document_prefix: str = "passage: "
    max_tokens: int = 512
    pooling: str = "attention-mask-mean"
    normalization: str = "l2"
    implementation: str = "transformers4.57.6-torch2.9.1-cpu-float32-v1"

    def __post_init__(self):
        if self.model != MODEL or not re.fullmatch(r"[a-f0-9]{40}", self.revision):
            raise ValueError("Use the reviewed model and an immutable revision")
        if (
            self.dimension != 384
            or isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or not 1 <= self.max_tokens <= 512
        ):
            raise ValueError("Unsupported local encoder shape or token limit")
        if self.pooling != "attention-mask-mean" or self.normalization != "l2":
            raise ValueError("Unsupported local encoder pooling or normalization")
        if any(not isinstance(p, str) for p in (self.query_prefix, self.document_prefix)):
            raise ValueError("Encoder prefixes must be strings")
        if self.implementation != "transformers4.57.6-torch2.9.1-cpu-float32-v1":
            raise ValueError("Unsupported local encoder implementation")

    @property
    def space(self):
        digest = hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return EmbeddingSpace(self.model, self.revision + "-" + digest, self.dimension)


class LocalE5Embeddings:
    """Lazy local-files-only CPU model, with bounded batches and explicit roles.

    Only standard Transformers code and safetensors are loaded. The caller
    downloads the fixed snapshot separately. Cache and weights stay outside git.
    """

    def __init__(self, cache_dir: Path, *, contract: LocalEmbeddingContract | None = None):
        self._contract = contract or LocalEmbeddingContract()
        self._cache_dir = Path(cache_dir)
        self._encoder = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def space(self):
        return self._contract.space

    @property
    def contract(self):
        return self._contract

    def _load_encoder(self):
        if version("torch") != "2.9.1" or version("transformers") != "4.57.6":
            raise LocalEmbeddingError("Install the contract's pinned local dependencies")
        import torch
        from transformers import AutoModel, AutoTokenizer

        common = dict(
            revision=self.contract.revision,
            cache_dir=str(self._cache_dir),
            local_files_only=True,
            trust_remote_code=False,
            token=False,
        )
        tokenizer = AutoTokenizer.from_pretrained(self.contract.model, **common)
        model = (
            AutoModel.from_pretrained(
                self.contract.model, **common, use_safetensors=True, dtype=torch.float32
            )
            .to("cpu")
            .eval()
        )
        if model.config.hidden_size != self.contract.dimension:
            raise LocalEmbeddingError("Local encoder dimension differs from its contract")

        def encode(texts):
            batch = tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=self.contract.max_tokens,
                return_tensors="pt",
            )
            with torch.inference_mode():
                outputs = model(**batch)
                mask = batch["attention_mask"]
                hidden = outputs.last_hidden_state.masked_fill(~mask[..., None].bool(), 0.0)
                vectors = hidden.sum(dim=1) / mask.sum(dim=1)[..., None]
                vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
            return vectors.tolist()

        return encode

    def _encode(self, texts, prefix):
        with self._lock:
            if self._closed:
                raise LocalEmbeddingError("Local encoder is closed")
            try:
                if self._encoder is None:
                    self._encoder = self._load_encoder()
                result = []
                for start in range(0, len(texts), 8):
                    batch = texts[start : start + 8]
                    rows = self._encoder([prefix + t for t in batch])
                    if len(rows) != len(batch):
                        raise ValueError("Unexpected vector count")
                    for row in rows:
                        values = self.space.validate(row)
                        if abs(sum(v * v for v in values) - 1.0) > 1e-4:
                            raise ValueError("Local encoder must return unit vectors")
                        result.append(values)
                return tuple(result)
            except Exception as exc:
                raise LocalEmbeddingError("Local encoding could not complete") from exc

    async def _embed(self, texts, prefix):
        if isinstance(texts, (str, bytes)):
            raise ValueError("Texts must be a sequence of nonempty strings")
        batch = tuple(texts)
        if any(not isinstance(t, str) or not t.strip() for t in batch):
            raise ValueError("Texts must be a sequence of nonempty strings")
        if self._closed:
            raise LocalEmbeddingError("Local encoder is closed")
        if not batch:
            return ()
        return await asyncio.to_thread(self._encode, batch, prefix)

    async def embed(self, texts: Sequence[str]):
        return await self._embed(texts, self.contract.document_prefix)

    async def embed_query(self, texts: Sequence[str]):
        return await self._embed(texts, self.contract.query_prefix)

    async def aclose(self):
        def close():
            # A cancelled to_thread call may still be finishing CPU inference.
            # Wait for that call before releasing the model; never race unload.
            with self._lock:
                self._closed = True
                self._encoder = None

        await asyncio.to_thread(close)


def make_provider():
    """Explicit factory for the existing evaluation CLI, using only local cache."""
    path = os.environ.get("JARVIS_LOCAL_MODEL_CACHE")
    if not path:
        raise LocalEmbeddingError("Set an explicit local model cache for this trial")
    return LocalE5Embeddings(Path(path))
