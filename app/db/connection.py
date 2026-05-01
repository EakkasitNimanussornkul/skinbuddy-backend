from supabase import create_client, Client
from app.config.setting import settings

supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_KEY
)