"""Run the Reelay desktop application via `python -m reelay`."""

from __future__ import annotations

from .gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
