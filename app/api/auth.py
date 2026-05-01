import os
from fastapi import APIRouter, Depends, HTTPException
from app.schemas import RegisterRequest, LoginRequest
from sqlalchemy.orm import Session
from supabase import create_client, Client

# Import your database connection and models
from app.db.connection import get_db
from app.db.repository import user_repo
from app.config.setting import settings

router = APIRouter()

supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


@router.post("/register")
def register_user(request: RegisterRequest, db: Session = Depends(get_db)):
    try:
        # STEP A: Tell Supabase to create the secure Auth account
        auth_response = supabase.auth.sign_up({
            "email": request.email,
            "password": request.password
        })
        
        # Check if Supabase successfully created the user
        if not auth_response.user:
            raise HTTPException(status_code=400, detail="Failed to create user in Supabase")

        # STEP B: Save the user to YOUR public.users table so you can attach apps to them!
        new_user = user_repo.create_user(
            db=db,
            user_id=auth_response.user.id,
            email=request.email,
            username=request.username
        )
        
        
        return {"message": "User registered successfully!", "user_id": new_user.id}

    except Exception as e:
        # If anything fails, return a clean error
        raise HTTPException(status_code=400, detail=str(e))
    
# NEW: The Login Endpoint
@router.post("/login")
def login_user(request: LoginRequest):
    try:
        # Ask Supabase to verify the email and password
        auth_response = supabase.auth.sign_in_with_password({
            "email": request.email,
            "password": request.password
        })
        
        # If successful, extract the secure "wristband" (JWT)
        session = auth_response.session
        if not session:
            raise HTTPException(status_code=401, detail="Invalid login credentials")

        # Return the token and the user's ID
        return {
            "message": "Login successful!",
            "access_token": session.access_token,
            "token_type": "bearer",
            "user_id": auth_response.user.id
        }
        
    except Exception as e:
        # Supabase will automatically throw an error if the password is wrong
        raise HTTPException(status_code=401, detail=f"Login failed: {str(e)}")