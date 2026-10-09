from supabase import create_client, Client
from app.config.setting import settings

supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_ROLE_KEY
)

# The client builds its PostgREST and storage sub-clients on first use. Request handlers
# now run in worker threads, so build both here, once, before any thread can race to.
# Everything else a request touches is per call (the query builders), and the shared
# httpx.Client is documented as thread-safe ("It can be shared between threads").
supabase.postgrest
supabase.storage
