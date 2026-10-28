#!/usr/bin/env python3
"""Preview package releases with a short-lived cache for GitHub checks."""

import sys
from pathlib import Path

# Isolated Python omits the script directory. Never import from the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_updater.cli import main

if __name__ == "__main__":
    sys.exit(main())
