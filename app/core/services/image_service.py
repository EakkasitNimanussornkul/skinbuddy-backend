from typing import Optional
from urllib.parse import urlparse

import httpx

from app.db.connection import supabase

STORAGE_BUCKET = "product-images"

# Only these hosts are trusted enough to fetch images from. Reject anything
# else outright - never let ingestion hotlink or fetch an arbitrary
# externally-supplied URL.
ALLOWED_IMAGE_HOSTS = {"images.openbeautyfacts.org"}

MAX_IMAGE_BYTES = 5 * 1024 * 1024


async def fetch_and_store_product_image(client: httpx.AsyncClient, url: Optional[str], slug: str) -> Optional[str]:
    """Validates, downloads, and stores an external product image in Supabase Storage.

    Returns the storage's public URL, or None if the source isn't trusted or
    anything fails along the way. Never raises - a failed/untrusted image must
    never block product ingestion.
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_IMAGE_HOSTS:
            return None

        # No redirects: an allowlisted URL that redirects elsewhere must not
        # be silently followed (SSRF/allowlist-bypass guard).
        resp = await client.get(url, timeout=15.0, follow_redirects=False)
        if resp.status_code != 200:
            return None

        content_type = resp.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            return None
        if len(resp.content) > MAX_IMAGE_BYTES:
            return None

        path = f"{slug}.jpg"
        supabase.storage.from_(STORAGE_BUCKET).upload(
            path, resp.content, {"content-type": content_type, "upsert": "true"}
        )
        return supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)
    except Exception as e:
        print(f"Image fetch skipped for {slug}: {e}")
        return None
