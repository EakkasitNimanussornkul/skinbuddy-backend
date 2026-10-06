"""What a user has agreed to, and whether they must be asked again.

Two separate consents are recorded on the users row (migration 0014):

  * the terms and privacy policy, with a confirmation that the user is 18 or
    older (terms_accepted_at, terms_version, age_confirmed_at);
  * health information: this week's check-in, and up to 4 earlier weeks, are sent
    to Google's Gemini without the name or LINE id (health_consent_at,
    health_consent_version, health_consent_withdrawn_at). Thailand's PDPA s.19
    and s.26 need that consent to be explicit, separate from the terms, and as
    easy to withdraw as to give.

The versions below are compared with the version stored on each user's row.
BUMPING A CONSTANT RE-PROMPTS EVERYONE: a user whose stored version differs is
asked again, and the frontend posts back the version it was shown, so a stale
screen cannot be agreed to. Change the constant whenever the text the user
agrees to changes in substance, to the new date. They live here, in code, and
not in .env: a version is part of what was deployed with the wording it names.
"""

from datetime import datetime
from typing import Any, Dict, Optional

TERMS_VERSION = "2026-10-06"
HEALTH_CONSENT_VERSION = "2026-10-06"


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def health_withdrawn(row: Dict[str, Any]) -> bool:
    """True when the user withdrew health consent and has not given it again since.
    Giving it again clears the withdrawal, so a withdrawal that is still set
    counts, unless the consent is demonstrably newer than it."""
    withdrawn_at = row.get("health_consent_withdrawn_at")
    if not withdrawn_at:
        return False
    given, withdrawn = _parse(row.get("health_consent_at")), _parse(withdrawn_at)
    if given is not None and withdrawn is not None and given > withdrawn:
        return False
    return True


def needs_terms(row: Dict[str, Any]) -> bool:
    """Never accepted, accepted a different version, or the age never confirmed."""
    return (not row.get("terms_accepted_at")
            or row.get("terms_version") != TERMS_VERSION
            or not row.get("age_confirmed_at"))


def needs_health_consent(row: Dict[str, Any]) -> bool:
    """Never given, given for a different version, or withdrawn since."""
    return (not row.get("health_consent_at")
            or row.get("health_consent_version") != HEALTH_CONSENT_VERSION
            or health_withdrawn(row))


def consent_object(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The consent object returned by GET /auth/me and every consent route. A
    row that lacks the columns (0014 not run yet) or is None reads as no consent."""
    row = row or {}
    return {
        "terms_accepted_at": row.get("terms_accepted_at"),
        "terms_version": row.get("terms_version"),
        "age_confirmed_at": row.get("age_confirmed_at"),
        "health_consent_at": row.get("health_consent_at"),
        "health_consent_version": row.get("health_consent_version"),
        "health_consent_withdrawn_at": row.get("health_consent_withdrawn_at"),
        "current_terms_version": TERMS_VERSION,
        "current_health_version": HEALTH_CONSENT_VERSION,
        "needs_terms": needs_terms(row),
        "needs_health_consent": needs_health_consent(row),
    }
