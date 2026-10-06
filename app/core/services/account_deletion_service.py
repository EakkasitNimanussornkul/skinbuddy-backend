"""The LINE side of deleting an account, and the photo cleanup.

Order of POST /auth/me/delete (app/api/account_deletion.py), stopping at the first
failure before step 4:
  1. load the user's row (404, or 409 for an admin) BEFORE touching LINE, because
     an authorization code can be used once;
  2. exchange the fresh LINE code for a user access token;
  3. read the LINE profile; its userId must be this account's line_id;
  4. delete_user_account() in the database (migration 0014), one transaction;
  5. delete the photos of the deleted submissions that nothing else uses;
  6. Deauthorize the app on LINE, best-effort.
After step 4 nothing can undo the deletion or fail the request: a photo or
Deauthorize failure is printed and reported, not raised.

Nothing here ever prints a token, a code or the channel secret.
"""

from typing import Any, Dict

import httpx

from app.config.setting import settings
from app.core.services.image_upload import delete_upload_if_unused

LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_PROFILE_URL = "https://api.line.me/v2/profile"
LINE_CHANNEL_TOKEN_URL = "https://api.line.me/oauth2/v3/token"
LINE_DEAUTHORIZE_URL = "https://api.line.me/user/v1/deauthorize"
LINE_TIMEOUT_SECONDS = 10.0


def line_client() -> httpx.AsyncClient:
    """The HTTP client for every LINE call. One place, so tests can replace it."""
    return httpx.AsyncClient(timeout=LINE_TIMEOUT_SECONDS)


class LineSignInFailed(Exception):
    """The code could not be exchanged, or the profile could not be read."""


def _json(response: httpx.Response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def exchange_code(code: str) -> str:
    """Exchanges the authorization code for a LINE user access token, using the
    deletion callback as the redirect_uri (it must match the authorize request).
    Raises LineSignInFailed on any failure."""
    try:
        async with line_client() as client:
            response = await client.post(
                LINE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.LINE_DELETE_REDIRECT_URI,
                    "client_id": settings.LINE_CHANNEL_ID,
                    "client_secret": settings.LINE_CHANNEL_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as e:
        print("Account deletion: LINE token exchange failed:", type(e).__name__)
        raise LineSignInFailed()
    token = _json(response).get("access_token") if response.status_code == 200 else None
    if not isinstance(token, str) or not token:
        print("Account deletion: LINE token exchange refused, HTTP", response.status_code)
        raise LineSignInFailed()
    return token


async def fetch_line_user_id(user_access_token: str) -> str:
    """The LINE userId the token belongs to. Raises LineSignInFailed on any failure."""
    try:
        async with line_client() as client:
            response = await client.get(
                LINE_PROFILE_URL, headers={"Authorization": f"Bearer {user_access_token}"})
    except httpx.HTTPError as e:
        print("Account deletion: LINE profile request failed:", type(e).__name__)
        raise LineSignInFailed()
    line_user_id = _json(response).get("userId") if response.status_code == 200 else None
    if not isinstance(line_user_id, str) or not line_user_id:
        print("Account deletion: LINE profile refused, HTTP", response.status_code)
        raise LineSignInFailed()
    return line_user_id


def delete_unused_photos(paths: Any) -> int:
    """Deletes each photo of a deleted submission that nothing uses any more (the
    rule, and never raising, are delete_upload_if_unused's). Returns how many were
    deleted."""
    if not isinstance(paths, list):
        return 0
    deleted = 0
    for path in paths:
        try:
            if delete_upload_if_unused(path, "POST /auth/me/delete"):
                deleted += 1
        except Exception as e:          # it should not raise; the request must not fail either way
            print("Account deletion: photo cleanup failed:", type(e).__name__)
    return deleted


async def deauthorize(user_access_token: str) -> bool:
    """Best-effort: tells LINE the app is no longer authorized for this user.
    Issues a stateless channel access token, then POSTs the user's access token to
    the Deauthorize endpoint. True only on LINE's 204. Never raises; any other
    outcome is printed, without a token, and answers False."""
    try:
        async with line_client() as client:
            channel = await client.post(
                LINE_CHANNEL_TOKEN_URL,
                data={"grant_type": "client_credentials",
                      "client_id": settings.LINE_CHANNEL_ID,
                      "client_secret": settings.LINE_CHANNEL_SECRET},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            channel_token = _json(channel).get("access_token") if channel.status_code == 200 else None
            if not isinstance(channel_token, str) or not channel_token:
                print("Account deletion: LINE channel token refused, HTTP", channel.status_code)
                return False
            response = await client.post(
                LINE_DEAUTHORIZE_URL,
                json={"userAccessToken": user_access_token},
                headers={"Authorization": f"Bearer {channel_token}"},
            )
        if response.status_code == 204:
            return True
        print("Account deletion: LINE Deauthorize refused, HTTP", response.status_code)
        return False
    except Exception as e:
        print("Account deletion: LINE Deauthorize failed:", type(e).__name__)
        return False
