"""Compatibility entry point; all vendors share one reviewed manifest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.dependencies.fetch_vendor import main

if __name__ == "__main__":
    main()
