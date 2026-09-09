from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from pydantic import Field, model_validator

from forge_core.conversation_runtime import RetrievedContextChunk
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

_TOKEN = re.compile(r"[A-Za-z0-9_가-힣]+")
_ZERO_HASH = "sha256:" + "0" * 64


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class SemanticContextChunk(ContractModel):
    schema_version: str = "1.0.0"
    context: RetrievedContextChunk
    embedding_model: str = Field(min_length=1)
    embedding: tuple[float, ...] = Field(min_length=1, max_length=4096)
    semantic_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def semantic_hash_must_match_payload(self) -> SemanticContextChunk:
        if any(not math.isfinite(value) for value in self.embedding):
            raise ValueError("semantic embedding must be finite")
        if self.semantic_hash != semantic_chunk_hash(
            self.model_copy(update={"semantic_hash": _ZERO_HASH})
        ):
            raise ValueError("semantic chunk hash does not match payload")
        return self


class SemanticSearchResult(ContractModel):
    context: RetrievedContextChunk
    score: float = Field(ge=0, le=1)
    semantic_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def semantic_chunk_hash(chunk: SemanticContextChunk) -> str:
    return canonical_sha256(chunk.model_copy(update={"semantic_hash": _ZERO_HASH}))


class DeterministicEmbeddingProvider:
    """Offline embedding provider for repeatable tests and local demos."""

    def __init__(
        self, *, dimensions: int = 64, model_id: str = "forge-hash-embedding"
    ) -> None:
        if dimensions < 8 or dimensions > 4096:
            raise ValueError("embedding dimensions are outside supported bounds")
        self._dimensions = dimensions
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self._dimensions
        for token in _TOKEN.findall(text.casefold()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


def build_semantic_chunks(
    chunks: Sequence[RetrievedContextChunk],
    *,
    provider: EmbeddingProvider,
) -> tuple[SemanticContextChunk, ...]:
    embeddings = provider.embed_texts([chunk.text for chunk in chunks])
    if len(embeddings) != len(chunks):
        raise ValueError("embedding provider returned the wrong count")
    results: list[SemanticContextChunk] = []
    for chunk, embedding in zip(chunks, embeddings, strict=True):
        if len(embedding) != provider.dimensions:
            raise ValueError("embedding provider returned the wrong dimensions")
        draft = SemanticContextChunk.model_construct(
            schema_version="1.0.0",
            context=chunk,
            embedding_model=provider.model_id,
            embedding=tuple(float(value) for value in embedding),
            semantic_hash=_ZERO_HASH,
        )
        results.append(
            SemanticContextChunk.model_validate(
                draft.model_copy(
                    update={"semantic_hash": semantic_chunk_hash(draft)}
                ).model_dump()
            )
        )
    return tuple(results)


class SemanticRAGIndex:
    def __init__(self, chunks: Sequence[SemanticContextChunk]) -> None:
        ids = [item.context.context_id for item in chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("semantic index chunks must be unique")
        self._chunks = tuple(chunks)

    def retrieve(
        self,
        *,
        tenant_id: str,
        project_id: str,
        query: str,
        provider: EmbeddingProvider,
        limit: int = 6,
    ) -> tuple[SemanticSearchResult, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("semantic retrieval limit must be between 1 and 20")
        query_embedding = provider.embed_texts([query])[0]
        if len(query_embedding) != provider.dimensions:
            raise ValueError("embedding provider returned the wrong dimensions")
        candidates: list[tuple[float, str, SemanticContextChunk]] = []
        for chunk in self._chunks:
            if (
                chunk.context.tenant_id != tenant_id
                or chunk.context.project_id != project_id
            ):
                continue
            if chunk.embedding_model != provider.model_id:
                continue
            if len(chunk.embedding) != provider.dimensions:
                raise ValueError("semantic index contains wrong embedding dimensions")
            score = _cosine(query_embedding, chunk.embedding)
            if score > 0:
                candidates.append((score, chunk.context.context_id, chunk))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            SemanticSearchResult(
                context=chunk.context,
                score=round(score, 8),
                semantic_hash=chunk.semantic_hash,
            )
            for score, _context_id, chunk in candidates[:limit]
        )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(
        0.0,
        min(
            1.0,
            sum(a * b for a, b in zip(left, right, strict=True))
            / (left_norm * right_norm),
        ),
    )


__all__ = [
    "DeterministicEmbeddingProvider",
    "EmbeddingProvider",
    "SemanticContextChunk",
    "SemanticRAGIndex",
    "SemanticSearchResult",
    "build_semantic_chunks",
    "semantic_chunk_hash",
]
