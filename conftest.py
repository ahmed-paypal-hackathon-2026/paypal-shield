"""Root conftest.py — adds `backend/` to sys.path so that
`from app.ai.xxx import yyy` works in all tests.
"""
import sys
import os

# Ensure the `backend` directory is on the Python path so that
# `import app.ai.*` resolves correctly regardless of the pytest invocation CWD.
_backend = os.path.join(os.path.dirname(__file__), "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)
