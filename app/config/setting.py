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

    # --- Frontend ---
    FRONTEND_URL: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()