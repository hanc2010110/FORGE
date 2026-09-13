from __future__ import annotations

import json
import unittest
from typing import Literal

from forge_core.hosted_ai import (
    HostedAIError,
    OpenAIEmbeddingProvider,
    OpenAIEmbeddingsClient,
    OpenAIResponsesClient,
)


class FakeTransport:
    def __init__(self, response: object, *, status: int = 200) -> None:
        self.response = response
        self.status = status
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
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return (
            self.status,
            {"content-type": "application/json"},
            json.dumps(self.response).encode(),
        )


class RawTransport(FakeTransport):
    def __init__(self, raw: bytes, *, status: int = 200) -> None:
        super().__init__({}, status=status)
        self.raw = raw

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
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.status, {"content-type": "application/json"}, self.raw


class HostedAITests(unittest.TestCase):
    def test_responses_client_redacts_token_and_extracts_text(self) -> None:
        transport = FakeTransport(
            {
                "id": "resp_1",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": "Use the confirmed arm."}
                        ]
                    }
                ],
            }
        )
        client = OpenAIResponsesClient(
            api_key="sk-test-secret", transport=transport, base_url="https://api.test"
        )
        result = client.create_text_response(
            model="gpt-test",
            input_text="summarize evidence",
            instructions="cite sources",
            metadata={"project_id": "robot-arm"},
        )
        self.assertEqual(result.output_text, "Use the confirmed arm.")
        self.assertEqual(result.response_id, "resp_1")
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://api.test/v1/responses")
        raw_body = request["body"]
        if not isinstance(raw_body, bytes):
            raise AssertionError("expected request body bytes")
        body = json.loads(raw_body.decode())
        self.assertEqual(body["metadata"], {"project_id": "robot-arm"})
        self.assertNotIn("sk-test-secret", repr(request))

    def test_embeddings_client_preserves_input_order(self) -> None:
        transport = FakeTransport(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }
        )
        client = OpenAIEmbeddingsClient(
            api_key="sk-test-secret", transport=transport, base_url="https://api.test"
        )
        embeddings = client.embed_texts("text-embedding-test", ["alpha", "beta"])
        self.assertEqual(embeddings, ((1.0, 0.0), (0.0, 1.0)))

        provider = OpenAIEmbeddingProvider(
            client=client, model_id="text-embedding-test", dimensions=2
        )
        self.assertEqual(provider.model_id, "text-embedding-test")
        self.assertEqual(provider.dimensions, 2)
        self.assertEqual(
            provider.embed_texts(["alpha", "beta"]),
            ((1.0, 0.0), (0.0, 1.0)),
        )

    def test_embedding_provider_rejects_dimension_mismatch(self) -> None:
        client = OpenAIEmbeddingsClient(
            api_key="sk-test-secret",
            transport=FakeTransport({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}),
            base_url="https://api.test",
        )
        provider = OpenAIEmbeddingProvider(
            client=client, model_id="text-embedding-test", dimensions=3
        )
        with self.assertRaisesRegex(
            HostedAIError, "openai_embedding_dimension_mismatch"
        ):
            provider.embed_texts(["alpha"])

    def test_responses_client_accepts_output_text_fallback(self) -> None:
        client = OpenAIResponsesClient(
            api_key="sk-test-secret",
            transport=FakeTransport({"id": "resp_2", "output_text": "Fallback text"}),
            base_url="https://api.test",
        )
        result = client.create_text_response(model="gpt-test", input_text="hello")
        self.assertEqual(result.output_text, "Fallback text")

    def test_clients_fail_closed_on_http_and_invalid_payloads(self) -> None:
        failing = OpenAIResponsesClient(
            api_key="sk-test-secret",
            transport=FakeTransport({"error": {"message": "bad"}}, status=429),
            base_url="https://api.test",
        )
        with self.assertRaisesRegex(HostedAIError, "openai_rate_limited"):
            failing.create_text_response(model="gpt-test", input_text="hello")

        invalid_embeddings = OpenAIEmbeddingsClient(
            api_key="sk-test-secret",
            transport=FakeTransport({"data": [{"index": 0, "embedding": ["x"]}]}),
            base_url="https://api.test",
        )
        with self.assertRaisesRegex(HostedAIError, "openai_invalid_response"):
            invalid_embeddings.embed_texts("text-embedding-test", ["alpha"])

        unauthorized = OpenAIResponsesClient(
            api_key="sk-test-secret",
            transport=FakeTransport({"error": {"message": "bad"}}, status=401),
            base_url="https://api.test",
        )
        with self.assertRaisesRegex(HostedAIError, "openai_unauthorized"):
            unauthorized.create_text_response(model="gpt-test", input_text="hello")

        bad_json = OpenAIResponsesClient(
            api_key="sk-test-secret",
            transport=RawTransport(b"{not-json"),
            base_url="https://api.test",
        )
        with self.assertRaisesRegex(HostedAIError, "openai_invalid_response"):
            bad_json.create_text_response(model="gpt-test", input_text="hello")

    def test_secret_and_boundary_validation(self) -> None:
        with self.assertRaises(ValueError):
            OpenAIResponsesClient(api_key="line\nbreak", transport=FakeTransport({}))
        with self.assertRaises(ValueError):
            OpenAIEmbeddingsClient(
                api_key="sk", transport=FakeTransport({})
            ).embed_texts("model", [])
        with self.assertRaises(ValueError):
            OpenAIResponsesClient(
                api_key="sk", transport=FakeTransport({}), base_url="http://api.test"
            )
        with self.assertRaises(ValueError):
            OpenAIResponsesClient(
                api_key="sk", transport=FakeTransport({}), timeout_seconds=0
            )
        with self.assertRaises(ValueError):
            OpenAIResponsesClient(
                api_key="sk", transport=FakeTransport({})
            ).create_text_response(model="bad model", input_text="hello")
        with self.assertRaises(ValueError):
            OpenAIEmbeddingProvider(
                client=OpenAIEmbeddingsClient(
                    api_key="sk", transport=FakeTransport({})
                ),
                model_id="embedding-model",
                dimensions=0,
            )


if __name__ == "__main__":
    unittest.main()
