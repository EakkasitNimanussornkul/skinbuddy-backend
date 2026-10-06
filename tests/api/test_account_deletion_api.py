"""Account deletion: POST /auth/me/delete.

The database, the storage bucket and LINE are all fakes: the database and bucket
are in tests/conftest.py, and LINE is an httpx MockTransport that records every
request it receives, so no real LINE endpoint is ever called. The steps, in order:
the user's row, the LINE code exchange, the LINE profile, delete_user_account(),
the photo cleanup and the Deauthorize call (app/core/services/account_deletion_service.py).
Docstrings state the expected output and are lifted verbatim into the Test Record.
"""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from postgrest.exceptions import APIError

from app.config.setting import settings
from app.core.services import account_deletion_service as service

DELETE_URI = "http://localhost:5173/account/delete/callback"
USER_TOKEN, CHANNEL_TOKEN, CODE = "user-token-AAA", "channel-token-BBB", "one-time-code-CCC"
PUBLIC = "https://test-project.supabase.co/storage/v1/object/public/product-images/"
PHOTO_USED = "submissions/0f8fad5b-d9cb-469f-a165-70867728950e.png"        # a product still shows it
PHOTO_FREE = "submissions/7c9e6679-7425-40de-944b-e07fc1f90ae7.jpg"        # nothing uses it
PHOTO_PENDING = "submissions/16fd2706-8baf-433b-82eb-8c7fada847da.webp"    # another user's pending submission
PHOTOS = [PHOTO_USED, PHOTO_FREE, PHOTO_PENDING]
MODULES = ("app.api.account_deletion", "app.core.services.image_upload",
           "app.core.services.ingredient_lookup")
RPC_RESULT = {"deleted": {"users": 1}, "cleared": {"product_submissions.reviewed_by": 0},
              "image_paths": PHOTOS}
FAILED_SIGN_IN = {"detail": "LINE sign-in could not be confirmed. Please try again.",
                  "code": "line_signin_failed"}
NOT_DELETED = {"detail": "The account could not be deleted. Nothing was removed.", "code": "internal_error"}
ADMIN_REFUSED = {"detail": "Admin accounts can't be deleted here. Ask the owner to change this account "
                           "to a normal user first.", "code": "admin_account"}


class Line:
    """A fake LINE. Each endpoint answers from `answers`, or raises if the answer is an Exception."""

    def __init__(self):
        self.requests = []
        self.answers = {
            "/oauth2/v2.1/token": (200, {"access_token": USER_TOKEN}),
            "/v2/profile": (200, {"userId": "line-user-1", "displayName": "Kla"}),
            "/oauth2/v3/token": (200, {"access_token": CHANNEL_TOKEN}),
            "/user/v1/deauthorize": (204, None),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[request.url.path]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    @property
    def paths(self):
        return [r.url.path for r in self.requests]

    def request(self, path):
        return next(r for r in self.requests if r.url.path == path)

    def form(self, path):
        return {k: v[0] for k, v in parse_qs(self.request(path).content.decode()).items()}


def user_row(user_id="user-1", role="user", line_id="line-user-1"):
    return {"id": user_id, "line_id": line_id, "display_name": "Kla", "role": role}


@pytest.fixture
def world(patch_backend, as_user, monkeypatch):
    """user-1 is logged in; LINE is a recording fake; the deletion callback is configured."""
    as_user("user-1")
    monkeypatch.setattr(settings, "LINE_DELETE_REDIRECT_URI", DELETE_URI)
    line = Line()
    monkeypatch.setattr(service, "line_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(line.handler)))

    def _make(users=None, products=(), submissions=(), stored=PHOTOS):
        fake = patch_backend({"users": users if users is not None else [user_row()],
                              "products": list(products), "product_submissions": list(submissions)},
                             *MODULES)
        for key in stored:
            fake.storage.put(key, hours_old=1)
        fake.rpc_results["delete_user_account"] = dict(RPC_RESULT)
        return fake, line
    return _make


def delete(client, code=CODE):
    return client.post("/auth/me/delete", json={"code": code})


def rpc_names(fake):
    return [name for name, _ in fake.rpc_calls]


# --- the whole flow -----------------------------------------------------------------

def test_deleting_an_account_runs_every_step_and_answers_200(client, world):
    """Returns HTTP 200 with {"deleted": true, "line_deauthorized": true} after
    exchanging the code, reading the LINE profile, calling delete_user_account for
    the caller, then issuing a channel token and calling LINE's Deauthorize, in that
    order and no other LINE call."""
    fake, line = world(products=[{"id": "p1", "image_url": PUBLIC + PHOTO_USED}],
                       submissions=[{"id": "s9", "submitted_by": "user-2", "status": "pending",
                                     "payload": {"image_path": PHOTO_PENDING}, "edited_payload": None}])
    resp = delete(client)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "line_deauthorized": True}
    assert line.paths == ["/oauth2/v2.1/token", "/v2/profile", "/oauth2/v3/token", "/user/v1/deauthorize"]
    assert fake.rpc_calls == [("delete_user_account", {"p_user_id": "user-1"})]


def test_the_code_is_exchanged_at_the_deletion_callback(client, world):
    """Sends LINE's token endpoint grant_type authorization_code with the code from the
    request, the channel id and secret, and redirect_uri equal to LINE_DELETE_REDIRECT_URI
    (not the login callback)."""
    _, line = world()
    delete(client)
    form = line.form("/oauth2/v2.1/token")
    assert form == {"grant_type": "authorization_code", "code": CODE, "redirect_uri": DELETE_URI,
                    "client_id": settings.LINE_CHANNEL_ID, "client_secret": settings.LINE_CHANNEL_SECRET}
    assert form["redirect_uri"] != settings.LINE_REDIRECT_URI


def test_the_profile_is_read_with_the_fresh_user_token(client, world):
    """Calls LINE's profile endpoint with the user access token the exchange returned."""
    _, line = world()
    delete(client)
    assert line.request("/v2/profile").headers["authorization"] == f"Bearer {USER_TOKEN}"


def test_deauthorize_sends_the_channel_token_and_the_user_token(client, world):
    """Calls LINE's Deauthorize endpoint with the stateless channel token as the bearer
    credential and the user's access token as userAccessToken, after asking LINE for
    that channel token with grant_type client_credentials, the channel id and the
    secret, and no authorization code."""
    _, line = world()
    delete(client)
    channel_form = line.form("/oauth2/v3/token")
    assert channel_form == {"grant_type": "client_credentials", "client_id": settings.LINE_CHANNEL_ID,
                            "client_secret": settings.LINE_CHANNEL_SECRET}
    call = line.request("/user/v1/deauthorize")
    assert call.method == "POST" and call.headers["authorization"] == f"Bearer {CHANNEL_TOKEN}"
    assert json.loads(call.content) == {"userAccessToken": USER_TOKEN}
    assert call.headers["content-type"].startswith("application/json")


# --- before LINE is touched ---------------------------------------------------------

def test_deleting_without_a_login_answers_401_and_calls_nothing(client, world):
    """Returns HTTP 401 for a request with no Authorization header, calling neither
    LINE nor the database."""
    from app.main import app
    from app.core.services.token import get_current_user_id
    fake, line = world()
    app.dependency_overrides.pop(get_current_user_id, None)
    resp = delete(client)
    assert resp.status_code == 401
    assert line.requests == [] and fake.rpc_calls == []


def test_deleting_when_the_callback_is_not_configured_answers_503(client, world, monkeypatch):
    """Returns HTTP 503 with code deletion_not_configured and detail 'Account deletion
    is not configured yet.' when LINE_DELETE_REDIRECT_URI is not set, before reading
    the user or calling LINE or the database."""
    fake, line = world()
    monkeypatch.setattr(settings, "LINE_DELETE_REDIRECT_URI", None)
    resp = delete(client)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Account deletion is not configured yet.", "code": "deletion_not_configured"}
    assert line.requests == [] and fake.rpc_calls == []


@pytest.mark.parametrize("body", [{}, {"code": ""}, {"code": 5}, {"nope": "x"}])
def test_deleting_without_a_usable_code_answers_422_with_a_code(client, world, body):
    """Returns HTTP 422 with code validation_error and a text detail when the body has
    no code, an empty code or a code that is not text, calling nothing."""
    fake, line = world()
    resp = client.post("/auth/me/delete", json=body)
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error" and isinstance(resp.json()["detail"], str)
    assert line.requests == [] and fake.rpc_calls == []


def test_deleting_for_a_user_who_is_gone_answers_404_before_calling_LINE(client, world):
    """Returns HTTP 404 with code user_not_found when the caller's users row does not
    exist, without using the one-time LINE code and without calling the database."""
    fake, line = world(users=[user_row("someone-else")])
    resp = delete(client)
    assert resp.status_code == 404 and resp.json()["code"] == "user_not_found"
    assert line.requests == [] and fake.rpc_calls == []


def test_an_admin_account_is_refused_with_409_before_calling_LINE(client, world):
    """Returns HTTP 409 with code admin_account and a message telling the owner to
    change the account to a normal user first, without using the one-time LINE code and
    without calling the database."""
    fake, line = world(users=[user_row(role="admin")])
    resp = delete(client)
    assert resp.status_code == 409 and resp.json() == ADMIN_REFUSED
    assert line.requests == [] and fake.rpc_calls == []


def test_a_database_failure_reading_the_user_answers_500_and_calls_nothing(client, world, monkeypatch):
    """Returns HTTP 500 with code internal_error and the detail 'The account could not
    be deleted. Nothing was removed.' when the user row cannot be read, calling neither
    LINE nor delete_user_account."""
    fake, line = world()
    monkeypatch.setattr(fake, "table", lambda name: (_ for _ in ()).throw(RuntimeError("down")))
    resp = delete(client)
    assert resp.status_code == 500 and resp.json() == NOT_DELETED
    assert line.requests == [] and fake.rpc_calls == []


# --- LINE sign-in must be confirmed -------------------------------------------------

@pytest.mark.parametrize("answer", [
    (400, {"error": "invalid_grant"}),
    (500, None),
    (200, {"token_type": "Bearer"}),
    httpx.ConnectError("no route"),
    httpx.ReadTimeout("slow"),
], ids=["refused", "server-error", "no-access-token", "network-error", "timeout"])
def test_a_failed_code_exchange_answers_400_and_deletes_nothing(client, world, answer):
    """Returns HTTP 400 with code line_signin_failed when LINE refuses the code, answers
    without an access token or cannot be reached, calling neither the profile endpoint
    nor the database."""
    fake, line = world()
    line.answers["/oauth2/v2.1/token"] = answer
    resp = delete(client)
    assert resp.status_code == 400 and resp.json() == FAILED_SIGN_IN
    assert line.paths == ["/oauth2/v2.1/token"] and fake.rpc_calls == []


@pytest.mark.parametrize("answer", [
    (401, {"message": "bad"}),
    (200, {"displayName": "no id"}),
    httpx.ConnectError("no route"),
], ids=["refused", "no-user-id", "network-error"])
def test_a_failed_profile_read_answers_400_and_deletes_nothing(client, world, answer):
    """Returns HTTP 400 with code line_signin_failed when the LINE profile cannot be
    read or has no userId, without calling the database."""
    fake, line = world()
    line.answers["/v2/profile"] = answer
    resp = delete(client)
    assert resp.status_code == 400 and resp.json() == FAILED_SIGN_IN
    assert fake.rpc_calls == [] and "/oauth2/v3/token" not in line.paths


def test_a_different_line_account_is_refused_with_403_and_nothing_is_deleted(client, world):
    """Returns HTTP 403 with code line_account_mismatch and detail 'That LINE account is
    not the one you are signed in with.' when the LINE profile's userId is not the
    caller's line_id, without calling the database or Deauthorize."""
    fake, line = world()
    line.answers["/v2/profile"] = (200, {"userId": "someone-elses-line-id"})
    resp = delete(client)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "That LINE account is not the one you are signed in with.",
                           "code": "line_account_mismatch"}
    assert fake.rpc_calls == [] and "/user/v1/deauthorize" not in line.paths


def test_a_user_row_without_a_line_id_cannot_match(client, world):
    """Returns HTTP 403 line_account_mismatch for a caller whose row has no line_id,
    without calling the database."""
    fake, line = world(users=[user_row(line_id=None)])
    resp = delete(client)
    assert resp.status_code == 403 and resp.json()["code"] == "line_account_mismatch"
    assert fake.rpc_calls == []


# --- the database call --------------------------------------------------------------

@pytest.mark.parametrize("code, status, body", [
    ("SBNFD", 404, {"detail": "User not found.", "code": "user_not_found"}),
    ("SBADM", 409, ADMIN_REFUSED),
    ("23503", 500, NOT_DELETED),
    ("XX000", 500, NOT_DELETED),
])
def test_database_errors_become_http_errors_and_nothing_after_them_runs(client, world, code, status, body):
    """Maps delete_user_account's SBNFD to HTTP 404 user_not_found, SBADM to HTTP 409
    admin_account and any other database error to HTTP 500 internal_error with the
    detail 'The account could not be deleted. Nothing was removed.', and then deletes
    no photo and does not call Deauthorize."""
    fake, line = world()
    fake.rpc_results["delete_user_account"] = APIError({"message": "db text", "code": code, "details": None, "hint": None})
    resp = delete(client)
    assert resp.status_code == status and resp.json() == body
    assert fake.storage.removals == [] and "/user/v1/deauthorize" not in line.paths


def test_a_non_database_exception_from_the_rpc_answers_500(client, world):
    """Returns HTTP 500 internal_error when the database call raises something other
    than a database error, and calls neither the photo cleanup nor Deauthorize."""
    fake, line = world()
    fake.rpc_results["delete_user_account"] = RuntimeError("connection reset")
    resp = delete(client)
    assert resp.status_code == 500 and resp.json() == NOT_DELETED
    assert fake.storage.removals == [] and "/user/v1/deauthorize" not in line.paths


# --- the photos ---------------------------------------------------------------------

def test_photos_of_the_deleted_submissions_go_unless_something_uses_them(client, world):
    """Deletes a photo of a deleted submission that nothing uses, and keeps one that a
    product still shows and one that another user's pending submission names."""
    fake, _ = world(products=[{"id": "p1", "image_url": PUBLIC + PHOTO_USED}],
                    submissions=[{"id": "s9", "submitted_by": "user-2", "status": "pending",
                                  "payload": {"image_path": PHOTO_PENDING}, "edited_payload": None}])
    assert delete(client).status_code == 200
    assert fake.storage.keys() == sorted([PHOTO_USED, PHOTO_PENDING])


def test_a_storage_failure_does_not_fail_the_deletion(client, world, capsys):
    """Returns HTTP 200 {"deleted": true, "line_deauthorized": true} even though the
    storage delete raises, and prints the failure."""
    fake, line = world()
    fake.storage.remove_error = RuntimeError("storage down")
    resp = delete(client)
    assert resp.status_code == 200 and resp.json() == {"deleted": True, "line_deauthorized": True}
    assert "storage down" in capsys.readouterr().out
    assert "/user/v1/deauthorize" in line.paths


def test_an_unreadable_reference_check_does_not_fail_the_deletion(client, world, monkeypatch):
    """Returns HTTP 200 and keeps every photo when the check of what still uses them
    fails, because a photo is only deleted after that check succeeds."""
    fake, _ = world()
    from app.core.services import image_upload
    monkeypatch.setattr(image_upload, "referenced_upload_paths",
                        lambda: (_ for _ in ()).throw(RuntimeError("cannot read")))
    resp = delete(client)
    assert resp.status_code == 200 and fake.storage.keys() == sorted(PHOTOS)


def test_paths_that_are_not_our_uploads_are_never_deleted(client, world):
    """Deletes nothing for image_paths that are not our upload paths (a seed image, an
    external URL, a path with a folder escape) and still answers 200."""
    fake, _ = world(stored=["Products/seed.png", "products/seed.png"])
    fake.rpc_results["delete_user_account"] = {
        "deleted": {}, "image_paths": ["Products/seed.png", "https://img.example/a.png", "../x.png", None, 7]}
    assert delete(client).status_code == 200
    assert fake.storage.removals == [] and fake.storage.keys() == ["Products/seed.png", "products/seed.png"]


def test_a_result_without_a_photo_list_still_succeeds(client, world):
    """Returns HTTP 200 when the database answers without an image_paths list."""
    fake, _ = world()
    fake.rpc_results["delete_user_account"] = {"deleted": {"users": 1}}
    assert delete(client).status_code == 200 and fake.storage.removals == []


# --- Deauthorize is best-effort -----------------------------------------------------

@pytest.mark.parametrize("path, answer", [
    ("/user/v1/deauthorize", (400, {"message": "Invalid access token for the target user."})),
    ("/user/v1/deauthorize", (200, {})),
    ("/user/v1/deauthorize", (500, None)),
    ("/user/v1/deauthorize", httpx.ConnectError("no route")),
    ("/oauth2/v3/token", (401, {"error": "invalid_client"})),
    ("/oauth2/v3/token", (200, {"token_type": "Bearer"})),
    ("/oauth2/v3/token", httpx.ReadTimeout("slow")),
], ids=["deauth-400", "deauth-200", "deauth-500", "deauth-network", "channel-401", "channel-no-token",
        "channel-timeout"])
def test_a_deauthorize_failure_still_answers_200_with_the_flag_false(client, world, path, answer):
    """Returns HTTP 200 {"deleted": true, "line_deauthorized": false} when LINE refuses
    the channel token, refuses Deauthorize, answers anything but 204 or cannot be
    reached; delete_user_account has already been called and its photos cleaned up."""
    fake, line = world()
    line.answers[path] = answer
    resp = delete(client)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "line_deauthorized": False}
    assert rpc_names(fake) == ["delete_user_account"] and fake.storage.keys() != sorted(PHOTOS)


def test_a_failed_deauthorize_is_printed_without_any_token_or_secret(client, world, capsys):
    """Prints that Deauthorize failed, naming the HTTP status, and prints no access
    token, no authorization code and no channel secret."""
    _, line = world()
    line.answers["/user/v1/deauthorize"] = (400, {"message": "Invalid access token for the target user."})
    delete(client)
    out = capsys.readouterr().out
    assert "Deauthorize" in out and "400" in out
    for secret in (USER_TOKEN, CHANNEL_TOKEN, CODE, settings.LINE_CHANNEL_SECRET):
        assert secret not in out


@pytest.mark.parametrize("step", ["/oauth2/v2.1/token", "/v2/profile"])
def test_a_failed_sign_in_prints_no_token_or_secret(client, world, capsys, step):
    """Prints nothing that contains the authorization code, an access token or the
    channel secret when the code exchange or the profile read fails."""
    _, line = world()
    line.answers[step] = (400, {"error": "bad", "access_token": USER_TOKEN})
    delete(client)
    out = capsys.readouterr().out
    for secret in (USER_TOKEN, CHANNEL_TOKEN, CODE, settings.LINE_CHANNEL_SECRET):
        assert secret not in out


def test_the_response_carries_no_token(client, world):
    """Returns a body of exactly deleted and line_deauthorized, with no token or code."""
    world()
    resp = delete(client)
    assert set(resp.json()) == {"deleted", "line_deauthorized"}
    assert USER_TOKEN not in resp.text and CHANNEL_TOKEN not in resp.text


# --- once only ----------------------------------------------------------------------

def test_an_account_cannot_be_deleted_twice(client, world):
    """Returns HTTP 404 user_not_found on a second request once the users row is gone,
    without calling LINE or the database again."""
    fake, line = world()
    assert delete(client).status_code == 200
    fake.store["users"].clear()                       # what delete_user_account does to the row
    calls_before, rpc_before = len(line.requests), len(fake.rpc_calls)
    resp = delete(client)
    assert resp.status_code == 404 and resp.json()["code"] == "user_not_found"
    assert len(line.requests) == calls_before and len(fake.rpc_calls) == rpc_before


def test_deleting_leaves_other_users_rows_in_the_fake_untouched(client, world):
    """Calls delete_user_account only with the caller's id, never an id taken from the
    request body, so another user cannot be named by a crafted request."""
    fake, _ = world(users=[user_row(), user_row("user-2", line_id="line-user-2")])
    client.post("/auth/me/delete", json={"code": CODE, "user_id": "user-2", "p_user_id": "user-2"})
    assert fake.rpc_calls == [("delete_user_account", {"p_user_id": "user-1"})]


# --- the route's own shape ----------------------------------------------------------

def test_the_route_is_post_and_is_in_the_openapi_schema(client):
    """Lists POST /auth/me/delete in the OpenAPI document, with no DELETE method on /auth/me."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/auth/me/delete"]
    assert "delete" not in paths["/auth/me"]


def test_a_deleted_users_token_still_verifies_and_the_profile_answers_404(client, patch_supabase):
    """Returns HTTP 404 'User not found' from GET /auth/me, not 401, for a validly signed
    token whose user row is gone: deleting an account does not revoke its token."""
    patch_supabase({"users": []}, "app.api.auth")
    from app.core.services.token import create_supabase_compatible_token
    token = create_supabase_compatible_token("user-1")
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
