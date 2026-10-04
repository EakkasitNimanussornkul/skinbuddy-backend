"""HTTP answers for the errors approve_submission() and admin_update_product() raise.

Both functions (migration 0013) raise with their own SQLSTATE codes; PostgREST
answers HTTP 400 for all of them and supabase-py raises APIError carrying the
code, the message and the DETAIL. The code decides the HTTP status; the table
is the one in 0013's header.
"""

import json
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError

# Invalid input. The message says what was wrong and is kept; a JSON DETAIL
# (the candidate rows of an ambiguous name, the unknown ids) is passed on.
UNPROCESSABLE_CODES = {"SBDEC", "SBNON", "SBLEG", "SBAMB", "SBFGR", "SBUNK", "SBVAL", "23514"}


class BodyHTTPException(HTTPException):
    """An HTTP error whose JSON body is `body` itself, not {"detail": ...}:
    the contract's 409 duplicate carries "candidates" beside "detail"."""

    def __init__(self, status_code: int, body: Dict[str, Any]):
        super().__init__(status_code=status_code, detail=body.get("detail"))
        self.body = body


async def body_http_exception_handler(_request: Request, exc: BodyHTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body)


def _json_detail(details):
    """The DETAIL parsed as JSON, or None when it is absent or plain text."""
    if not isinstance(details, str) or not details.strip():
        return None
    try:
        return json.loads(details)
    except ValueError:
        return None


def http_error_for_rpc(err: APIError, where: str) -> BodyHTTPException:
    code = getattr(err, "code", None)
    message = getattr(err, "message", None) or "invalid request"
    detail_json = _json_detail(getattr(err, "details", None))

    if code == "SBDUP":
        return BodyHTTPException(409, {"detail": "duplicate",
                                       "candidates": detail_json if isinstance(detail_json, list) else []})
    if code == "23505":
        # Two approvals of the same brand+name raced and this one lost on the
        # unique index; the winner's row is not known here.
        return BodyHTTPException(409, {"detail": "duplicate", "candidates": []})
    if code == "SBSTL":
        return BodyHTTPException(409, {"detail": "stale"})
    if code in UNPROCESSABLE_CODES:
        body = {"detail": message, "code": code}
        if detail_json is not None:
            body["details"] = detail_json
        return BodyHTTPException(422, body)
    if code == "SBNFD":
        return BodyHTTPException(404, {"detail": message, "code": code})
    if code == "SBNPD":
        return BodyHTTPException(409, {"detail": message, "code": code})
    if code == "SBADM":
        return BodyHTTPException(403, {"detail": message, "code": code})

    print(f"{where} database error:", code, message, getattr(err, "details", None))
    return BodyHTTPException(500, {"detail": "The database refused the change."})
