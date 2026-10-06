"""The health-consent gate on the weekly check-in.

POST /analysis/log saves a week's symptoms and sends them to Google's Gemini, so
it must not run for someone without a current, non-withdrawn health consent. The
rule is enforced here and attached in app/main.py when the analysis router is
included, so the analysis files themselves stay untouched:

    app.include_router(analysis.router, ..., dependencies=[Depends(require_health_consent_for_checkin)])

FastAPI runs an include_router dependency before the route's own dependencies and
before the request body is validated, so the answer is the same whatever the body:
no login is 401 (from get_current_user_id), no consent is 403, and the route body,
and with it the Gemini call, never runs. Only POST is gated: reading old logs and
reports stays open to everyone, including after withdrawal.
"""

from fastapi import Depends, Request

from app.core import consent
from app.core.services.coded_errors import coded
from app.core.services.token import get_current_user_id
from app.db.connection import supabase

MESSAGE = "Health consent is required before a weekly check-in."


def require_health_consent_for_checkin(request: Request, user_id: str = Depends(get_current_user_id)) -> None:
    if request.method != "POST":
        return
    try:
        res = supabase.table("users").select("*").eq("id", user_id).limit(1).execute()
    except Exception as e:
        print("Health consent check error:", e)
        raise coded(500, "Your consent could not be checked. Please try again.", "internal_error")
    row = res.data[0] if res.data else {}
    if consent.needs_health_consent(row):
        raise coded(403, MESSAGE, "health_consent_required")
