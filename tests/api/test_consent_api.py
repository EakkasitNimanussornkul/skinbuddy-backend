"""Consent records: POST /consent/terms, POST /consent/health, DELETE /consent/health,
and the consent object in GET /auth/me.

The database is the in-memory fake in tests/conftest.py. Every error body from
these routes is {"detail": str, "code": str}; the 401 comes from the shared login
dependency and has no code. Docstrings state the expected output and are lifted
verbatim into the Test Record.
"""

from datetime import datetime

import pytest

from app.core import consent

T, H = consent.TERMS_VERSION, consent.HEALTH_CONSENT_VERSION
CONSENT_KEYS = {"terms_accepted_at", "terms_version", "age_confirmed_at", "health_consent_at",
                "health_consent_version", "health_consent_withdrawn_at", "current_terms_version",
                "current_health_version", "needs_terms", "needs_health_consent"}
STORED_KEYS = ("terms_accepted_at", "terms_version", "age_confirmed_at", "health_consent_at",
               "health_consent_version", "health_consent_withdrawn_at")


def user_row(user_id="user-1", **consent_columns):
    row = {"id": user_id, "line_id": f"line-{user_id}", "display_name": "Test User",
           "picture_url": "https://pic", "skin_type": "DSPT", "role": "user"}
    row.update({key: None for key in STORED_KEYS})
    row.update(consent_columns)
    return row


@pytest.fixture
def db(patch_supabase, as_user):
    """The caller is logged in as user-1; a second user exists, and must never be touched."""
    as_user("user-1")

    def _make(*rows):
        return patch_supabase({"users": list(rows) or [user_row(), user_row("user-2")]},
                              "app.api.consent", "app.api.auth")
    return _make


def stored(fake, user_id="user-1"):
    return next(r for r in fake.store["users"] if r["id"] == user_id)


def is_iso_time(value):
    return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None


# --- POST /consent/terms ------------------------------------------------------------

def test_accepting_the_terms_records_the_version_the_age_and_the_time(client, db):
    """Returns HTTP 200 and the consent object with terms_accepted_at and
    age_confirmed_at set to timestamps, terms_version equal to the current version
    and needs_terms false, and stores the same three values on the caller's row."""
    fake = db()
    resp = client.post("/consent/terms", json={"terms_version": T, "age_confirmed": True})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == CONSENT_KEYS
    assert body["terms_version"] == T and body["needs_terms"] is False
    assert is_iso_time(body["terms_accepted_at"]) and is_iso_time(body["age_confirmed_at"])
    row = stored(fake)
    assert (row["terms_accepted_at"], row["terms_version"], row["age_confirmed_at"]) == (
        body["terms_accepted_at"], T, body["age_confirmed_at"])


def test_accepting_the_terms_leaves_health_consent_and_other_users_alone(client, db):
    """Leaves the caller's health consent columns as they were, and leaves every
    column of another user's row untouched."""
    fake = db(user_row(health_consent_at="2026-10-01T00:00:00+00:00", health_consent_version=H),
              user_row("user-2"))
    other_before = dict(stored(fake, "user-2"))
    resp = client.post("/consent/terms", json={"terms_version": T, "age_confirmed": True})
    assert resp.status_code == 200
    assert stored(fake)["health_consent_at"] == "2026-10-01T00:00:00+00:00"
    assert resp.json()["needs_health_consent"] is False
    assert stored(fake, "user-2") == other_before


def test_terms_are_refused_without_the_age_confirmation(client, db):
    """Returns HTTP 422 with code age_not_confirmed when age_confirmed is false, and
    stores nothing."""
    fake = db()
    resp = client.post("/consent/terms", json={"terms_version": T, "age_confirmed": False})
    assert resp.status_code == 422
    assert resp.json()["code"] == "age_not_confirmed" and isinstance(resp.json()["detail"], str)
    assert stored(fake)["terms_accepted_at"] is None and stored(fake)["age_confirmed_at"] is None


def test_terms_with_a_missing_field_answer_422_with_a_code(client, db):
    """Returns HTTP 422 with a text detail and code validation_error, not a list of
    errors, when the body lacks age_confirmed or terms_version, or has the wrong type;
    and stores nothing."""
    fake = db()
    for body in ({"terms_version": T}, {"age_confirmed": True}, {}, {"terms_version": T, "age_confirmed": "maybe"}):
        resp = client.post("/consent/terms", json=body)
        assert resp.status_code == 422, body
        assert resp.json()["code"] == "validation_error" and isinstance(resp.json()["detail"], str), body
    assert stored(fake)["terms_accepted_at"] is None


def test_terms_with_a_stale_version_answer_409_with_the_current_version(client, db):
    """Returns HTTP 409 with code policy_version_changed and current_version set to
    the current terms version when the posted version is not the current one, and
    stores nothing."""
    fake = db()
    resp = client.post("/consent/terms", json={"terms_version": "2020-01-01", "age_confirmed": True})
    assert resp.status_code == 409
    body = resp.json()
    assert set(body) == {"detail", "code", "current_version"}
    assert body["code"] == "policy_version_changed" and body["current_version"] == T
    assert isinstance(body["detail"], str) and body["detail"]
    assert stored(fake)["terms_accepted_at"] is None


def test_terms_need_a_login(client, db):
    """Returns HTTP 401 for a request with no Authorization header."""
    from app.main import app
    from app.core.services.token import get_current_user_id
    db()
    app.dependency_overrides.pop(get_current_user_id, None)
    resp = client.post("/consent/terms", json={"terms_version": T, "age_confirmed": True})
    assert resp.status_code == 401


def test_terms_for_a_user_who_no_longer_exists_answer_404_with_a_code(client, db):
    """Returns HTTP 404 with code user_not_found when no users row matches the
    caller, and creates no row."""
    fake = db(user_row("someone-else"))
    resp = client.post("/consent/terms", json={"terms_version": T, "age_confirmed": True})
    assert resp.status_code == 404 and resp.json()["code"] == "user_not_found"
    assert [r["id"] for r in fake.store["users"]] == ["someone-else"]


def test_a_database_failure_while_saving_terms_answers_500_with_a_code(client, db, monkeypatch, capsys):
    """Returns HTTP 500 with code internal_error and a plain detail when the save
    fails, without exposing the database error text."""
    fake = db()
    monkeypatch.setattr(fake, "table", lambda name: (_ for _ in ()).throw(RuntimeError("secret db text")))
    resp = client.post("/consent/terms", json={"terms_version": T, "age_confirmed": True})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Your choice could not be saved. Please try again.", "code": "internal_error"}
    assert "secret db text" not in resp.text


# --- POST /consent/health -----------------------------------------------------------

def test_giving_health_consent_records_the_version_and_the_time(client, db):
    """Returns HTTP 200 and the consent object with health_consent_at set,
    health_consent_version equal to the current version, health_consent_withdrawn_at
    null and needs_health_consent false, and stores the same on the caller's row."""
    fake = db()
    resp = client.post("/consent/health", json={"health_version": H})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == CONSENT_KEYS
    assert body["health_consent_version"] == H and body["needs_health_consent"] is False
    assert is_iso_time(body["health_consent_at"]) and body["health_consent_withdrawn_at"] is None
    assert stored(fake)["health_consent_at"] == body["health_consent_at"]
    assert stored(fake)["terms_accepted_at"] is None


def test_giving_health_consent_again_clears_an_earlier_withdrawal(client, db):
    """Returns needs_health_consent false and health_consent_withdrawn_at null for a
    user who had withdrawn, and clears the stored withdrawal time."""
    fake = db(user_row(health_consent_at="2026-10-01T00:00:00+00:00", health_consent_version=H,
                       health_consent_withdrawn_at="2026-10-02T00:00:00+00:00"))
    resp = client.post("/consent/health", json={"health_version": H})
    assert resp.status_code == 200
    assert resp.json()["needs_health_consent"] is False
    assert stored(fake)["health_consent_withdrawn_at"] is None


def test_health_consent_with_a_stale_version_answers_409_with_the_current_version(client, db):
    """Returns HTTP 409 with code policy_version_changed and current_version set to
    the current health consent version when the posted version is not the current
    one, and stores nothing."""
    fake = db()
    resp = client.post("/consent/health", json={"health_version": "2020-01-01"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": resp.json()["detail"], "code": "policy_version_changed",
                           "current_version": H}
    assert stored(fake)["health_consent_at"] is None


def test_health_consent_with_a_missing_field_answers_422_with_a_code(client, db):
    """Returns HTTP 422 with code validation_error and a text detail when the body
    lacks health_version, and stores nothing."""
    fake = db()
    resp = client.post("/consent/health", json={})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error" and isinstance(resp.json()["detail"], str)
    assert stored(fake)["health_consent_at"] is None


def test_health_consent_needs_a_login(client, db):
    """Returns HTTP 401 for a request with no Authorization header."""
    from app.main import app
    from app.core.services.token import get_current_user_id
    db()
    app.dependency_overrides.pop(get_current_user_id, None)
    assert client.post("/consent/health", json={"health_version": H}).status_code == 401


def test_health_consent_for_a_user_who_no_longer_exists_answers_404(client, db):
    """Returns HTTP 404 with code user_not_found when no users row matches the caller."""
    db(user_row("someone-else"))
    resp = client.post("/consent/health", json={"health_version": H})
    assert resp.status_code == 404 and resp.json()["code"] == "user_not_found"


# --- DELETE /consent/health ---------------------------------------------------------

def test_withdrawing_health_consent_records_the_time_and_deletes_nothing(client, db):
    """Returns HTTP 200 and the consent object with health_consent_withdrawn_at set
    and needs_health_consent true, keeping the earlier consent time and version and
    every other table's rows."""
    fake = db(user_row(health_consent_at="2026-10-01T00:00:00+00:00", health_consent_version=H))
    fake.store["skin_logs"] = [{"id": "log-1", "user_id": "user-1"}]
    resp = client.delete("/consent/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == CONSENT_KEYS
    assert is_iso_time(body["health_consent_withdrawn_at"]) and body["needs_health_consent"] is True
    assert body["health_consent_at"] == "2026-10-01T00:00:00+00:00" and body["health_consent_version"] == H
    assert fake.store["skin_logs"] == [{"id": "log-1", "user_id": "user-1"}]


def test_withdrawing_twice_changes_nothing_the_second_time(client, db):
    """Returns HTTP 200 both times, and the second call keeps the first withdrawal
    time instead of replacing it."""
    fake = db(user_row(health_consent_at="2026-10-01T00:00:00+00:00", health_consent_version=H))
    first = client.delete("/consent/health").json()
    second = client.delete("/consent/health")
    assert second.status_code == 200
    assert second.json()["health_consent_withdrawn_at"] == first["health_consent_withdrawn_at"]
    assert stored(fake)["health_consent_withdrawn_at"] == first["health_consent_withdrawn_at"]


def test_withdrawing_when_never_consented_still_answers_200(client, db):
    """Returns HTTP 200 and needs_health_consent true for a user who never gave health
    consent."""
    db()
    resp = client.delete("/consent/health")
    assert resp.status_code == 200 and resp.json()["needs_health_consent"] is True


def test_withdrawing_touches_only_the_callers_row(client, db):
    """Leaves another user's health consent as it was."""
    fake = db(user_row(health_consent_at="2026-10-01T00:00:00+00:00", health_consent_version=H),
              user_row("user-2", health_consent_at="2026-10-01T00:00:00+00:00", health_consent_version=H))
    client.delete("/consent/health")
    assert stored(fake, "user-2")["health_consent_withdrawn_at"] is None


def test_withdrawing_then_consenting_again_restores_the_consent(client, db):
    """Returns needs_health_consent false after withdrawal followed by a new consent."""
    db()
    client.post("/consent/health", json={"health_version": H})
    assert client.delete("/consent/health").json()["needs_health_consent"] is True
    assert client.post("/consent/health", json={"health_version": H}).json()["needs_health_consent"] is False


def test_withdrawing_needs_a_login(client, db):
    """Returns HTTP 401 for a request with no Authorization header."""
    from app.main import app
    from app.core.services.token import get_current_user_id
    db()
    app.dependency_overrides.pop(get_current_user_id, None)
    assert client.delete("/consent/health").status_code == 401


def test_withdrawing_for_a_user_who_no_longer_exists_answers_404(client, db):
    """Returns HTTP 404 with code user_not_found when no users row matches the caller."""
    db(user_row("someone-else"))
    resp = client.delete("/consent/health")
    assert resp.status_code == 404 and resp.json()["code"] == "user_not_found"


# --- GET /auth/me -------------------------------------------------------------------

def test_get_me_returns_the_user_row_and_the_consent_object(client, db):
    """Returns HTTP 200 with every field of the user row as before (id, line_id,
    display_name, picture_url, skin_type, role) plus a consent object with the ten
    documented fields, needing both consents for a user who gave none."""
    db()
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    for key, value in user_row().items():
        assert body[key] == value
    assert set(body["consent"]) == CONSENT_KEYS
    assert body["consent"]["needs_terms"] is True and body["consent"]["needs_health_consent"] is True
    assert body["consent"]["current_terms_version"] == T and body["consent"]["current_health_version"] == H


def test_get_me_consent_reflects_what_the_user_has_given(client, db):
    """Returns a consent object with needs_terms false and needs_health_consent false
    for a user who accepted the current terms and gave current health consent."""
    t = "2026-10-06T08:00:00+00:00"
    db(user_row(terms_accepted_at=t, terms_version=T, age_confirmed_at=t,
                health_consent_at=t, health_consent_version=H))
    consent_obj = client.get("/auth/me").json()["consent"]
    assert consent_obj["needs_terms"] is False and consent_obj["needs_health_consent"] is False
    assert consent_obj["terms_accepted_at"] == t


def test_get_me_still_works_before_the_consent_columns_exist(client, db):
    """Returns HTTP 200 for a users row that has none of the six consent columns
    (migration 0014 not yet run), with a consent object that needs both consents."""
    row = {"id": "user-1", "line_id": "line-user-1", "display_name": "Test User", "role": "user"}
    db(row)
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    assert resp.json()["consent"]["needs_terms"] is True
    assert resp.json()["consent"]["needs_health_consent"] is True
