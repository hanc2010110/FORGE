from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path


def main() -> int:
    lock_path = Path(sys.argv[1])
    mismatches: list[str] = []
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, expected = line.partition("==")
        if not separator or not name or not expected:
            mismatches.append(f"invalid lock entry: {line}")
            continue
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"missing package: {name}=={expected}")
            continue
        if actual != expected:
            mismatches.append(
                f"version mismatch: {name} expected {expected}, got {actual}"
            )

    if mismatches:
        print("\n".join(mismatches), file=sys.stderr)
        return 1
    print(f"lock verified: {lock_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
