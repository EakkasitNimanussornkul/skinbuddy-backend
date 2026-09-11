from fastapi import APIRouter, HTTPException, Depends
import httpx
from pydantic import BaseModel, field_validator
from app.schemas import LineAuthRequest, validate_baumann_skin_type
from app.config.setting import settings
from app.db.repository.user_repo import user_repo
from app.core.services.token import create_supabase_compatible_token
from app.db.connection import supabase
from app.core.services.token import get_current_user_id

router = APIRouter()

class UserUpdateRequest(BaseModel):
    skin_type: str

    _validate_skin_type = field_validator("skin_type")(validate_baumann_skin_type)

@router.post("/line")
async def line_login(payload: LineAuthRequest):
    # Step 1: Exchange the 'code' for a LINE Access Token
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://api.line.me/oauth2/v2.1/token",
            data={
                "grant_type": "authorization_code",
                "code": payload.code,
                "redirect_uri": settings.LINE_REDIRECT_URI,
                "client_id": settings.LINE_CHANNEL_ID,
                "client_secret": settings.LINE_CHANNEL_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get LINE token")
        
        line_access_token = token_response.json().get("access_token")

        # Step 2: Get the User's Profile from LINE
        profile_response = await client.get(
            "https://api.line.me/v2/profile",
            headers={"Authorization": f"Bearer {line_access_token}"}
        )

        if profile_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch LINE profile")

        line_data = profile_response.json()

    # Step 3: Save to Supabase (via our Repo)
    db_user = user_repo.get_or_create_line_user(
        line_id=line_data["userId"],
        name=line_data["displayName"],
        picture=line_data.get("pictureUrl", "")
    )

    if not db_user:
        raise HTTPException(status_code=500, detail="Database error during user creation")

    # Step 4: Issue the "Wristband" (Supabase JWT)
    access_token = create_supabase_compatible_token(str(db_user["id"]))

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": db_user["id"],
            "name": db_user["display_name"],
            "avatar": db_user["picture_url"],
            "skin_type": db_user.get("skin_type"),
        }
    }

@router.get("/me")
async def get_me(user_id: str = Depends(get_current_user_id)):
    user = supabase.table("users").select("*").eq("id", user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User not found")
    return user.data[0]

# --- NEW: Express Skin Type Update Route ---
@router.patch("/me")
async def update_me(payload: UserUpdateRequest, user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("users").update({
            "skin_type": payload.skin_type
        }).eq("id", user_id).execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="User not found or update failed")

        return response.data[0]
    except HTTPException:
        # Ahead of the generic clause below, which catches HTTPException too and
        # re-raised the 404 above as a 500 carrying it as text:
        # "Database error: 404: User not found or update failed". A user who is
        # not there is not a database failure. BE-DEF-02's trap; this handler
        # never got the guard the shelf, products, quiz and routine handlers have.
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")