from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlencode

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.immutable_json import freeze_json_mapping, thaw_json
from forge_core.models import ContractModel

MAX_EXTERNAL_API_RESPONSE_BYTES = 1_000_000
DEFAULT_EXTERNAL_API_TIMEOUT_SECONDS = 12.0
MAX_EXTERNAL_IDENTIFIER_LENGTH = 512

_SAFE_ENTITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}")
_SAFE_COLLECTION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_HEX_SHA = re.compile(r"[A-Fa-f0-9]{7,64}")
_ZERO_HASH = "sha256:" + "0" * 64

ExternalPayload = Mapping[str, object] | tuple[Mapping[str, object], ...]
PaginationState = Literal["not_applicable", "complete", "partial", "unknown"]


class RedactedHeaders(dict[str, str]):
    def __repr__(self) -> str:
        secret_keys = {"authorization", "private-token", "x-api-key"}
        redacted = {
            key: "<redacted>"
            if key.casefold() in secret_keys or "key" in key.casefold()
            else value
            for key, value in self.items()
        }
        return repr(redacted)


class ExternalAPIError(RuntimeError):
    def __init__(self, code: str, public_message: str, *, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message
        self.status = status


class ReadOnlyHTTPTransport(Protocol):
    def request(
        self,
        method: Literal["GET"],
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]: ...


class ExternalReadEvidenceEnvelope(ContractModel):
    """Immutable provenance receipt for a read-only external API response.

    This is connector evidence, not release evidence. A trusted normalization layer
    must still map the payload to FORGE's domain evidence contracts.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    provider_id: str = Field(min_length=1, max_length=64)
    resource_kind: str = Field(min_length=1, max_length=128)
    source_url: str = Field(min_length=1, max_length=4096)
    captured_at: datetime
    source_etag: str | None = Field(default=None, max_length=1024)
    source_last_modified: str | None = Field(default=None, max_length=1024)
    pagination_state: PaginationState
    next_page_reference: str | None = Field(default=None, max_length=2048)
    record_count: int = Field(ge=0)
    payload: ExternalPayload
    response_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    envelope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    release_evidence: Literal[False] = False

    @field_validator("provider_id", "resource_kind")
    @classmethod
    def identifiers_must_be_safe(cls, value: str) -> str:
        if _SAFE_ENTITY_NAME.fullmatch(value) is None:
            raise ValueError("external evidence identifier is invalid")
        return value

    @field_validator("source_url")
    @classmethod
    def source_url_must_be_https(cls, value: str) -> str:
        if not value.startswith("https://") or "\r" in value or "\n" in value:
            raise ValueError("external evidence source URL must be https")
        return value

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("external evidence capture time must be UTC")
        return value

    @field_validator("payload", mode="after")
    @classmethod
    def payload_must_be_deeply_immutable(
        cls, value: ExternalPayload
    ) -> ExternalPayload:
        if isinstance(value, Mapping):
            return freeze_json_mapping(value)
        return tuple(freeze_json_mapping(item) for item in value)

    @field_serializer("payload")
    def serialize_payload(self, value: ExternalPayload) -> object:
        return thaw_json(value)

    @model_validator(mode="after")
    def hashes_and_pagination_must_match(self) -> ExternalReadEvidenceEnvelope:
        if self.payload_sha256 != _canonical_payload_hash(self.payload):
            raise ValueError("external evidence payload hash does not match")
        expected_hash = canonical_sha256(
            self.model_copy(update={"envelope_hash": _ZERO_HASH})
        )
        if self.envelope_hash != expected_hash:
            raise ValueError("external evidence envelope hash does not match")
        if self.pagination_state != "partial" and self.next_page_reference is not None:
            raise ValueError("next page reference requires partial pagination")
        return self

    def object_payload(self) -> dict[str, object]:
        if not isinstance(self.payload, Mapping):
            raise ExternalAPIError(
                "external_api_payload_shape", "Expected an object payload."
            )
        return cast(dict[str, object], thaw_json(self.payload))

    def records_payload(self) -> tuple[dict[str, object], ...]:
        if not isinstance(self.payload, tuple):
            raise ExternalAPIError(
                "external_api_payload_shape", "Expected a record-list payload."
            )
        return tuple(cast(dict[str, object], thaw_json(item)) for item in self.payload)


@dataclass(frozen=True)
class _ExternalRead:
    source_url: str
    captured_at: datetime
    response_headers: Mapping[str, str]
    raw: bytes
    decoded: object


@dataclass(frozen=True)
class UnavailableIntegrationDescriptor:
    provider_id: str
    name: str
    reason: str
    required_from_vendor: tuple[str, ...]

    def model_dump(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "available": False,
            "reason": self.reason,
            "required_from_vendor": list(self.required_from_vendor),
        }


def solidworks_3dexperience_descriptor() -> UnavailableIntegrationDescriptor:
    return UnavailableIntegrationDescriptor(
        provider_id="solidworks_3dexperience",
        name="SOLIDWORKS 3DEXPERIENCE",
        reason=(
            "3DEXPERIENCE endpoints and object contracts depend on tenant apps, "
            "licenses, and platform version; FORGE does not guess those contracts."
        ),
        required_from_vendor=(
            "tenant base URL",
            "OAuth application credentials",
            "3DEXPERIENCE app/service endpoint documentation for the tenant",
            "read-only role for CAD/BOM/revision objects",
        ),
    )


def teamcenter_descriptor() -> UnavailableIntegrationDescriptor:
    return UnavailableIntegrationDescriptor(
        provider_id="teamcenter",
        name="Siemens Teamcenter",
        reason=(
            "Teamcenter REST/SOA surfaces are deployment and version specific; "
            "FORGE needs the installed server contract before enabling a driver."
        ),
        required_from_vendor=(
            "Teamcenter version",
            "enabled REST or SOA API contract",
            "tenant/base URL",
            "read-only service account",
        ),
    )


def _base_url(value: str) -> str:
    trimmed = value.rstrip("/")
    if not trimmed.startswith("https://") or "\r" in trimmed or "\n" in trimmed:
        raise ValueError("external API base URL must be https")
    if len(trimmed) > 2048:
        raise ValueError("external API base URL is too long")
    return trimmed


def _secret(value: str, name: str) -> str:
    if not value or "\r" in value or "\n" in value or len(value) > 4096:
        raise ValueError(f"{name} boundary is invalid")
    return value


def _path_value(value: str, name: str, *, allow_slash: bool = False) -> str:
    if (
        not value
        or "\r" in value
        or "\n" in value
        or len(value) > MAX_EXTERNAL_IDENTIFIER_LENGTH
        or value.startswith("/")
        or value.endswith("/")
        or ".." in value.split("/")
    ):
        raise ValueError(f"{name} boundary is invalid")
    if not allow_slash and "/" in value:
        raise ValueError(f"{name} boundary is invalid")
    return value


def _entity_name(value: str, name: str) -> str:
    checked = _path_value(value, name)
    if _SAFE_ENTITY_NAME.fullmatch(checked) is None:
        raise ValueError(f"{name} boundary is invalid")
    return checked


def _collection(value: str, name: str) -> str:
    if _SAFE_COLLECTION_NAME.fullmatch(value) is None:
        raise ValueError(f"{name} boundary is invalid")
    return value


def _git_sha(value: str, name: str) -> str:
    checked = _entity_name(value, name)
    if _HEX_SHA.fullmatch(checked) is None:
        raise ValueError(f"{name} must be a commit SHA")
    return checked


def _decode(status: int, raw: bytes) -> object:
    if status < 200 or status >= 300:
        raise ExternalAPIError(
            f"external_api_http_{status}",
            "External read-only adapter returned an error.",
            status=502,
        )
    if len(raw) > MAX_EXTERNAL_API_RESPONSE_BYTES:
        raise ExternalAPIError(
            "external_api_response_too_large",
            "External adapter returned more data than FORGE accepts.",
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalAPIError(
            "external_api_invalid_response",
            "External adapter returned invalid JSON.",
        ) from exc
    if not isinstance(decoded, dict | list):
        raise ExternalAPIError(
            "external_api_invalid_response",
            "External adapter returned invalid JSON.",
        )
    return decoded


def _expect_dict(decoded: object) -> dict[str, object]:
    if not isinstance(decoded, dict):
        raise ExternalAPIError("external_api_invalid_response", "Expected object.")
    return dict(decoded)


def _expect_list(decoded: object) -> list[dict[str, object]]:
    if not isinstance(decoded, list):
        raise ExternalAPIError("external_api_invalid_response", "Expected list.")
    if any(not isinstance(item, dict) for item in decoded):
        raise ExternalAPIError(
            "external_api_invalid_response", "Expected a list of objects."
        )
    return [dict(item) for item in decoded]


class _ReadOnlyClient:
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        transport: ReadOnlyHTTPTransport,
        timeout_seconds: float = DEFAULT_EXTERNAL_API_TIMEOUT_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("external API timeout is outside supported bounds")
        self._provider_id = _entity_name(provider_id, "provider id")
        self._base_url = _base_url(base_url)
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))

    def _get(self, path: str, headers: Mapping[str, str]) -> _ExternalRead:
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("external API path boundary is invalid")
        source_url = f"{self._base_url}{path}"
        status, response_headers, raw = self._transport.request(
            "GET",
            source_url,
            headers=RedactedHeaders(headers),
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_EXTERNAL_API_RESPONSE_BYTES,
        )
        captured_at = self._now()
        if captured_at.tzinfo is None or captured_at.utcoffset() != timedelta(0):
            raise ValueError("external API clock must return timezone-aware UTC")
        return _ExternalRead(
            source_url=source_url,
            captured_at=captured_at,
            response_headers=dict(response_headers),
            raw=raw,
            decoded=_decode(status, raw),
        )

    def _dict_evidence(
        self,
        read: _ExternalRead,
        *,
        resource_kind: str,
    ) -> ExternalReadEvidenceEnvelope:
        return self._evidence(
            read,
            resource_kind=resource_kind,
            payload=_expect_dict(read.decoded),
            pagination_state="not_applicable",
            next_page_reference=None,
        )

    def _list_evidence(
        self,
        read: _ExternalRead,
        *,
        resource_kind: str,
        payload: list[dict[str, object]] | None = None,
    ) -> ExternalReadEvidenceEnvelope:
        pagination_state, next_page_reference = _pagination(read.response_headers)
        return self._evidence(
            read,
            resource_kind=resource_kind,
            payload=tuple(
                payload if payload is not None else _expect_list(read.decoded)
            ),
            pagination_state=pagination_state,
            next_page_reference=next_page_reference,
        )

    def _evidence(
        self,
        read: _ExternalRead,
        *,
        resource_kind: str,
        payload: ExternalPayload,
        pagination_state: PaginationState,
        next_page_reference: str | None,
    ) -> ExternalReadEvidenceEnvelope:
        normalized_headers = {
            key.casefold(): value for key, value in read.response_headers.items()
        }
        draft = ExternalReadEvidenceEnvelope.model_construct(
            schema_version="1.0.0",
            provider_id=self._provider_id,
            resource_kind=_entity_name(resource_kind, "resource kind"),
            source_url=read.source_url,
            captured_at=read.captured_at,
            source_etag=normalized_headers.get("etag"),
            source_last_modified=normalized_headers.get("last-modified"),
            pagination_state=pagination_state,
            next_page_reference=next_page_reference,
            record_count=len(payload) if isinstance(payload, tuple) else 1,
            payload=payload,
            response_sha256="sha256:" + hashlib.sha256(read.raw).hexdigest(),
            payload_sha256=_canonical_payload_hash(payload),
            envelope_hash=_ZERO_HASH,
            release_evidence=False,
        )
        return ExternalReadEvidenceEnvelope.model_validate(
            draft.model_copy(
                update={"envelope_hash": canonical_sha256(draft)}
            ).model_dump()
        )


def _pagination(headers: Mapping[str, str]) -> tuple[PaginationState, str | None]:
    normalized = {key.casefold(): value for key, value in headers.items()}
    if "x-next-page" in normalized:
        next_page = normalized["x-next-page"].strip()
        return ("partial", next_page) if next_page else ("complete", None)
    link = normalized.get("link")
    if link is None:
        return "unknown", None
    for entry in link.split(","):
        if 'rel="next"' in entry or "rel=next" in entry:
            reference = entry.split(";", maxsplit=1)[0].strip().strip("<>")
            return "partial", "sha256:" + hashlib.sha256(reference.encode()).hexdigest()
    return "complete", None


def _canonical_payload_hash(payload: ExternalPayload) -> str:
    encoded = json.dumps(
        thaw_json(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class GitLabReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        project_path: str,
        token: str,
        transport: ReadOnlyHTTPTransport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_id="gitlab", base_url=base_url, transport=transport, now=now
        )
        self._project_path = quote(
            _path_value(project_path, "GitLab project path", allow_slash=True),
            safe="",
        )
        self._token = _secret(token, "GitLab token")

    def get_commit(self, *, sha: str) -> ExternalReadEvidenceEnvelope:
        read = self._get(
            f"/api/v4/projects/{self._project_path}/repository/commits/"
            f"{quote(_git_sha(sha, 'GitLab commit SHA'), safe='')}",
            {"Private-Token": self._token},
        )
        return self._dict_evidence(read, resource_kind="commit")

    def list_merge_requests(
        self, *, state: Literal["opened", "closed", "locked", "merged"] | None = None
    ) -> ExternalReadEvidenceEnvelope:
        query = f"?{urlencode({'state': state})}" if state else ""
        read = self._get(
            f"/api/v4/projects/{self._project_path}/merge_requests{query}",
            {"Private-Token": self._token},
        )
        return self._list_evidence(read, resource_kind="merge_requests")

    def list_pipelines(self, *, ref: str | None = None) -> ExternalReadEvidenceEnvelope:
        query = f"?{urlencode({'ref': _path_value(ref, 'GitLab ref')})}" if ref else ""
        read = self._get(
            f"/api/v4/projects/{self._project_path}/pipelines{query}",
            {"Private-Token": self._token},
        )
        return self._list_evidence(read, resource_kind="pipelines")

    def list_pipeline_jobs(self, *, pipeline_id: int) -> ExternalReadEvidenceEnvelope:
        if pipeline_id < 1:
            raise ValueError("GitLab pipeline id must be positive")
        read = self._get(
            f"/api/v4/projects/{self._project_path}/pipelines/{pipeline_id}/jobs",
            {"Private-Token": self._token},
        )
        return self._list_evidence(read, resource_kind="pipeline_jobs")


class JenkinsReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        job_path: str,
        username: str,
        api_token: str,
        transport: ReadOnlyHTTPTransport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_id="jenkins", base_url=base_url, transport=transport, now=now
        )
        self._job_path = "/".join(
            f"job/{quote(part, safe='')}"
            for part in _path_value(
                job_path, "Jenkins job path", allow_slash=True
            ).split("/")
        )
        credential = (
            f"{_secret(username, 'Jenkins username')}:"
            f"{_secret(api_token, 'Jenkins token')}"
        )
        self._authorization = "Basic " + base64.b64encode(credential.encode()).decode()

    def get_job(self) -> ExternalReadEvidenceEnvelope:
        read = self._get(f"/{self._job_path}/api/json", self._headers())
        return self._dict_evidence(read, resource_kind="job")

    def get_build(self, *, build_number: int) -> ExternalReadEvidenceEnvelope:
        if build_number < 1:
            raise ValueError("Jenkins build number must be positive")
        read = self._get(
            f"/{self._job_path}/{build_number}/api/json",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="build")

    def get_last_build(self) -> ExternalReadEvidenceEnvelope:
        read = self._get(f"/{self._job_path}/lastBuild/api/json", self._headers())
        return self._dict_evidence(read, resource_kind="last_build")

    def get_test_report(self, *, build_number: int) -> ExternalReadEvidenceEnvelope:
        if build_number < 1:
            raise ValueError("Jenkins build number must be positive")
        read = self._get(
            f"/{self._job_path}/{build_number}/testReport/api/json",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="test_report")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class OnshapeReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_id="onshape", base_url=base_url, transport=transport, now=now
        )
        self._authorization = f"Bearer {_secret(access_token, 'Onshape OAuth token')}"

    def get_document(self, *, document_id: str) -> ExternalReadEvidenceEnvelope:
        safe_document_id = quote(
            _entity_name(document_id, "Onshape document id"), safe=""
        )
        read = self._get(
            f"/api/documents/{safe_document_id}",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="document")

    def list_document_versions(
        self, *, document_id: str
    ) -> ExternalReadEvidenceEnvelope:
        safe_document_id = quote(
            _entity_name(document_id, "Onshape document id"), safe=""
        )
        read = self._get(
            f"/api/documents/{safe_document_id}/versions",
            self._headers(),
        )
        return self._list_evidence(read, resource_kind="document_versions")

    def get_assembly_definition(
        self,
        *,
        document_id: str,
        workspace_or_version: Literal["w", "v", "m"],
        workspace_or_version_id: str,
        element_id: str,
    ) -> ExternalReadEvidenceEnvelope:
        safe_document_id = quote(
            _entity_name(document_id, "Onshape document id"), safe=""
        )
        safe_revision_id = quote(
            _entity_name(workspace_or_version_id, "Onshape revision id"), safe=""
        )
        safe_element_id = quote(_entity_name(element_id, "Onshape element id"), safe="")
        read = self._get(
            "/api/assemblies/d/"
            f"{safe_document_id}/{workspace_or_version}/{safe_revision_id}"
            f"/e/{safe_element_id}",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="assembly_definition")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class AutodeskAPSReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_id="autodesk_aps",
            base_url=base_url,
            transport=transport,
            now=now,
        )
        self._authorization = f"Bearer {_secret(access_token, 'Autodesk APS token')}"

    def get_project(
        self, *, hub_id: str, project_id: str
    ) -> ExternalReadEvidenceEnvelope:
        read = self._get(
            "/project/v1/hubs/"
            f"{quote(_entity_name(hub_id, 'Autodesk APS hub id'), safe='')}"
            "/projects/"
            f"{quote(_entity_name(project_id, 'Autodesk APS project id'), safe='')}",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="project")

    def list_item_versions(
        self, *, project_id: str, item_id: str
    ) -> ExternalReadEvidenceEnvelope:
        read = self._get(
            "/data/v1/projects/"
            f"{quote(_entity_name(project_id, 'Autodesk APS project id'), safe='')}"
            "/items/"
            f"{quote(_path_value(item_id, 'Autodesk APS item id'), safe='')}"
            "/versions",
            self._headers(),
        )
        payload = _expect_dict(read.decoded)
        data = payload.get("data")
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            raise ExternalAPIError(
                "external_api_invalid_response", "Expected data list."
            )
        return self._list_evidence(
            read,
            resource_kind="item_versions",
            payload=[dict(item) for item in data],
        )

    def get_derivative_manifest(self, *, urn: str) -> ExternalReadEvidenceEnvelope:
        safe_urn = quote(_path_value(urn, "Autodesk APS urn"), safe="")
        read = self._get(
            f"/modelderivative/v2/designdata/{safe_urn}/manifest",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="derivative_manifest")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class WindchillReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_id="windchill", base_url=base_url, transport=transport, now=now
        )
        self._authorization = f"Bearer {_secret(access_token, 'Windchill token')}"

    def get_part(self, *, part_oid: str) -> ExternalReadEvidenceEnvelope:
        read = self._get(
            "/Windchill/servlet/odata/v4/ProdMgmt/Parts"
            f"('{quote(_path_value(part_oid, 'Windchill part oid'), safe='')}')",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="part")

    def get_change_notice(self, *, notice_oid: str) -> ExternalReadEvidenceEnvelope:
        safe_notice_oid = quote(
            _path_value(notice_oid, "Windchill change notice oid"), safe=""
        )
        read = self._get(
            "/Windchill/servlet/odata/v4/ChangeMgmt/ChangeNotices"
            f"('{safe_notice_oid}')",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="change_notice")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class ArasReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_id="aras_innovator",
            base_url=base_url,
            transport=transport,
            now=now,
        )
        self._authorization = f"Bearer {_secret(access_token, 'Aras token')}"

    def get_item(
        self, *, collection: str, item_id: str
    ) -> ExternalReadEvidenceEnvelope:
        read = self._get(
            "/Server/odata/"
            f"{quote(_collection(collection, 'Aras collection'), safe='')}"
            f"('{quote(_path_value(item_id, 'Aras item id'), safe='')}')",
            self._headers(),
        )
        return self._dict_evidence(read, resource_kind="item")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


__all__ = [
    "ArasReadOnlyClient",
    "AutodeskAPSReadOnlyClient",
    "ExternalAPIError",
    "ExternalReadEvidenceEnvelope",
    "GitLabReadOnlyClient",
    "JenkinsReadOnlyClient",
    "OnshapeReadOnlyClient",
    "ReadOnlyHTTPTransport",
    "RedactedHeaders",
    "UnavailableIntegrationDescriptor",
    "WindchillReadOnlyClient",
    "solidworks_3dexperience_descriptor",
    "teamcenter_descriptor",
]
