from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def canonical_sha256(value: BaseModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
