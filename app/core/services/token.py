from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client
from app.config.setting import settings

# Initialize Supabase client
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

# This tells FastAPI to look for the "Authorization: Bearer <token>" header
security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    
    try:
        # Ask Supabase if the token is valid, and if so, get the user info
        user_response = supabase.auth.get_user(token)
        
        if not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
            
        # Return the user object so your endpoints can use it!
        return user_response.user
        
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")