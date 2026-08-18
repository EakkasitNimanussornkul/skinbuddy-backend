"""API-level tests for the Quiz router — POST /quiz/save.

Covers what `test_auth_boundaries.py` cannot: what the handler writes once a
valid request gets through. Auth rejection is proven there and not repeated.

The handler performs two writes with no read-back, so the response body reveals
almost nothing about whether they were correct. These tests assert on the fake
client's store instead — see tests/conftest.py's FakeQuery, which had to learn
insert/update before any of this was observable.
"""

import pytest

PAYLOAD = {"skinType": "DSPT", "scores": {"hydration": 1.0, "sebum": 4.0}}


def users(*rows):
    return [{"id": uid, "skin_type": skin} for uid, skin in rows]


# --- the happy path ----------------------------------------------------------

def test_quiz_save_returns_a_success_message(client, patch_supabase, as_user):
    """Returns HTTP 200 and {"message": "Quiz results saved successfully!"} for a
    valid, authenticated save."""
    as_user("user-1")
    patch_supabase({"quiz_results": [], "users": users(("user-1", None))}, "app.api.quiz")

    resp = client.post("/quiz/save", json=PAYLOAD)
    assert resp.status_code == 200
    assert resp.json() == {"message": "Quiz results saved successfully!"}


def test_quiz_save_inserts_a_row_carrying_the_submitted_answers(client, patch_supabase, as_user):
    """Inserts exactly one quiz_results row holding the caller's user_id, the
    submitted skin type and the submitted scores."""
    as_user("user-1")
    fake = patch_supabase({"quiz_results": [], "users": users(("user-1", None))}, "app.api.quiz")

    client.post("/quiz/save", json=PAYLOAD)

    saved = fake.store["quiz_results"]
    assert len(saved) == 1
    assert saved[0]["user_id"] == "user-1"
    assert saved[0]["skin_type"] == "DSPT"
    assert saved[0]["scores"] == {"hydration": 1.0, "sebum": 4.0}


def test_quiz_save_takes_the_user_id_from_the_session_not_the_body(client, patch_supabase, as_user):
    """Stores the authenticated caller's id on the quiz_results row even when the
    request body tries to supply a different one, so a caller cannot write a
    quiz result against someone else's account."""
    as_user("user-1")
    fake = patch_supabase({"quiz_results": [], "users": users(("user-1", None))}, "app.api.quiz")

    client.post("/quiz/save", json={**PAYLOAD, "user_id": "user-2"})

    assert fake.store["quiz_results"][0]["user_id"] == "user-1"


def test_quiz_save_updates_only_the_callers_profile(client, patch_supabase, as_user):
    """Sets the caller's users.skin_type to the submitted code and leaves every
    other user's skin_type untouched."""
    as_user("user-1")
    fake = patch_supabase(
        {"quiz_results": [], "users": users(("user-1", None), ("user-2", "ORNT"))},
        "app.api.quiz",
    )

    client.post("/quiz/save", json=PAYLOAD)

    stored = {row["id"]: row["skin_type"] for row in fake.store["users"]}
    assert stored == {"user-1": "DSPT", "user-2": "ORNT"}


# --- validation --------------------------------------------------------------

def test_quiz_save_rejects_a_malformed_skin_type_before_writing(client, patch_supabase, as_user):
    """Returns HTTP 422 and writes no quiz_results row for a skin type that is
    not one of the sixteen Baumann codes, so validation runs before any database
    write rather than after."""
    as_user("user-1")
    fake = patch_supabase({"quiz_results": [], "users": users(("user-1", None))}, "app.api.quiz")

    resp = client.post("/quiz/save", json={"skinType": "XYZ1", "scores": {"hydration": 1.0}})

    assert resp.status_code == 422
    assert fake.store["quiz_results"] == []


# --- failure handling --------------------------------------------------------

def test_quiz_save_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to save quiz results." and
    no trace of the internal exception text, when a write raises."""
    import app.api.quiz as quiz_module

    as_user("user-1")
    patch_supabase({"quiz_results": [], "users": users(("user-1", None))}, "app.api.quiz")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(quiz_module.supabase, "table", explode)

    resp = client.post("/quiz/save", json=PAYLOAD)
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to save quiz results."


def test_quiz_save_reports_failure_when_the_profile_row_is_missing(client, patch_supabase, as_user):
    """Returns HTTP 404 when the caller has no users row, so a quiz result is
    never reported as saved while the profile silently did not change.

    Regression guard for BE-DEF-05: the handler previously ignored the result of
    the users update and answered "Quiz results saved successfully!" regardless."""
    as_user("user-1")
    patch_supabase({"quiz_results": [], "users": []}, "app.api.quiz")

    resp = client.post("/quiz/save", json=PAYLOAD)
    assert resp.status_code == 404
