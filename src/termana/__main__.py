"""Allow Termana to be started with ``python -m termana``."""

from termana.app import main

if __name__ == "__main__":
    raise SystemExit(main())
