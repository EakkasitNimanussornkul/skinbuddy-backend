"""Unit tests for JWT issuing/verification (app/core/services/token.py).

No DB or network — just signing and verification against SUPABASE_JWT_SECRET
(forced to a dummy value by conftest.py).
"""

from datetime import datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config.setting import settings
from app.core.services.token import (
    create_supabase_compatible_token,
    get_current_user_id,
    get_optional_user_id,
)

USER_ID = "11111111-1111-1111-1111-111111111111"


def bearer(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def encode(payload):
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256")


# --- Happy path --------------------------------------------------------------

def test_issued_token_round_trips_to_the_same_user_id():
    token = create_supabase_compatible_token(USER_ID)
    assert get_current_user_id(bearer(token)) == USER_ID


def test_issued_token_is_supabase_shaped():
    payload = jwt.decode(
        create_supabase_compatible_token(USER_ID),
        settings.SUPABASE_JWT_SECRET,
        algorithms=["HS256"],
        audience="authenticated",
    )
    assert payload["sub"] == USER_ID
    assert payload["aud"] == "authenticated"
    assert payload["role"] == "authenticated"


def test_token_carries_no_authorisation_claims():
    """Authorisation must come from a live DB lookup, never from the token — a
    role baked into the JWT would stay valid until expiry after being revoked."""
    payload = jwt.decode(
        create_supabase_compatible_token(USER_ID),
        settings.SUPABASE_JWT_SECRET,
        algorithms=["HS256"],
        audience="authenticated",
    )
    # "role" here is Postgres' authenticated role, not an app permission level.
    assert payload["role"] == "authenticated"
    assert "admin" not in str(payload).lower()


# --- Rejection paths ---------------------------------------------------------

def test_missing_credentials_is_401():
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(None)
    assert exc.value.status_code == 401


def test_garbage_token_is_401():
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(bearer("not-a-jwt"))
    assert exc.value.status_code == 401


def test_expired_token_is_401():
    past = datetime.utcnow() - timedelta(days=1)
    expired = encode({"sub": USER_ID, "aud": "authenticated", "exp": int(past.timestamp())})
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(bearer(expired))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_token_signed_with_the_wrong_secret_is_rejected():
    forged = jwt.encode(
        {"sub": "attacker", "aud": "authenticated",
         "exp": int((datetime.utcnow() + timedelta(days=1)).timestamp())},
        "a-different-secret-long-enough-to-avoid-a-key-length-warning",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(bearer(forged))
    assert exc.value.status_code == 401


def test_token_with_wrong_audience_is_rejected():
    wrong_aud = encode({
        "sub": USER_ID, "aud": "anon",
        "exp": int((datetime.utcnow() + timedelta(days=1)).timestamp()),
    })
    with pytest.raises(HTTPException):
        get_current_user_id(bearer(wrong_aud))


# --- Optional (anonymous-tolerant) variant -----------------------------------

def test_optional_returns_none_when_unauthenticated():
    assert get_optional_user_id(None) is None


def test_optional_returns_none_instead_of_raising_on_a_bad_token():
    """Public endpoints degrade to anonymous rather than 401-ing."""
    assert get_optional_user_id(bearer("not-a-jwt")) is None


def test_optional_returns_the_user_id_for_a_valid_token():
    token = create_supabase_compatible_token(USER_ID)
    assert get_optional_user_id(bearer(token)) == USER_ID
