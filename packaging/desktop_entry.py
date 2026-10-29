"""Windowed desktop entry; stdio is unavailable on Windows windowed builds."""

import os
import sys

from termana.app import main

if __name__ == "__main__":
    # Keep argparse and fixed diagnostic messages safe without creating a console.
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w"))
    raise SystemExit(main())
