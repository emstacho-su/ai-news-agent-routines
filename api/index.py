"""Vercel serverless entrypoint.

Vercel's @vercel/python builder treats `api/*.py` as serverless
functions. We re-export the FastAPI app from `dashboard.py` so every
route hits the existing handlers without duplicating any code.

The routing in vercel.json sends `/static/*` to the static builder and
everything else to this function.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Vercel's build mounts api/ separately; the parent dir holds dashboard.py
# and config.py. Add it to sys.path so `import dashboard` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dashboard import app  # noqa: E402,F401

# Vercel's @vercel/python looks for `app` (or `handler`) at module scope.
