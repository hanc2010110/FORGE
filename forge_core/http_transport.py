from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from types import TracebackType
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

MAX_TRANSPORT_RESPONSE_BYTES = 8_000_000


class BoundedHTTPTransportError(RuntimeError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message


class HTTPResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> HTTPResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class HTTPSOpener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> HTTPResponse: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class UrllibHTTPSJSONTransport:
    """Dependency-free bounded HTTPS transport shared by credential adapters.

    Redirects are rejected so Authorization headers can never be forwarded to a
    different host. The transport stores no request data and never formats secret
    headers into its public errors.
    """

    def __init__(self, *, opener: HTTPSOpener | None = None) -> None:
        self._opener = opener or cast(
            HTTPSOpener, urllib.request.build_opener(_RejectRedirects())
        )

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        _validate_request(method, url, timeout_seconds, max_response_bytes)
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout_seconds) as response:
                return self._read_response(response, max_response_bytes)
        except urllib.error.HTTPError as exc:
            raw = exc.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise BoundedHTTPTransportError(
                    "https_response_too_large",
                    "HTTPS provider error response exceeded the configured bound.",
                ) from exc
            return exc.code, dict(exc.headers.items()), raw
        except (TimeoutError, urllib.error.URLError) as exc:
            raise BoundedHTTPTransportError(
                "https_unreachable",
                "HTTPS provider could not be reached within the configured timeout.",
            ) from exc

    @staticmethod
    def _read_response(
        response: HTTPResponse, max_response_bytes: int
    ) -> tuple[int, dict[str, str], bytes]:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise BoundedHTTPTransportError(
                    "https_invalid_content_length",
                    "HTTPS provider returned an invalid Content-Length.",
                ) from exc
            if declared < 0 or declared > max_response_bytes:
                raise BoundedHTTPTransportError(
                    "https_response_too_large",
                    "HTTPS provider response exceeded the configured bound.",
                )
        raw = response.read(max_response_bytes + 1)
        if len(raw) > max_response_bytes:
            raise BoundedHTTPTransportError(
                "https_response_too_large",
                "HTTPS provider response exceeded the configured bound.",
            )
        return response.status, dict(response.headers), raw


def _validate_request(
    method: str, url: str, timeout_seconds: float, max_response_bytes: int
) -> None:
    if method not in {"GET", "POST"}:
        raise ValueError("HTTPS transport method is not allowlisted")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("HTTPS transport requires a credential-free HTTPS URL")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise ValueError("HTTPS transport timeout is outside supported bounds")
    if max_response_bytes < 1 or max_response_bytes > MAX_TRANSPORT_RESPONSE_BYTES:
        raise ValueError("HTTPS response bound is outside supported limits")


__all__ = [
    "BoundedHTTPTransportError",
    "HTTPSOpener",
    "HTTPResponse",
    "UrllibHTTPSJSONTransport",
]
