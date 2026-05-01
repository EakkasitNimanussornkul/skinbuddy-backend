# Loads environment variables (DB URL, Secret Keys)
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: SecretStr
    
    SUPABASE_URL: str
    SUPABASE_KEY: str  

    FRONTEND_URL: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

# Create the globally available settings object
settings = Settings()