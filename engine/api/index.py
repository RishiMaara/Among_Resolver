"""
Vercel entrypoint for the engine.

Vercel's Python runtime serves ASGI apps from files under api/. The engine's
modules import one another flat (`import audit`, `from linkage import ...`)
because they are written to run with src/ on the path, so src/ goes on
sys.path before main is imported. Nothing else happens here: the app is
main.app, unchanged, and vercel.json rewrites every route to this function so
the engine's own paths (/health, /reconcile/upload, ...) work as they do
locally.

This directory has no __init__.py on purpose. src/api/ is a regular package
holding the route modules; leaving this one a plain directory means it can
never shadow that package, whichever order the runtime lays out sys.path.
"""

import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from main import app  # noqa: E402,F401  (re-exported for the runtime)
