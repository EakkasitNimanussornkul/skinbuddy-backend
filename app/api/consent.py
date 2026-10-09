"""Consent records: POST /consent/terms, POST /consent/health, DELETE /consent/health.

All three need a login and answer the consent object (app/core/consent.py). Every
error body is {"detail": str, "code": str}; the 401 comes from the login dependency.
Withdrawing health consent deletes no data: it stops the weekly check-in being
analysed (the gate in app/core/services/consent_gate.py) until it is given again.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core import consent
from app.core.services.coded_errors import CodedErrorRoute, coded
from app.core.services.token import get_current_user_id
from app.db.connection import supabase

router = APIRouter(route_class=CodedErrorRoute)

SAVE_FAILED = "Your choice could not be saved. Please try again."


class TermsConsentRequest(BaseModel):
    terms_version: str
    age_confirmed: bool


class HealthConsentRequest(BaseModel):
    health_version: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_version(what: str, current: str):
    return coded(409, f"The {what} have changed. Please read the current version before agreeing.",
                 "policy_version_changed", current_version=current)


def _read_row(user_id: str) -> dict:
    try:
        res = supabase.table("users").select("*").eq("id", user_id).limit(1).execute()
    except Exception as e:
        print("Consent read error:", e)
        raise coded(500, "Your consent could not be read. Please try again.", "internal_error")
    if not res.data:
        raise coded(404, "User not found.", "user_not_found")
    return res.data[0]


def _write(user_id: str, changes: dict) -> dict:
    try:
        res = supabase.table("users").update(changes).eq("id", user_id).execute()
    except Exception as e:
        print("Consent save error:", e)
        raise coded(500, SAVE_FAILED, "internal_error")
    if not res.data:
        raise coded(404, "User not found.", "user_not_found")
    return res.data[0]


@router.post("/terms")
def accept_terms(req: TermsConsentRequest, user_id: str = Depends(get_current_user_id)):
    if req.age_confirmed is not True:
        raise coded(422, "You must confirm that you are 18 or older to use SkinBuddy.", "age_not_confirmed")
    if req.terms_version != consent.TERMS_VERSION:
        raise _stale_version("terms and privacy policy", consent.TERMS_VERSION)
    now = _now()
    row = _write(user_id, {"terms_accepted_at": now, "terms_version": consent.TERMS_VERSION,
                           "age_confirmed_at": now})
    return consent.consent_object(row)


@router.post("/health")
def give_health_consent(req: HealthConsentRequest, user_id: str = Depends(get_current_user_id)):
    if req.health_version != consent.HEALTH_CONSENT_VERSION:
        raise _stale_version("health information terms", consent.HEALTH_CONSENT_VERSION)
    row = _write(user_id, {"health_consent_at": _now(),
                           "health_consent_version": consent.HEALTH_CONSENT_VERSION,
                           "health_consent_withdrawn_at": None})
    return consent.consent_object(row)


@router.delete("/health")
def withdraw_health_consent(user_id: str = Depends(get_current_user_id)):
    row = _read_row(user_id)
    if consent.health_withdrawn(row):
        return consent.consent_object(row)       # already withdrawn: nothing to change
    row = _write(user_id, {"health_consent_withdrawn_at": _now()})
    return consent.consent_object(row)
