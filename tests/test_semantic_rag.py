from __future__ import annotations

import unittest
from datetime import UTC, datetime

from forge_core.local_rag import build_knowledge_source, ingest_knowledge_source
from forge_core.semantic_rag import (
    DeterministicEmbeddingProvider,
    SemanticRAGIndex,
    build_semantic_chunks,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class SemanticRAGTests(unittest.TestCase):
    def test_project_scoped_semantic_retrieval_keeps_lineage(self) -> None:
        source = build_knowledge_source(
            source_id="datasheet-1",
            tenant_id="org-1",
            project_id="robot-arm",
            source_kind="document",
            source_uri="file:///datasheet.md",
            source_version="rev-a",
            captured_at=NOW,
            text="Upper arm voltage limit is 24 V. Payload test requires HIL evidence.",
        )
        ingestion = ingest_knowledge_source(source, chunk_chars=128)
        provider = DeterministicEmbeddingProvider(dimensions=32)
        chunks = build_semantic_chunks(ingestion.chunks, provider=provider)
        index = SemanticRAGIndex(chunks)

        results = index.retrieve(
            tenant_id="org-1",
            project_id="robot-arm",
            query="What evidence is needed for payload voltage changes?",
            provider=provider,
            limit=3,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].context.context_id, "datasheet-1:chunk:0001")
        self.assertEqual(results[0].context.source_hash, source.source_hash)
        self.assertGreater(results[0].score, 0)

        other_project = index.retrieve(
            tenant_id="org-1",
            project_id="other",
            query="voltage",
            provider=provider,
        )
        self.assertEqual(other_project, ())

    def test_semantic_chunks_are_hash_bound_and_dimension_checked(self) -> None:
        source = build_knowledge_source(
            source_id="doc-2",
            tenant_id="org-1",
            project_id="robot-arm",
            source_kind="document",
            source_uri="file:///doc.md",
            source_version="rev-a",
            captured_at=NOW,
            text="Torque margin.",
        )
        provider = DeterministicEmbeddingProvider(dimensions=16)
        chunks = build_semantic_chunks(
            ingest_knowledge_source(source, chunk_chars=128).chunks,
            provider=provider,
        )
        self.assertEqual(chunks[0].embedding_model, provider.model_id)

        with self.assertRaises(ValueError):
            SemanticRAGIndex(chunks).retrieve(
                tenant_id="org-1",
                project_id="robot-arm",
                query="torque",
                provider=DeterministicEmbeddingProvider(dimensions=8),
            )


if __name__ == "__main__":
    unittest.main()
