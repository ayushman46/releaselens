"""Make test-only helpers in this directory (``tracegen``) importable.

The workspace runs pytest with ``--import-mode=importlib``, which does not put test directories
on ``sys.path``.
"""

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
