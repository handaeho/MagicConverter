"""PyInstaller entrypoint; importing the package preserves absolute imports."""

from magic_converter.core.jobs.phase_trace import mark

mark("python_entry")
from magic_converter.main import main

if __name__ == "__main__":
    raise SystemExit(main())
