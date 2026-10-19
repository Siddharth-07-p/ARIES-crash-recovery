"""Module entrypoint enabling `python -m app.benchmark` execution."""

import sys
from app.benchmark.cli import main

if __name__ == "__main__":
    sys.exit(main())
