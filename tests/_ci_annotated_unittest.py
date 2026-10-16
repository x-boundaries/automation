"""Run unittest modules and surface each failure/error as a GitHub annotation.

Same tests, same exit status as ``python -m unittest <names> -v``; it only adds
one ``::error`` line per failing test (test id plus the final traceback line)
so a red hosted run can be diagnosed from public check-run annotations. Tests
use synthetic data only; any Windows account SID is redacted anyway.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: import tests.* by name

_SID = re.compile(r"S-1-5-21-[0-9-]+")


def main(names: list[str]) -> int:
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for kind, items in (("FAIL", result.failures), ("ERROR", result.errors)):
        for test, trace in items[:25]:
            lines = [line.strip() for line in trace.strip().splitlines() if line.strip()]
            last = _SID.sub("S-1-5-21-<redacted>", lines[-1] if lines else "")[:300].replace("%", "%25").replace("\r", "").replace("\n", " ")
            print(f"::error title={kind} {test.id()}::{last}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
