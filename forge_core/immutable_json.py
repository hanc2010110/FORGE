from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast


def freeze_json_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Return a detached, recursively immutable JSON object."""
    normalized = _normalize_json(value)
    frozen = _freeze(normalized)
    if not isinstance(frozen, Mapping):
        raise ValueError("JSON payload must be an object")
    return cast(Mapping[str, object], frozen)


def thaw_json(value: object) -> object:
    """Return ordinary JSON-compatible dict/list containers for serialization."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [thaw_json(item) for item in value]
    return value


def _normalize_json(value: object) -> object:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain only bounded JSON values") from exc
    return json.loads(encoded)


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = ["freeze_json_mapping", "thaw_json"]
