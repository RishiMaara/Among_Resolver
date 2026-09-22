"""
GET /ai/status — whether a model is on this server, for what, and how much
budget is left today.

The interface reads this to say plainly which answers came from a model and
which from fixed rules. It never returns the key or any part of it.
"""

from __future__ import annotations

from fastapi import APIRouter

import llm_header_mapper
import llm_provider
import model_budget
import settlement_qa

router = APIRouter()


@router.get("/ai/status", summary="Which model uses are live on this server")
def ai_status():
    live = llm_provider.is_configured()
    return {
        "model": llm_provider.DEFAULT_MODEL if live else None,
        "live": live,
        "uses": {
            "column_headers": live and llm_header_mapper.is_enabled(),
            "bank_narrations": live,
            "investigator": live,
            "questions": live and settlement_qa.is_enabled(),
            "scanned_statements": live,
        },
        "decides_membership": False,
        "budget": model_budget.status(),
        "plain": (
            f"{llm_provider.DEFAULT_MODEL} is live on this server, metered per visitor and "
            f"per day. Every answer it gives is checked in code before it is used, and none "
            f"of them decides which payments make up a settlement."
            if live else
            "No model key on this server. Every model use falls back to fixed rules, and "
            "the interface says so wherever that happens."),
    }
