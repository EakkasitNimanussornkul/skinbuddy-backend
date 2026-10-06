"""Auth boundaries of the consent, deletion and check-in routes: each one needs a login.

Every assertion is about a request rejected before any query runs, so no database
is patched. The 401 comes from the shared login dependency, so its body is
{"detail": ...} with no code.
"""

import pytest

from app.core import consent

ROUTES = [
    ("post", "/consent/terms", {"terms_version": consent.TERMS_VERSION, "age_confirmed": True}),
    ("post", "/consent/health", {"health_version": consent.HEALTH_CONSENT_VERSION}),
    ("delete", "/consent/health", None),
    ("post", "/auth/me/delete", {"code": "abc"}),
    ("post", "/analysis/log", {"symptoms": [{"symptom": "redness", "severity": 2}]}),
]


def call(client, method, path, body, headers=None):
    kwargs = {"headers": headers} if headers else {}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_new_route_rejects_anonymous_requests(client, method, path, body):
    """Returns HTTP 401 for a request carrying no Authorization header, so the route
    is unreachable without a session."""
    resp = call(client, method, path, body)
    assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"
    assert set(resp.json()) == {"detail"}


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_new_route_rejects_a_forged_token(client, method, path, body):
    """Returns HTTP 401 for a request carrying a bearer token that fails signature
    verification, so a fabricated token grants no access."""
    resp = call(client, method, path, body, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"
