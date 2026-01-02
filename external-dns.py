#!/usr/bin/env python3

"""Compatibility wrapper.

The project is packaged under `src/external_dns`. This wrapper preserves the
original `./external-dns.py` invocation style from a fresh checkout.

Note: This file intentionally tweaks sys.path before importing the package.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from external_dns.cli import main  # noqa: E402


if __name__ == "__main__":
    main()
