from __future__ import annotations

import os
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast


def _repository_root() -> Path:
    configured = os.environ.get("FORGE_REPOSITORY_ROOT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
    else:
        candidate = Path(__file__).resolve().parents[3]
    if not candidate.joinpath("forge_core", "mcp_server.py").is_file():
        raise SystemExit(
            "FORGE backend not found. Set FORGE_REPOSITORY_ROOT to the cloned "
            "ai-engineering-orchestrator directory."
        )
    return candidate


root = _repository_root()
sys.path.insert(0, str(root))
main = cast(Callable[[], None], import_module("forge_core.mcp_server").main)


if __name__ == "__main__":
    main()
