"""POST /auth/me/delete: delete the caller's account.

Mounted under /auth. The user signs in with LINE again just before deleting, and
the body carries that fresh authorization code; see
app/core/services/account_deletion_service.py for the order of steps and for what
can and cannot fail the request. Every error body is {"detail": str, "code": str}
except the 401 from the login dependency.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from postgrest.exceptions import APIError

from app.config.setting import settings
from app.core.services import account_deletion_service as service
from app.core.services.coded_errors import CodedErrorRoute, coded
from app.core.services.token import get_current_user_id
from app.db.connection import supabase

router = APIRouter(route_class=CodedErrorRoute)

NOT_DELETED = "The account could not be deleted. Nothing was removed."
ADMIN_MESSAGE = ("Admin accounts can't be deleted here. Ask the owner to change this account "
                 "to a normal user first.")


class DeleteAccountRequest(BaseModel):
    code: str = Field(min_length=1)


def _load_user(user_id: str) -> dict:
    try:
        res = supabase.table("users").select("id, line_id, role").eq("id", user_id).limit(1).execute()
    except Exception as e:
        print("Account deletion: could not read the user:", type(e).__name__)
        raise coded(500, NOT_DELETED, "internal_error")
    if not res.data:
        raise coded(404, "User not found.", "user_not_found")
    return res.data[0]


def _delete_in_database(user_id: str) -> dict:
    try:
        res = supabase.rpc("delete_user_account", {"p_user_id": user_id}).execute()
    except APIError as e:
        code = getattr(e, "code", None)
        if code == "SBNFD":
            raise coded(404, "User not found.", "user_not_found")
        if code == "SBADM":
            raise coded(409, ADMIN_MESSAGE, "admin_account")
        print("Account deletion: database error:", code, getattr(e, "message", None))
        raise coded(500, NOT_DELETED, "internal_error")
    except Exception as e:
        print("Account deletion: database call failed:", type(e).__name__)
        raise coded(500, NOT_DELETED, "internal_error")
    result = res.data[0] if isinstance(res.data, list) and res.data else res.data
    return result if isinstance(result, dict) else {}


@router.post("/me/delete")
async def delete_my_account(req: DeleteAccountRequest, user_id: str = Depends(get_current_user_id)):
    if not settings.LINE_DELETE_REDIRECT_URI:
        raise coded(503, "Account deletion is not configured yet.", "deletion_not_configured")

    # 1. Before LINE is touched: the code can be used once.
    user = _load_user(user_id)
    if user.get("role") == "admin":
        raise coded(409, ADMIN_MESSAGE, "admin_account")

    # 2-3. Prove the person at the screen holds this LINE account right now.
    try:
        line_token = await service.exchange_code(req.code)
        line_user_id = await service.fetch_line_user_id(line_token)
    except service.LineSignInFailed:
        raise coded(400, "LINE sign-in could not be confirmed. Please try again.", "line_signin_failed")
    if not user.get("line_id") or line_user_id != user.get("line_id"):
        raise coded(403, "That LINE account is not the one you are signed in with.", "line_account_mismatch")

    # 4. The point of no return: one database transaction.
    result = _delete_in_database(user_id)

    # 5-6. Nothing below can undo the deletion or fail the request.
    service.delete_unused_photos(result.get("image_paths"))
    line_deauthorized = await service.deauthorize(line_token)

    return {"deleted": True, "line_deauthorized": line_deauthorized}
