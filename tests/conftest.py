"""Make the src/ package importable for the offline regression tests.

The pipeline modules live in ../src and import each other with flat module
names, so we prepend that directory to sys.path before collection.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
