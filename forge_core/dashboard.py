from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType

DASHBOARD_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; connect-src 'self'; style-src 'self'; script-src 'self'"
)


@dataclass(frozen=True, slots=True)
class DashboardAsset:
    content_type: str
    body: bytes


_ASSET_MANIFEST = MappingProxyType(
    {
        "/app/": ("web/index.html", "text/html; charset=utf-8"),
        "/app/styles.css": ("web/styles.css", "text/css; charset=utf-8"),
        "/app/app.js": ("web/app.js", "text/javascript; charset=utf-8"),
    }
)


def is_dashboard_target(target: str) -> bool:
    return target.startswith("/app/")


@lru_cache(maxsize=len(_ASSET_MANIFEST))
def load_dashboard_asset(target: str) -> DashboardAsset | None:
    manifest = _ASSET_MANIFEST.get(target)
    if manifest is None:
        return None
    resource_name, content_type = manifest
    body = files("forge_core").joinpath(resource_name).read_bytes()
    return DashboardAsset(content_type=content_type, body=body)


__all__ = [
    "DASHBOARD_CONTENT_SECURITY_POLICY",
    "DashboardAsset",
    "is_dashboard_target",
    "load_dashboard_asset",
]
