"""conftest.py for the backend test suite.

Ensures `app.*` imports resolve correctly when pytest is run from either
the repository root or the `backend/` directory.
"""
import sys
import os

# Add `backend/` to sys.path (idempotent)
_backend = os.path.dirname(os.path.abspath(__file__))
if _backend not in sys.path:
    sys.path.insert(0, _backend)
