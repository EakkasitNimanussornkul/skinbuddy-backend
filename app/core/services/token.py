import jwt
from datetime import datetime, timedelta
from typing import Optional
from app.config.setting import settings
from app.db.connection import supabase
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Crucial: auto_error=False prevents FastAPI from auto-blocking anonymous requests
security = HTTPBearer(auto_error=False)

def create_supabase_compatible_token(user_id: str):
    now = datetime.utcnow()
    payload = {
        "aud": "authenticated",
        "role": "authenticated",
        "sub": user_id,  # UUID from your database
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=7)).timestamp()),
        "app_metadata": {"provider": "line"},
        "user_metadata": {}
    }
    
    # Sign using the Secret found in your Supabase Project Settings
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256")

def get_current_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> str:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )
        return payload.get("sub")  # user_id UUID
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_optional_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Optional[str]:
    # No Authorization header at all: an anonymous guest, which public product
    # pages must keep serving.
    if not credentials:
        return None
    # A header that is present but expired or invalid is a broken session, not a
    # guest. Treating it as a guest hid an expired login: every % Match showed
    # "unavailable" and nothing prompted a re-login. Delegating gives the same
    # 401 and detail text ("Token expired" / "Invalid token") as protected routes,
    # which the frontend's 401 interceptor turns into a login prompt.
    return get_current_user_id(credentials)


def get_admin_user_id(user_id: str = Depends(get_current_user_id)) -> str:
    """Admin-only routes. 401 for no login or a bad one (get_current_user_id),
    403 unless the caller's users row says role = 'admin' (migration 0006).

    The role is read from the database on every request, never from the token,
    so revoking admin takes effect at once. .limit(1), not .single(): .single()
    raises on a missing row, which would answer 500 instead of 403.
    """
    res = supabase.table("users").select("role").eq("id", user_id).limit(1).execute()
    if not res.data or res.data[0].get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id
