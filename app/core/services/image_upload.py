"""Image uploads for product submissions and admin product edits.

POST /submissions/images and POST /products/{id}/image both come through here:
a multipart body with one field, `file`. The file is judged by its first bytes
(its "magic bytes"), never by its declared content type or file name, because
both of those are whatever the client says. jpeg, png and webp are accepted, up
to 5 MB: bigger answers 413, anything else 415.

Why the multipart body is parsed here rather than by FastAPI's UploadFile:
UploadFile needs the python-multipart package, which is not in requirements.txt
or the app's environment. A body with one file field needs only the small
parser below (RFC 7578), and reading the body as a stream lets an oversized
upload be refused as soon as it passes the limit, not after it has all arrived.

Files are stored with the service-role client in the product-images bucket
(migrations 0004/0005) under <folder>/<uuid>.<ext>, so an upload never
overwrites another.
"""

import re
import uuid
from typing import Optional

from fastapi import HTTPException, Request

from app.db.connection import supabase

STORAGE_BUCKET = "product-images"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Room for the multipart boundary lines and part headers around the file.
MULTIPART_OVERHEAD_BYTES = 16 * 1024

# Shown in the OpenAPI page: the route reads the body itself, so FastAPI cannot
# describe it on its own.
UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object", "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary",
                                    "description": "jpeg, png or webp, 5 MB at most"}},
        }}},
    }
}


def sniff_image_type(data: bytes) -> Optional[tuple]:
    """(extension, content type) for a jpeg, png or webp file, judged by its
    first bytes; None for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def parse_multipart_file(body: bytes, content_type: str, field: str = "file") -> Optional[bytes]:
    """The bytes of the form field named `field` in a multipart/form-data body,
    or None if there is no such field. ValueError if the body is not multipart."""
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type or "", re.I)
    if not content_type.lower().startswith("multipart/form-data") or not match:
        raise ValueError("not a multipart/form-data body")
    boundary = (match.group(1) or match.group(2)).encode("latin-1")
    # Each delimiter is CRLF + "--" + boundary; the first may start the body.
    parts = (b"\r\n" + body).split(b"\r\n--" + boundary)
    for part in parts[1:]:
        if part.startswith(b"--"):          # the closing delimiter
            break
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        disposition = ""
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-disposition":
                disposition = value
        found = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition)
        if found and found.group(1) == field:
            return content
    return None


async def read_image_upload(request: Request) -> tuple:
    """The uploaded file's bytes, extension and content type, or the HTTP error:
    413 over 5 MB, 415 not jpeg/png/webp, 422 no multipart `file` field."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES:
        raise HTTPException(status_code=413, detail="The image is larger than 5 MB.")

    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES:
            raise HTTPException(status_code=413, detail="The image is larger than 5 MB.")
        chunks.append(chunk)

    try:
        data = parse_multipart_file(b"".join(chunks), request.headers.get("content-type", ""))
    except ValueError:
        raise HTTPException(status_code=422, detail="Send the image as multipart/form-data in a field named 'file'.")
    if not data:
        raise HTTPException(status_code=422, detail="Send the image as multipart/form-data in a field named 'file'.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The image is larger than 5 MB.")

    kind = sniff_image_type(data)
    if kind is None:
        raise HTTPException(status_code=415, detail="The image must be a JPEG, PNG or WebP file.")
    ext, content_type = kind
    return data, ext, content_type


def public_url(path: str) -> str:
    """The public URL of an object in the product-images bucket."""
    return supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)


def store_image(data: bytes, ext: str, content_type: str, folder: str) -> dict:
    """Upload to product-images/<folder>/<uuid>.<ext>; returns image_path and public_url."""
    path = f"{folder}/{uuid.uuid4()}.{ext}"
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path, data, {"content-type": content_type, "upsert": "false"})
    return {"image_path": path, "public_url": public_url(path)}
