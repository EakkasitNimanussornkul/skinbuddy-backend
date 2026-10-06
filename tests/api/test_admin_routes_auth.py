"""Who may call the product-submission routes.

Every admin route answers 401 with no login or a bad one, and 403 to a logged-in
user whose users row is missing or is not role 'admin' (migration 0006). Both
live users are admins, so the normal-user refusal is proven here only.
"""

import pytest

from app.core.services.token import get_admin_user_id

SUB_ID = "5ab00000-0000-0000-0000-000000000001"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

ADMIN_ROUTES = [
    ("get", "/submissions/admin"),
    ("get", f"/submissions/admin/{SUB_ID}"),
    ("patch", f"/submissions/admin/{SUB_ID}"),
    ("post", f"/submissions/admin/{SUB_ID}/approve"),
    ("post", f"/submissions/admin/{SUB_ID}/reject"),
    ("patch", f"/products/{PROD_ID}"),
    ("post", f"/products/{PROD_ID}/image"),
    ("post", "/submissions/admin/cleanup-images"),
]

USER_ROUTES = [
    ("post", "/submissions/images"),
    ("post", "/submissions"),
    ("get", "/submissions/mine"),
]

AUTH_MODULES = ("app.core.services.token", "app.api.submissions", "app.api.products",
                "app.core.services.submission_service", "app.core.services.ingredient_lookup",
                "app.core.services.image_upload")


def call(client, method, path, headers=None):
    """A JSON body for the JSON routes, a valid PNG for the upload routes: the
    refusal must come from the login check, not from a malformed request."""
    kwargs = {"headers": headers} if headers else {}
    if path.endswith("/image") or path.endswith("/images"):
        kwargs["files"] = {"file": ("photo.png", PNG, "image/png")}
    elif method in ("post", "patch"):
        kwargs["json"] = {}
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method, path", ADMIN_ROUTES + USER_ROUTES)
def test_submission_route_rejects_anonymous_requests(client, method, path):
    """Returns HTTP 401 for a request with no Authorization header."""
    resp = call(client, method, path)
    assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"


@pytest.mark.parametrize("method, path", ADMIN_ROUTES + USER_ROUTES)
def test_submission_route_rejects_a_forged_token(client, method, path):
    """Returns HTTP 401 for a bearer token that fails signature verification."""
    resp = call(client, method, path, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_admin_route_refuses_a_normal_user(client, patch_backend, as_user, method, path):
    """Returns HTTP 403 "Admin access required" to a logged-in user whose role is
    'user', and nothing is written, uploaded or sent to the database functions."""
    fake = patch_backend({"users": [{"id": "user-1", "role": "user"}],
                          "product_submissions": [{"id": SUB_ID, "status": "pending", "payload": {}}],
                          "products": [{"id": PROD_ID}]}, *AUTH_MODULES)
    as_user("user-1")
    resp = call(client, method, path)
    assert resp.status_code == 403, f"{method.upper()} {path} returned {resp.status_code}"
    assert resp.json() == {"detail": "Admin access required"}
    assert fake.rpc_calls == [] and fake.storage.uploads == []
    assert fake.store["product_submissions"] == [{"id": SUB_ID, "status": "pending", "payload": {}}]


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_admin_route_refuses_a_login_with_no_users_row(client, patch_backend, as_user, method, path):
    """Returns HTTP 403, not 500, when the token is valid but no users row has its id."""
    patch_backend({"users": []}, *AUTH_MODULES)
    as_user("ghost-user")
    resp = call(client, method, path)
    assert resp.status_code == 403, f"{method.upper()} {path} returned {resp.status_code}"


def test_admin_dependency_returns_the_admins_id(patch_backend):
    """get_admin_user_id returns the caller's id when their users row has role 'admin'."""
    patch_backend({"users": [{"id": "admin-1", "role": "admin"}, {"id": "user-1", "role": "user"}]},
                  "app.core.services.token")
    assert get_admin_user_id("admin-1") == "admin-1"


@pytest.mark.parametrize("path", ["/meta/categories", "/meta/concern-tags", "/meta/functional-groups",
                                  "/ingredients/search?q=water"])
def test_meta_and_ingredient_reads_need_no_login(client, patch_backend, path):
    """Returns HTTP 200 with no Authorization header: the lists and search are public."""
    patch_backend({"ingredients": []}, "app.core.services.ingredient_lookup")
    assert client.get(path).status_code == 200


def test_ingredient_match_needs_no_login(client, patch_backend):
    """POST /ingredients/match returns HTTP 200 with no Authorization header."""
    patch_backend({"ingredients": []}, "app.core.services.ingredient_lookup")
    assert client.post("/ingredients/match", json={"names": ["Water"]}).status_code == 200
