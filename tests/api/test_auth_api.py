"""API-level tests for the Auth router's profile update (PATCH /auth/me).

MD-55. Covers the four outcomes the handler can produce: a successful update, a
skin_type the request model rejects before the handler body runs, an update that
matches no user row, and a database failure.

Whether the route requires a token at all is covered by
tests/api/test_auth_boundaries.py and deliberately not repeated here.

The Supabase client is replaced with an in-memory fake (see tests/conftest.py)
so nothing touches the live database.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest


def user_row(user_id="user-1", skin_type="DSPT"):
    return {"id": user_id, "line_id": f"line-{user_id}", "display_name": "Test User",
            "skin_type": skin_type}


# --- Success -----------------------------------------------------------------

def test_update_me_stores_a_valid_baumann_code(client, patch_supabase, as_user):
    """Returns HTTP 200 and the caller's updated user record, carrying the new
    skin_type."""
    as_user("user-1")
    fake = patch_supabase({"users": [user_row(skin_type="ORNT")]}, "app.api.auth")

    resp = client.patch("/auth/me", json={"skin_type": "DSPT"})
    assert resp.status_code == 200

    body = resp.json()
    assert body["id"] == "user-1"
    assert body["skin_type"] == "DSPT"
    assert fake.store["users"][0]["skin_type"] == "DSPT"


def test_update_me_writes_only_to_the_calling_users_row(client, patch_supabase, as_user):
    """Leaves another user's skin_type untouched, since the update is scoped to
    the id taken from the caller's token rather than from the request body."""
    as_user("user-1")
    fake = patch_supabase(
        {"users": [user_row("user-1", "ORNT"), user_row("user-2", "ORNT")]},
        "app.api.auth",
    )

    resp = client.patch("/auth/me", json={"skin_type": "DSPT"})
    assert resp.status_code == 200

    stored = {u["id"]: u["skin_type"] for u in fake.store["users"]}
    assert stored == {"user-1": "DSPT", "user-2": "ORNT"}


# --- Invalid format ----------------------------------------------------------

@pytest.mark.parametrize("bad_code", ["", "DS", "DSPTX", "XXXX", "dspt", "1234"])
def test_update_me_rejects_a_skin_type_that_is_not_a_baumann_code(
        client, patch_supabase, as_user, bad_code):
    """Returns HTTP 422 without reaching the handler, for any value that is not
    one of the sixteen valid Baumann codes.

    UserUpdateRequest applies validate_baumann_skin_type as a field validator, so
    the rejection happens during request parsing. "dspt" is included because the
    pattern is case-sensitive, and "DSPTX" because a length check alone would let
    it through."""
    as_user("user-1")
    patch_supabase({"users": [user_row(skin_type="ORNT")]}, "app.api.auth")

    resp = client.patch("/auth/me", json={"skin_type": bad_code})
    assert resp.status_code == 422


def test_update_me_leaves_the_row_untouched_when_the_code_is_rejected(
        client, patch_supabase, as_user):
    """Leaves the stored skin_type as it was when validation fails, confirming
    the 422 is raised before any write is attempted."""
    as_user("user-1")
    fake = patch_supabase({"users": [user_row(skin_type="ORNT")]}, "app.api.auth")

    client.patch("/auth/me", json={"skin_type": "XXXX"})
    assert fake.store["users"][0]["skin_type"] == "ORNT"


# --- No matching user --------------------------------------------------------

def test_update_me_404s_when_no_user_row_matches(client, patch_supabase, as_user):
    """Returns HTTP 404 with the detail "User not found or update failed" when
    the update matches no row, rather than reporting a database failure for a
    user who simply is not there."""
    as_user("user-1")
    patch_supabase({"users": []}, "app.api.auth")

    resp = client.patch("/auth/me", json={"skin_type": "DSPT"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found or update failed"


def test_update_me_404s_when_the_row_belongs_to_another_user(client, patch_supabase, as_user):
    """Returns HTTP 404 when the only user row belongs to somebody else, so a
    valid token cannot edit a profile it does not own."""
    as_user("user-1")
    patch_supabase({"users": [user_row("user-2", "ORNT")]}, "app.api.auth")

    resp = client.patch("/auth/me", json={"skin_type": "DSPT"})
    assert resp.status_code == 404


# --- Database error ----------------------------------------------------------

def test_update_me_reports_a_database_failure_as_a_500(
        client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with a detail containing "Database error" when the
    update call raises."""
    import app.api.auth as auth_module

    as_user("user-1")
    patch_supabase({"users": [user_row()]}, "app.api.auth")

    def explode(*_a, **_k):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(auth_module.supabase, "table", explode)

    resp = client.patch("/auth/me", json={"skin_type": "DSPT"})
    assert resp.status_code == 500
    assert "Database error" in resp.json()["detail"]
