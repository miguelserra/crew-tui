#!/usr/bin/env python3
"""Fail fast if any test module cannot be imported.

This catches module-level errors (missing names, bad imports) that would
otherwise only surface during test discovery in CI. Platform-specific test
modules are skipped when their platform does not apply.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKIP_ON = {
    "test_power_win": "win32",
    "test_power": "linux",
}


def main() -> int:
    failed: list[tuple[str, str]] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        required = SKIP_ON.get(path.stem)
        if required and sys.platform != required:
            continue
        name = f"tests.{path.stem}"
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}"))
    if failed:
        for name, error in failed:
            print(f"FAIL {name}: {error}", file=sys.stderr)
        return 1
    print(f"OK: imported all applicable test modules ({sys.platform})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
