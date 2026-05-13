"""HR Reporting Pipeline — entry point."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    print(f"HR Reporting Pipeline starting from {PROJECT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
