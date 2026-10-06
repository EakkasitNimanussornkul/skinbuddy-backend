"""GET /meta/policy-versions: the versions the consent screens ask people to agree to."""

import re

from app.core import consent
from app.core.consent import consent_object


def test_policy_versions_need_no_login_and_return_the_two_current_versions(client):
    """Returns HTTP 200 with exactly {"terms_version", "health_version"} for a request with no
    Authorization header, each the plain string held in app/core/consent.py."""
    resp = client.get("/meta/policy-versions")
    assert resp.status_code == 200
    assert resp.json() == {"terms_version": consent.TERMS_VERSION, "health_version": consent.HEALTH_CONSENT_VERSION}
    assert all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in resp.json().values())


def test_policy_versions_match_the_versions_in_the_consent_object(client):
    """Returns the same two strings GET /auth/me puts in consent.current_terms_version and
    consent.current_health_version, so the pages and the consent gate never disagree."""
    shown = consent_object({})
    body = client.get("/meta/policy-versions").json()
    assert body["terms_version"] == shown["current_terms_version"]
    assert body["health_version"] == shown["current_health_version"]


def test_policy_versions_follow_a_bump_in_the_consent_constants(client, monkeypatch):
    """Returns the new strings straight after TERMS_VERSION and HEALTH_CONSENT_VERSION change,
    so bumping a constant changes what the pages show with no second edit."""
    monkeypatch.setattr(consent, "TERMS_VERSION", "2027-01-15")
    monkeypatch.setattr(consent, "HEALTH_CONSENT_VERSION", "2027-02-20")
    assert client.get("/meta/policy-versions").json() == {"terms_version": "2027-01-15", "health_version": "2027-02-20"}
