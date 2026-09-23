"""Deployment settings read at startup: which origins may call the engine."""
from __future__ import annotations

import os

# CORS: explicit origins (the local dev servers by default; CORS_ORIGINS to
# deploy). A wildcard with credentials let any site read settlement data and
# is invalid under the spec anyway.
_DEV_ORIGINS = [
    "http://localhost:8080", "http://127.0.0.1:8080",   # this project's vite port
    "http://localhost:5173", "http://127.0.0.1:5173",   # vite default
]
_configured = os.environ.get("CORS_ORIGINS", "").strip()
CORS_ORIGINS = (
    [o.strip() for o in _configured.split(",") if o.strip()]
    if _configured else _DEV_ORIGINS
)

# Optional: a regex for origins that cannot be listed ahead of time.
#
# A Vercel deployment serves every preview at its own generated host
# (project-git-branch-owner.vercel.app), so an explicit list can only ever name
# production — and a preview of the frontend then fails every request with a
# CORS error that looks exactly like the engine being down. Anchored by
# Starlette (fullmatch), so "https://my-app.*\.vercel\.app" cannot be satisfied
# by an attacker's "https://my-app.evil.example/.vercel.app".
CORS_ORIGIN_REGEX = os.environ.get("CORS_ORIGIN_REGEX", "").strip() or None
