from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol, cast

from pydantic import Field

from forge_core.models import ContractModel

MAX_AI_REQUEST_BYTES = 128_000
MAX_AI_RESPONSE_BYTES = 1_000_000
MAX_EMBEDDING_INPUTS = 128
MAX_EMBEDDING_TEXT_BYTES = 64_000
DEFAULT_TIMEOUT_SECONDS = 20.0
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class RedactedHeaders(dict[str, str]):
    def __repr__(self) -> str:
        redacted = {
            key: "<redacted>" if key.casefold() == "authorization" else value
            for key, value in self.items()
        }
        return repr(redacted)


class HostedAIError(RuntimeError):
    def __init__(self, code: str, public_message: str, *, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message
        self.status = status


class HostedAITransport(Protocol):
    def request(
        self,
        method: Literal["POST"],
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]: ...


class HostedTextResponse(ContractModel):
    response_id: str = Field(min_length=1)
    output_text: str = Field(min_length=1)


def _validate_api_key(api_key: str) -> str:
    if not api_key or "\r" in api_key or "\n" in api_key:
        raise ValueError("API key boundary is invalid")
    return api_key


def _validate_base_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if not trimmed.startswith("https://"):
        raise ValueError("hosted AI base URL must be https")
    if "\r" in trimmed or "\n" in trimmed:
        raise ValueError("hosted AI base URL is invalid")
    return trimmed


def _validate_model(model: str) -> str:
    if _MODEL_ID.fullmatch(model) is None:
        raise ValueError("model id is invalid")
    return model


def _json_body(payload: Mapping[str, object]) -> bytes:
    raw = json.dumps(
        payload, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(raw) > MAX_AI_REQUEST_BYTES:
        raise ValueError("hosted AI request exceeds byte bound")
    return raw


def _decode_json(status: int, raw: bytes) -> object:
    if status == 429:
        raise HostedAIError(
            "openai_rate_limited",
            "Hosted AI provider rate limited the request.",
            status=429,
        )
    if status in {401, 403}:
        raise HostedAIError(
            "openai_unauthorized",
            "Hosted AI credentials were rejected.",
            status=status,
        )
    if status < 200 or status >= 300:
        raise HostedAIError(
            f"openai_http_{status}",
            "Hosted AI provider returned an error.",
            status=502,
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostedAIError(
            "openai_invalid_response",
            "Hosted AI provider returned invalid JSON.",
        ) from exc


class _HostedAIClient:
    def __init__(
        self,
        *,
        api_key: str,
        transport: HostedAITransport,
        base_url: str = "https://api.openai.com",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("hosted AI timeout must be between 0 and 60 seconds")
        self._api_key = _validate_api_key(api_key)
        self._transport = transport
        self._base_url = _validate_base_url(base_url)
        self._timeout_seconds = timeout_seconds

    def _post(self, path: str, payload: Mapping[str, object]) -> object:
        status, _headers, raw = self._transport.request(
            "POST",
            f"{self._base_url}{path}",
            headers=RedactedHeaders(
                {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "FORGE-engineering-agent/0.1",
                }
            ),
            body=_json_body(payload),
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_AI_RESPONSE_BYTES,
        )
        if len(raw) > MAX_AI_RESPONSE_BYTES:
            raise HostedAIError(
                "openai_response_too_large",
                "Hosted AI provider returned more data than FORGE accepts.",
            )
        return _decode_json(status, raw)


class OpenAIResponsesClient(_HostedAIClient):
    def create_text_response(
        self,
        *,
        model: str,
        input_text: str,
        instructions: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> HostedTextResponse:
        if not input_text or len(input_text.encode("utf-8")) > MAX_AI_REQUEST_BYTES:
            raise ValueError("response input is outside supported bounds")
        payload: dict[str, object] = {
            "model": _validate_model(model),
            "input": input_text,
        }
        if instructions is not None:
            payload["instructions"] = instructions
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        decoded = self._post("/v1/responses", payload)
        if not isinstance(decoded, dict):
            raise HostedAIError("openai_invalid_response", "Invalid response payload.")
        response_id = decoded.get("id")
        text = _extract_response_text(decoded)
        if not isinstance(response_id, str) or not response_id or not text:
            raise HostedAIError("openai_invalid_response", "Invalid response payload.")
        return HostedTextResponse(response_id=response_id, output_text=text)


class OpenAIEmbeddingsClient(_HostedAIClient):
    def embed_texts(
        self, model: str, texts: Sequence[str]
    ) -> tuple[tuple[float, ...], ...]:
        if not texts or len(texts) > MAX_EMBEDDING_INPUTS:
            raise ValueError("embedding input count is outside supported bounds")
        for text in texts:
            if not text or len(text.encode("utf-8")) > MAX_EMBEDDING_TEXT_BYTES:
                raise ValueError("embedding input text is outside supported bounds")
        decoded = self._post(
            "/v1/embeddings",
            {"model": _validate_model(model), "input": list(texts)},
        )
        if not isinstance(decoded, dict) or not isinstance(decoded.get("data"), list):
            raise HostedAIError("openai_invalid_response", "Invalid embedding payload.")
        ordered: dict[int, tuple[float, ...]] = {}
        for item in decoded["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                raise HostedAIError(
                    "openai_invalid_response", "Invalid embedding payload."
                )
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise HostedAIError(
                    "openai_invalid_response", "Invalid embedding payload."
                )
            values = tuple(_finite_float(value) for value in embedding)
            ordered[cast(int, item["index"])] = values
        if set(ordered) != set(range(len(texts))):
            raise HostedAIError(
                "openai_invalid_response", "Embedding indexes mismatch."
            )
        return tuple(ordered[index] for index in range(len(texts)))


def _finite_float(value: object) -> float:
    if not isinstance(value, int | float):
        raise HostedAIError("openai_invalid_response", "Embedding value is invalid.")
    as_float = float(value)
    if not math.isfinite(as_float):
        raise HostedAIError("openai_invalid_response", "Embedding value is invalid.")
    return as_float


def _extract_response_text(payload: Mapping[str, object]) -> str:
    texts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    texts.append(part["text"])
    if not texts and isinstance(payload.get("output_text"), str):
        texts.append(cast(str, payload["output_text"]))
    return "\n".join(text for text in texts if text).strip()


__all__ = [
    "HostedAIError",
    "HostedAITransport",
    "HostedTextResponse",
    "OpenAIEmbeddingsClient",
    "OpenAIResponsesClient",
]
