"""Shortcut: `python auth.py` -> scripts/qbo_authorize.py

Exists purely so the command has no directory separator in it. Shell
tab-completion on `scripts/` was corrupting the typed path.

Any arguments are passed straight through:

    python auth.py                # sandbox
    python auth.py --production
"""

import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "scripts" / "qbo_authorize.py"

if __name__ == "__main__":
    sys.argv[0] = str(TARGET)
    runpy.run_path(str(TARGET), run_name="__main__")
