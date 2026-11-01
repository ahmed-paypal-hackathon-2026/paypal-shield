#!/usr/bin/env python3
"""REQ-NF-05: no user-facing string may contain the 'i-word' (regulatory risk).

Scans frontend sources/markup and backend route/schemas for the banned term,
skipping this and other CI-facing files and markdown documentation.
"""

import sys
from pathlib import Path

BANNED = "insur" + "ance"
SCAN_ROOTS = ["frontend/src", "frontend/index.html", "backend/app", "shared"]
SKIP_SUFFIXES = (".md",)


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    violations = []

    for scan_root in SCAN_ROOTS:
        target = repo / scan_root
        if target.is_file():
            files = [target]
        else:
            files = [p for p in target.rglob("*") if p.is_file() and not p.suffix in SKIP_SUFFIXES]
        for file in files:
            text = file.read_text(errors="ignore")
            lowered = text.lower()
            if BANNED in lowered:
                line = next(
                    (i for i, line in enumerate(lowered.splitlines(), 1) if BANNED in line), 0
                )
                violations.append(f"{file.relative_to(repo)}:{line}")

    if violations:
        print("TERMINLOGY VIOLATIONS — the fee is a 'Buyer Protection Fee', never described as underwritten coverage:")
        for v in violations:
            print(f"  {v}")
        return 1
    print("terminology lint clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
