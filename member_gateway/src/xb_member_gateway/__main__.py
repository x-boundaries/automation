"""Production module entry point."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from .bootstrap import BootstrapError, run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xb_member_gateway")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        run(args.config, environment=os.environ)
    except BootstrapError as exc:
        sys.stderr.write(f"gateway_bootstrap_failed:{exc.code}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
