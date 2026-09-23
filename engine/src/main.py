"""
FastAPI entrypoint for the reconciliation engine: app construction, CORS,
the two middlewares (model metering, API key) and the route groups.

  api/routes_reconcile.py   /reconcile, /multi, /joint, /upload, /queue,
                            /settlements/detect
  api/routes_ops.py         /audit/{batch_id}[/verify], /demo, /health
  api/routes_*.py           decisions, reports, Razorpay, statements, tax,
                            open items, chargebacks, webhooks, exports, AI
What a run does lives in pipeline.py, orchestrator.py and settlement_run.py.
Run: uvicorn main:app --reload --app-dir src
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Load .env before anything reads os.environ. override=False: a real
# environment variable always wins over a file left in the working tree.
try:
    from dotenv import load_dotenv
    for _candidate in (
        Path(__file__).resolve().parents[1] / ".env",   # engine/.env
        Path(__file__).resolve().parents[2] / ".env",   # repo root
    ):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
            logging.getLogger(__name__).info("Loaded environment from %s", _candidate)
except ImportError:
    # Optional. Without it the engine still reads real environment variables.
    pass

import auth  # noqa: E402
import model_budget  # noqa: E402

# Without this, every logger.info(...) call in the pipeline (compliance
# blocks, linkage narrowing, which tier cleared) is swallowed by Python's
# default WARNING level, and a long run looks idle.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Re-exported: tests and callers reach for these here.
from api.config import CORS_ORIGINS, CORS_ORIGIN_REGEX  # noqa: E402,F401
from api.models import (  # noqa: E402,F401
    TxnIn, SettlementBatchIn, ReconcileRequest, MultiSourceReconcileRequest,
    JointReconcileRequest, _build_settlement_batch,
)
from api.presentation import (  # noqa: E402,F401
    _format_report, _check_then_record, contested_payments, compliance_review,
    interchangeable_note, matched_rows, _settlement_instant, _rate_card,
    _rate_card_is_assumed,
)
from api.uploads import MAX_UPLOAD_BYTES, _UPLOAD_CHUNK, read_upload_capped  # noqa: E402,F401
from fee_decomposition import DEFAULT_RATE_CARD  # noqa: E402,F401


app = FastAPI(
    title="AI Finance Controller — Reconciliation Engine",
    description=(
        "Multi-source reconciliation engine using CP-SAT subset-sum matching, "
        "fuzzy semantic fallback, and rule-based exception diagnosis. "
        "Designed for Razorpay Buildathon Track 04."
    ),
    version="1.0.0",
)

if CORS_ORIGINS == ["*"]:
    logging.getLogger(__name__).warning(
        "CORS is open to every origin. Any site a user visits can read this "
        "engine's settlement data and audit trails. Acceptable for a local "
        "demo; never for a deployment holding real settlements."
    )

@app.middleware("http")
async def _meter_model_calls(request, call_next):
    """
    Name the visitor, so model calls made while serving them can be metered.

    Vercel sets x-real-ip and the first x-forwarded-for hop from the connection
    it received; elsewhere the socket's address is the best there is.
    """
    ip = (request.headers.get("x-real-ip")
          or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    model_budget.begin_request(ip)
    return await call_next(request)


@app.middleware("http")
async def _require_api_key(request, call_next):
    """
    Auth as middleware rather than a per-route dependency.

    A dependency has to be remembered on every new endpoint, and the one it
    is forgotten on is the one that leaks. Middleware defaults to closed and
    names its exceptions, so adding a route cannot accidentally add an
    unauthenticated route.
    """
    if request.method == "OPTIONS" or request.url.path in auth.PUBLIC_PATHS:
        return await call_next(request)
    # Paths that prove who they are by HMAC instead of by API key. See
    # auth.HMAC_AUTHENTICATED_PATHS for why this is a different door and not
    # a hole in the same one.
    if request.url.path in auth.HMAC_AUTHENTICATED_PATHS:
        return await call_next(request)
    expected = auth.configured_key()
    if expected is not None:
        supplied = request.headers.get("X-API-Key")
        if not supplied:
            hdr = request.headers.get("Authorization", "")
            if hdr.lower().startswith("bearer "):
                supplied = hdr[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API key. Send it as "
                                   "X-API-Key or 'Authorization: Bearer <key>'."},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


auth.warn_if_open()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    # Only meaningful once origins are explicit, which is now the default.
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Route groups, in the order they were registered when they lived here.
from api.routes_reconcile import router as _reconcile_router  # noqa: E402
from api.routes_decisions import router as _decisions_router  # noqa: E402
from api.routes_reports import router as _reports_router  # noqa: E402
from api.routes_webhook import router as _webhook_router  # noqa: E402
from api.routes_chargebacks import router as _chargeback_router  # noqa: E402
from api.routes_tax import router as _tax_router  # noqa: E402
from api.routes_open_items import router as _open_items_router  # noqa: E402
from api.routes_razorpay import router as _razorpay_router  # noqa: E402
from api.routes_statements import router as _statements_router  # noqa: E402
from api.routes_exports import router as _exports_router  # noqa: E402
from api.routes_ai import router as _ai_router  # noqa: E402
from api.routes_ops import router as _ops_router  # noqa: E402

for _router in (_reconcile_router, _decisions_router, _reports_router, _webhook_router,
                _chargeback_router, _tax_router, _open_items_router, _razorpay_router,
                _statements_router, _exports_router, _ai_router, _ops_router):
    app.include_router(_router)
