from __future__ import annotations

import unittest
import urllib.request
from collections.abc import Mapping
from types import TracebackType

from forge_core.http_transport import (
    BoundedHTTPTransportError,
    HTTPResponse,
    UrllibHTTPSJSONTransport,
)


class FakeResponse:
    def __init__(self, body: bytes, *, declared_length: str | None = None) -> None:
        self.status = 200
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if declared_length is not None:
            headers["Content-Length"] = declared_length
        self.headers: Mapping[str, str] = headers
        self.body = body

    def read(self, amount: int = -1) -> bytes:
        return self.body[:amount] if amount >= 0 else self.body

    def __enter__(self) -> HTTPResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float) -> HTTPResponse:
        self.requests.append(request)
        return self.response


class HTTPTransportTests(unittest.TestCase):
    def test_https_get_and_post_are_bounded_and_do_not_persist_secrets(self) -> None:
        opener = FakeOpener(FakeResponse(b'{"ok":true}', declared_length="11"))
        transport = UrllibHTTPSJSONTransport(opener=opener)
        status, headers, body = transport.request(
            "POST",
            "https://api.example.com/v1/evidence",
            headers={"Authorization": "Bearer secret-value"},
            body=b"{}",
            timeout_seconds=10,
            max_response_bytes=100,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, b'{"ok":true}')
        self.assertNotIn("secret-value", repr(transport))

    def test_rejects_credentials_redirect_prone_urls_and_oversize_responses(
        self,
    ) -> None:
        transport = UrllibHTTPSJSONTransport(
            opener=FakeOpener(FakeResponse(b"12345", declared_length="5"))
        )
        for url in (
            "http://api.example.com/data",
            "https://user:pass@api.example.com/data",
            "https://api.example.com/data#secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transport.request(
                    "GET",
                    url,
                    headers={},
                    timeout_seconds=10,
                    max_response_bytes=4,
                )

        with self.assertRaisesRegex(
            BoundedHTTPTransportError, "https_response_too_large"
        ):
            transport.request(
                "GET",
                "https://api.example.com/data",
                headers={},
                timeout_seconds=10,
                max_response_bytes=4,
            )


if __name__ == "__main__":
    unittest.main()
