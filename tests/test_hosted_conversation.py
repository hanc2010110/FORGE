from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from typing import Literal

from forge_core.conversation_runtime import (
    ConversationRequest,
    build_context_chunk,
    build_prompt_manifest,
    build_provider_manifest,
)
from forge_core.hosted_ai import OpenAIResponsesClient
from forge_core.hosted_conversation import OpenAIConversationProvider


class FakeTransport:
    def __init__(self, draft: dict[str, object]) -> None:
        self.draft = draft
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: Literal["POST"],
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]:
        self.requests.append({"method": method, "url": url, "body": body})
        response = {
            "id": "resp-hosted-1",
            "output_text": json.dumps(self.draft, separators=(",", ":")),
        }
        return 200, {"content-type": "application/json"}, json.dumps(response).encode()


class HostedConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        self.request = ConversationRequest(
            request_id="request-1",
            tenant_id="tenant-1",
            project_id="project-1",
            user_id="user-1",
            user_message="팔 길이를 늘리면 어떤 시험이 필요해?",
            requested_at=self.now,
        )
        self.context = (
            build_context_chunk(
                context_id="cad-1",
                tenant_id="tenant-1",
                project_id="project-1",
                source_kind="cad",
                source_uri="file:///upper-arm.step",
                source_version="rev-b",
                source_captured_at=self.now,
                text="Upper arm length is 500 mm.",
                source_hash="sha256:" + "a" * 64,
            ),
        )
        self.prompt = build_prompt_manifest(
            prompt_id="forge-engineering",
            prompt_version="1",
            system_contract="Use source-bound engineering context.",
            safety_rules=("Do not invent measurements.",),
        )
        self.manifest = build_provider_manifest(
            provider_id="openai",
            model_id="gpt-test",
            model_version="2026-09-01",
            temperature=0,
            tool_allowlist=(),
        )

    def test_provider_builds_strict_request_and_binds_trusted_hashes(self) -> None:
        transport = FakeTransport(
            {
                "disposition": "answer",
                "message": "The geometry source requires a structural review.",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "kind": "fact",
                        "statement": "The current arm length is 500 mm.",
                        "context_refs": ["cad-1"],
                        "confidence": None,
                    }
                ],
                "questions": [],
                "unknowns": [],
            }
        )
        provider = OpenAIConversationProvider(
            client=OpenAIResponsesClient(
                api_key="sk-test", transport=transport, base_url="https://api.test"
            ),
            model_id="gpt-test",
        )

        draft = provider.draft(self.request, self.prompt, self.manifest, self.context)

        self.assertEqual(draft.claims[0].context_refs, ("cad-1",))
        self.assertEqual(draft.context_hashes, (self.context[0].chunk_hash,))
        raw_body = transport.requests[0]["body"]
        if not isinstance(raw_body, bytes):
            raise AssertionError("expected request body bytes")
        request_body = json.loads(raw_body)
        self.assertTrue(request_body["text"]["format"]["strict"])
        self.assertEqual(request_body["temperature"], 0)

    def test_provider_rejects_uncited_or_authoritative_model_output(self) -> None:
        uncited = FakeTransport(
            {
                "disposition": "answer",
                "message": "A design fact.",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "kind": "fact",
                        "statement": "Unsupported.",
                        "context_refs": ["missing"],
                        "confidence": None,
                    }
                ],
                "questions": [],
                "unknowns": [],
            }
        )
        provider = OpenAIConversationProvider(
            client=OpenAIResponsesClient(
                api_key="sk-test", transport=uncited, base_url="https://api.test"
            ),
            model_id="gpt-test",
        )
        with self.assertRaises(ValueError):
            provider.draft(self.request, self.prompt, self.manifest, self.context)

        authoritative = FakeTransport(
            {
                "disposition": "question",
                "message": "READY",
                "claims": [],
                "questions": ["Continue?"],
                "unknowns": [],
            }
        )
        provider = OpenAIConversationProvider(
            client=OpenAIResponsesClient(
                api_key="sk-test", transport=authoritative, base_url="https://api.test"
            ),
            model_id="gpt-test",
        )
        with self.assertRaises(ValueError):
            provider.draft(self.request, self.prompt, self.manifest, self.context)

    def test_provider_rejects_manifest_model_mismatch(self) -> None:
        provider = OpenAIConversationProvider(
            client=OpenAIResponsesClient(
                api_key="sk-test",
                transport=FakeTransport({}),
                base_url="https://api.test",
            ),
            model_id="other-model",
        )
        with self.assertRaises(ValueError):
            provider.draft(self.request, self.prompt, self.manifest, self.context)


if __name__ == "__main__":
    unittest.main()
