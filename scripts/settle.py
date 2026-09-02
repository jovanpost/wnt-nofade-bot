#!/usr/bin/env python3
"""Nightly / manual settlement sweep. Delegates to wnt.settle."""
from __future__ import annotations

import sys

from wnt import store
from wnt.settle import sweep


def main() -> int:
    store.init_db()
    n = sweep()
    print(f"updated {n} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
