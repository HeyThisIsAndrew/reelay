#!/usr/bin/env python3
"""Launch Reelay.  `--self-test` checks a packaged build without opening a window."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for frozen Windows builds
    if "--self-test" in sys.argv:
        from reelay.selftest import run
        sys.exit(run())
    from reelay.gui.app import main
    sys.exit(main())
