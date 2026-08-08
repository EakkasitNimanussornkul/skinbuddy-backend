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

def get_current_admin_user_id(user_id: str = Depends(get_current_user_id)) -> str:
    """Admin-only gate. Looks up role fresh on every request - never trusts a
    claim from the token itself - so revoking admin in the DB takes effect
    immediately, and there's no role data in the JWT to tamper with."""
    res = supabase.table("users").select("role").eq("id", user_id).single().execute()
    if not res.data or res.data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id

def get_optional_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Optional[str]:
    if not credentials:
        return None
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )
        return payload.get("sub")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None  # Fallback to anonymous guest state gracefully