"""Run every offline check. One command, no framework, no network.

    python scripts/run_tests.py

Deliberately not pytest. The suite is three files; a framework would add a
dependency, a config file and a plugin surface to run three functions. If this
grows past a dozen files, reach for pytest then - not before.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS = [
    ("gold SQL + data-quality suite", ROOT / "tests" / "test_gold.py"),
    ("library unit tests", ROOT / "tests" / "test_lib.py"),
    ("notebook generation", ROOT / "scripts" / "make_notebooks.py"),
]


def main() -> int:
    failures: list[str] = []

    for label, script in CHECKS:
        if not script.exists():
            print(f"SKIP  {label} ({script.name} not present)")
            continue
        print(f"\n--- {label} ---")
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(script)], cwd=str(ROOT), check=False
        )
        if result.returncode != 0:
            failures.append(label)

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
