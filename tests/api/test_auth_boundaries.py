"""Auth-boundary tests: which routes require a token, and which tolerate anonymity.

No Supabase patching needed — every assertion here is about requests that are
rejected before any query runs. This is the cheapest regression net in the suite:
it catches an endpoint accidentally losing its auth dependency.
"""

import pytest

PROTECTED_ROUTES = [
    ("get", "/shelf/"),
    ("post", "/shelf/add"),
    ("delete", "/shelf/some-item-id"),
    ("get", "/shelf/analyze/some-product-id"),
    ("patch", "/shelf/some-item-id/open"),
    ("patch", "/shelf/some-item-id/status"),
    ("post", "/quiz/save"),
    ("get", "/auth/me"),
    ("patch", "/auth/me"),
    ("get", "/routine/"),
    ("post", "/routine/generate"),
    ("get", "/analysis/report"),
    ("post", "/chat/ask"),
]


def call(client, method, path, headers=None):
    """httpx only accepts a body on methods that take one."""
    kwargs = {"headers": headers} if headers else {}
    if method in ("post", "patch", "put"):
        kwargs["json"] = {}
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method, path", PROTECTED_ROUTES)
def test_protected_route_rejects_anonymous_requests(client, method, path):
    """Returns HTTP 401 for a request carrying no Authorization header, so the
    route is unreachable without a session."""
    resp = call(client, method, path)
    assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"


@pytest.mark.parametrize("method, path", PROTECTED_ROUTES)
def test_protected_route_rejects_a_forged_token(client, method, path):
    """Returns HTTP 401 for a request carrying a bearer token that fails
    signature verification, so a fabricated token grants no access."""
    resp = call(client, method, path, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"


def test_health_endpoint_is_public(client):
    assert client.get("/health/").status_code == 200


def test_openapi_schema_is_served(client):
    """A broken response model surfaces here before it reaches a user."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/products/search" in resp.json()["paths"]


def test_cors_allows_the_local_frontend_origin(client):
    resp = client.get("/health/", headers={"Origin": "http://localhost:5173"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
