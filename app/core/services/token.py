import jwt
from datetime import datetime, timedelta
from app.config.setting import settings
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

def create_supabase_compatible_token(user_id: str):
    now = datetime.utcnow()
    payload = {
        "aud": "authenticated",
        "role": "authenticated",
        "sub": user_id,  # This must be the UUID from your database
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=7)).timestamp()),
        "app_metadata": {"provider": "line"},
        "user_metadata": {}
    }
    
    # Sign using the Secret found in your Supabase Project Settings
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256")

def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )
        return payload.get("sub")  # this is the user_id UUID
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")