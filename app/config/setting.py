from typing import Optional
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # --- Database & Supabase ---
    DATABASE_URL: SecretStr
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str  
    SUPABASE_SERVICE_ROLE_KEY: str 
    SUPABASE_JWT_SECRET: str 

    # --- LINE Login ---
    # We will use 'CHANNEL' to match your console
    LINE_CHANNEL_ID: str
    LINE_CHANNEL_SECRET: str
    LINE_REDIRECT_URI: str

    # --- LINE Messaging API (push notifications: UC-21, UC-26) ---
    # Optional so the app still boots without messaging configured.
    LINE_CHANNEL_ACCESS_TOKEN: Optional[str] = None
    # URL the reminder buttons open (your routine / check-in page or LIFF URL).
    LINE_LIFF_ROUTINE_URL: Optional[str] = None
    LINE_LIFF_CHECKIN_URL: Optional[str] = None
    # Shared secret to protect the scheduled reminder endpoints.
    CRON_SECRET: Optional[str] = None

    # --- Frontend ---
    FRONTEND_URL: str

    # --- Google AI Studio ---
    GEMINI_API_KEY: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()