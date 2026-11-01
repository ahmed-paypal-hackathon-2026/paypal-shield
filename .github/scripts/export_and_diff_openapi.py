#!/usr/bin/env python3
"""Export the live FastAPI app's OpenAPI and diff it against the frozen contract.

Fails the CI job when any path/schema differs. This is REQ-OPS-02: the
canonical contract cannot drift silently.
"""

import json
import sys
from asyncio import new_event_loop
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "backend"))


def deep_diff(a, b, path=""):
    errors = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                errors.append(f"{path}.{key}: missing in frozen contract")
            elif key not in b:
                errors.append(f"{path}.{key}: extra in frozen contract")
            else:
                errors.extend(deep_diff(a[key], b[key], f"{path}.{key}"))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            errors.append(f"{path}: list length {len(a)} != {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                errors.extend(deep_diff(x, y, f"{path}[{i}]"))
    elif a != b:
        errors.append(f"{path}: {a!r} != {b!r}")
    return errors


def main() -> int:
    try:
        from app.main import app  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"cannot import backend app (P1 has not landed main.py?): {exc}")
        print("treat as failure: backend must export `app` for contract diffs")
        return 1

    loop = new_event_loop()
    try:
        # FastAPI usage is sync here; allow async app wiring.
        spec = app.openapi()
    finally:
        loop.close()

    frozen_path = ROOT / "shared" / "openapi.json"
    frozen = json.loads(frozen_path.read_text())

    errors = deep_diff(frozen, spec, "$")
    if errors:
        print("CONTRACT DRIFT DETECTED — regenerate shared/openapi.json and bump the version:")
        for error in errors[:40]:
            print(f"  {error}")
        return 1
    print("generated OpenAPI matches the frozen contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
