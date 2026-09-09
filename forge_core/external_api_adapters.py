from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

MAX_EXTERNAL_API_RESPONSE_BYTES = 1_000_000
DEFAULT_EXTERNAL_API_TIMEOUT_SECONDS = 12.0
MAX_EXTERNAL_IDENTIFIER_LENGTH = 512

_SAFE_ENTITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}")
_SAFE_COLLECTION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_HEX_SHA = re.compile(r"[A-Fa-f0-9]{7,64}")


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
    return [dict(item) for item in decoded if isinstance(item, dict)]


class _ReadOnlyClient:
    def __init__(
        self,
        *,
        base_url: str,
        transport: ReadOnlyHTTPTransport,
        timeout_seconds: float = DEFAULT_EXTERNAL_API_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("external API timeout is outside supported bounds")
        self._base_url = _base_url(base_url)
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def _get(self, path: str, headers: Mapping[str, str]) -> object:
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("external API path boundary is invalid")
        status, _headers, raw = self._transport.request(
            "GET",
            f"{self._base_url}{path}",
            headers=RedactedHeaders(headers),
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_EXTERNAL_API_RESPONSE_BYTES,
        )
        return _decode(status, raw)


class GitLabReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        project_path: str,
        token: str,
        transport: ReadOnlyHTTPTransport,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport)
        self._project_path = quote(
            _path_value(project_path, "GitLab project path", allow_slash=True),
            safe="",
        )
        self._token = _secret(token, "GitLab token")

    def get_commit(self, *, sha: str) -> dict[str, object]:
        decoded = self._get(
            f"/api/v4/projects/{self._project_path}/repository/commits/"
            f"{quote(_git_sha(sha, 'GitLab commit SHA'), safe='')}",
            {"Private-Token": self._token},
        )
        return _expect_dict(decoded)

    def list_merge_requests(
        self, *, state: Literal["opened", "closed", "locked", "merged"] | None = None
    ) -> list[dict[str, object]]:
        query = f"?{urlencode({'state': state})}" if state else ""
        decoded = self._get(
            f"/api/v4/projects/{self._project_path}/merge_requests{query}",
            {"Private-Token": self._token},
        )
        return _expect_list(decoded)

    def list_pipelines(self, *, ref: str | None = None) -> list[dict[str, object]]:
        query = f"?{urlencode({'ref': _path_value(ref, 'GitLab ref')})}" if ref else ""
        decoded = self._get(
            f"/api/v4/projects/{self._project_path}/pipelines{query}",
            {"Private-Token": self._token},
        )
        return _expect_list(decoded)

    def list_pipeline_jobs(self, *, pipeline_id: int) -> list[dict[str, object]]:
        if pipeline_id < 1:
            raise ValueError("GitLab pipeline id must be positive")
        decoded = self._get(
            f"/api/v4/projects/{self._project_path}/pipelines/{pipeline_id}/jobs",
            {"Private-Token": self._token},
        )
        return _expect_list(decoded)


class JenkinsReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        job_path: str,
        username: str,
        api_token: str,
        transport: ReadOnlyHTTPTransport,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport)
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

    def get_job(self) -> dict[str, object]:
        decoded = self._get(f"/{self._job_path}/api/json", self._headers())
        return _expect_dict(decoded)

    def get_build(self, *, build_number: int) -> dict[str, object]:
        if build_number < 1:
            raise ValueError("Jenkins build number must be positive")
        decoded = self._get(
            f"/{self._job_path}/{build_number}/api/json",
            self._headers(),
        )
        return _expect_dict(decoded)

    def get_last_build(self) -> dict[str, object]:
        decoded = self._get(f"/{self._job_path}/lastBuild/api/json", self._headers())
        return _expect_dict(decoded)

    def get_test_report(self, *, build_number: int) -> dict[str, object]:
        if build_number < 1:
            raise ValueError("Jenkins build number must be positive")
        decoded = self._get(
            f"/{self._job_path}/{build_number}/testReport/api/json",
            self._headers(),
        )
        return _expect_dict(decoded)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class OnshapeReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport)
        self._authorization = f"Bearer {_secret(access_token, 'Onshape OAuth token')}"

    def get_document(self, *, document_id: str) -> dict[str, object]:
        safe_document_id = quote(
            _entity_name(document_id, "Onshape document id"), safe=""
        )
        decoded = self._get(
            f"/api/documents/{safe_document_id}",
            self._headers(),
        )
        return _expect_dict(decoded)

    def list_document_versions(self, *, document_id: str) -> list[dict[str, object]]:
        safe_document_id = quote(
            _entity_name(document_id, "Onshape document id"), safe=""
        )
        decoded = self._get(
            f"/api/documents/{safe_document_id}/versions",
            self._headers(),
        )
        return _expect_list(decoded)

    def get_assembly_definition(
        self,
        *,
        document_id: str,
        workspace_or_version: Literal["w", "v", "m"],
        workspace_or_version_id: str,
        element_id: str,
    ) -> dict[str, object]:
        safe_document_id = quote(
            _entity_name(document_id, "Onshape document id"), safe=""
        )
        safe_revision_id = quote(
            _entity_name(workspace_or_version_id, "Onshape revision id"), safe=""
        )
        safe_element_id = quote(_entity_name(element_id, "Onshape element id"), safe="")
        decoded = self._get(
            "/api/assemblies/d/"
            f"{safe_document_id}/{workspace_or_version}/{safe_revision_id}"
            f"/e/{safe_element_id}",
            self._headers(),
        )
        return _expect_dict(decoded)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class AutodeskAPSReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport)
        self._authorization = f"Bearer {_secret(access_token, 'Autodesk APS token')}"

    def get_project(self, *, hub_id: str, project_id: str) -> dict[str, object]:
        decoded = self._get(
            "/project/v1/hubs/"
            f"{quote(_entity_name(hub_id, 'Autodesk APS hub id'), safe='')}"
            "/projects/"
            f"{quote(_entity_name(project_id, 'Autodesk APS project id'), safe='')}",
            self._headers(),
        )
        return _expect_dict(decoded)

    def list_item_versions(
        self, *, project_id: str, item_id: str
    ) -> list[dict[str, object]]:
        decoded = self._get(
            "/data/v1/projects/"
            f"{quote(_entity_name(project_id, 'Autodesk APS project id'), safe='')}"
            "/items/"
            f"{quote(_path_value(item_id, 'Autodesk APS item id'), safe='')}"
            "/versions",
            self._headers(),
        )
        payload = _expect_dict(decoded)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ExternalAPIError(
                "external_api_invalid_response", "Expected data list."
            )
        return [dict(item) for item in data if isinstance(item, dict)]

    def get_derivative_manifest(self, *, urn: str) -> dict[str, object]:
        safe_urn = quote(_path_value(urn, "Autodesk APS urn"), safe="")
        decoded = self._get(
            f"/modelderivative/v2/designdata/{safe_urn}/manifest",
            self._headers(),
        )
        return _expect_dict(decoded)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class WindchillReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport)
        self._authorization = f"Bearer {_secret(access_token, 'Windchill token')}"

    def get_part(self, *, part_oid: str) -> dict[str, object]:
        decoded = self._get(
            "/Windchill/servlet/odata/v4/ProdMgmt/Parts"
            f"('{quote(_path_value(part_oid, 'Windchill part oid'), safe='')}')",
            self._headers(),
        )
        return _expect_dict(decoded)

    def get_change_notice(self, *, notice_oid: str) -> dict[str, object]:
        safe_notice_oid = quote(
            _path_value(notice_oid, "Windchill change notice oid"), safe=""
        )
        decoded = self._get(
            "/Windchill/servlet/odata/v4/ChangeMgmt/ChangeNotices"
            f"('{safe_notice_oid}')",
            self._headers(),
        )
        return _expect_dict(decoded)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class ArasReadOnlyClient(_ReadOnlyClient):
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: ReadOnlyHTTPTransport,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport)
        self._authorization = f"Bearer {_secret(access_token, 'Aras token')}"

    def get_item(self, *, collection: str, item_id: str) -> dict[str, object]:
        decoded = self._get(
            "/Server/odata/"
            f"{quote(_collection(collection, 'Aras collection'), safe='')}"
            f"('{quote(_path_value(item_id, 'Aras item id'), safe='')}')",
            self._headers(),
        )
        return _expect_dict(decoded)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


__all__ = [
    "ArasReadOnlyClient",
    "AutodeskAPSReadOnlyClient",
    "ExternalAPIError",
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
