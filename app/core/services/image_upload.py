"""Image uploads for product submissions and admin product edits.

POST /submissions/images and POST /products/{id}/image both come through here:
a multipart body with one field, `file`. The file is judged by its first bytes
(its "magic bytes"), never by its declared content type or file name, because
both of those are whatever the client says. jpeg, png and webp are accepted, up
to 5 MB: bigger answers 413, anything else 415.

Every accepted file is then decoded and RE-ENCODED with Pillow, and only the
fresh image is stored (reencode_image below). The bytes the user sent are never
published. That removes, in one step:
  * metadata: EXIF (GPS position, camera serial numbers), XMP, ICC profiles,
    comments and text chunks. A phone photo's GPS tag says where the user lives;
  * anything hidden after or inside the image: a "polyglot" file that is a
    valid JPEG and also HTML or script, which a browser could be talked into
    running from our bucket's origin;
  * files that only pretend to be images: magic bytes followed by anything else.
Pillow refuses truncated or corrupt data (415), and images over MAX_PIXELS
(413) before decoding them, so a small file declaring huge dimensions (a
"decompression bomb") cannot exhaust memory.

Why the multipart body is parsed here rather than by FastAPI's UploadFile:
UploadFile needs the python-multipart package, which is not in requirements.txt
or the app's environment. A body with one file field needs only the small
parser below (RFC 7578), and reading the body as a stream lets an oversized
upload be refused as soon as it passes the limit, not after it has all arrived.
The parser enforces the limits python-multipart does: the boundary's length and
characters (RFC 2046), the number of parts, and the size and number of each
part's headers. It uses no regular expressions, so hostile input cannot make it
backtrack, and every malformed body answers 422, never 500.

Files are stored with the service-role client in the product-images bucket
(migrations 0004/0005) under <folder>/<uuid>.<ext>, so an upload never
overwrites another.

Uploads are deleted again once nothing uses them (owner's decision,
2026-10-06): see "Deleting uploads nothing uses" at the end of this module.
"""

import io
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException, Request
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from app.core.services.ingredient_lookup import fetch_all_rows
from app.core.services.submission_service import merged_payload
from app.db.connection import supabase
from app.schemas import PRODUCT_IMAGE_PATH, UPLOAD_PATH_IN_TEXT

STORAGE_BUCKET = "product-images"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Room for the multipart boundary lines and part headers around the file.
MULTIPART_OVERHEAD_BYTES = 16 * 1024

# --- Multipart limits ---------------------------------------------------------
MAX_BOUNDARY_LENGTH = 70                 # RFC 2046 section 5.1.1
# RFC 2046's bchars: digits, letters and '()+_,-./:=? plus space (not last).
BOUNDARY_CHARS = frozenset("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'()+_,-./:=? ")
MAX_PARTS = 10                           # the form sends one; a few spare for a browser's extras
MAX_PART_HEADER_BYTES = 8 * 1024         # all of one part's header lines together
MAX_PART_HEADERS = 8                     # header lines in one part
MAX_TRANSPORT_PADDING = 64               # spaces/tabs allowed after a delimiter

# --- Image limits -------------------------------------------------------------
# 40 megapixels. A 5 MB JPEG holds a 12-24 MP phone photo comfortably, and the
# 48-50 MP full-resolution modes save files well over 5 MB, so a genuine upload
# that passes the size limit is far below this. Decoded as RGBA, 40 MP is about
# 160 MB of memory, which bounds what one upload can make the server allocate.
# Pillow's own guard (Image.MAX_IMAGE_PIXELS, about 89 MP) is left as it is,
# as a second line, and is not changed process-wide.
MAX_PIXELS = 40_000_000
# Product photos are shown at most a few hundred pixels wide; 2048 px on the
# long side keeps them sharp on any screen and bounds what each one stores.
MAX_SIDE = 2048
JPEG_QUALITY = 85
WEBP_QUALITY = 85

# Pillow's format names for each sniffed type. A multi-picture JPEG (MPO, some
# cameras and phones) opens in Pillow as "MPO"; it is a JPEG whose first frame
# is the photo, so it is accepted as one.
PIL_FORMATS: Dict[str, Tuple[str, ...]] = {"jpg": ("JPEG", "MPO"), "png": ("PNG",), "webp": ("WEBP",)}
SAVE_FORMATS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}

# --- The answers --------------------------------------------------------------
MSG_TOO_BIG = "The image is larger than 5 MB."
MSG_NOT_AN_IMAGE = "The image must be a JPEG, PNG or WebP file."
MSG_UNREADABLE = "The image could not be read. It may be damaged, or not really a JPEG, PNG or WebP file."
MSG_TOO_MANY_PIXELS = "The image has too many pixels: at most 40 megapixels."
# Shown to the person uploading, as written (the frontend displays `detail`).
MSG_NO_FILE = "No photo was received. Please choose a photo and try again."
MSG_BAD_BOUNDARY = "The multipart boundary must be 1 to 70 characters, of those RFC 2046 allows."
MSG_TOO_MANY_PARTS = f"The upload has more than {MAX_PARTS} parts."
MSG_HEADERS_TOO_LONG = "A part's headers are too long."
MSG_TOO_MANY_HEADERS = "A part has too many headers."
MSG_TWO_FILES = "Send exactly one file, in a field named 'file'."
MSG_MALFORMED = "The upload is malformed or incomplete."

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


class MultipartError(ValueError):
    """A body that is not acceptable multipart/form-data; str(error) is the 422 detail."""


class ImageRefused(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


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


# --- Multipart ------------------------------------------------------------------

def _split_params(value: str) -> Tuple[str, Dict[str, str]]:
    """'form-data; name="file"; filename="a;b.jpg"' -> ("form-data", {"name": "file",
    "filename": "a;b.jpg"}). One pass over the characters, honouring quoted
    strings and backslash escapes in them; no regex. A parameter named twice,
    or an unclosed quote, is malformed: two readers could disagree on it."""
    items, buf = [], []
    quoted = escaped = False
    for ch in value:
        if quoted:
            if escaped:
                buf.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
                buf.append(ch)
            else:
                buf.append(ch)
        elif ch == '"':
            quoted = True
            buf.append(ch)
        elif ch == ";":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if quoted:
        raise MultipartError(MSG_MALFORMED)
    items.append("".join(buf))

    params: Dict[str, str] = {}
    for item in items[1:]:
        key, eq, val = item.partition("=")
        key, val = key.strip().lower(), val.strip()
        if not key and not eq:
            continue                                  # an empty item: "a; ; b" or a trailing ";"
        if not key or not eq or key in params:
            raise MultipartError(MSG_MALFORMED)
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        params[key] = val
    return items[0].strip().lower(), params


def _boundary(content_type: str) -> bytes:
    kind, params = _split_params(content_type or "")
    if kind != "multipart/form-data" or "boundary" not in params:
        raise MultipartError(MSG_NO_FILE)
    boundary = params["boundary"]
    if (not 1 <= len(boundary) <= MAX_BOUNDARY_LENGTH or boundary.endswith(" ")
            or not set(boundary) <= BOUNDARY_CHARS):
        raise MultipartError(MSG_BAD_BOUNDARY)
    return boundary.encode("ascii")


def _part_headers(head: bytes) -> Dict[str, str]:
    lines = head.split(b"\r\n") if head else []
    if len(lines) > MAX_PART_HEADERS:
        raise MultipartError(MSG_TOO_MANY_HEADERS)
    headers: Dict[str, str] = {}
    for line in lines:
        name, colon, value = line.decode("latin-1").partition(":")
        name = name.lower()
        if not colon or not name or name != name.strip() or " " in name or "\t" in name:
            raise MultipartError(MSG_MALFORMED)
        if name in headers:
            raise MultipartError(MSG_MALFORMED)
        headers[name] = value.strip()
    return headers


def parse_multipart_file(body: bytes, content_type: str, field: str = "file") -> Optional[bytes]:
    """The bytes of the form field named `field` in a multipart/form-data body,
    or None if there is no such field. MultipartError (a ValueError) if the body
    is not multipart, breaks a limit, holds the field twice, or is malformed
    (for example, it has no closing delimiter)."""
    boundary = _boundary(content_type)
    delimiter = b"--" + boundary
    separator = b"\r\n" + delimiter

    # The first delimiter starts the body or ends a preamble, which is ignored.
    if body.startswith(delimiter):
        pos = len(delimiter)
    else:
        first = body.find(separator)
        if first < 0:
            raise MultipartError(MSG_MALFORMED)
        pos = first + len(separator)

    found: Optional[bytes] = None
    parts = 0
    while True:
        if body.startswith(b"--", pos):              # the closing delimiter: done
            return found
        # Optional transport padding (spaces, tabs), then the line break.
        line_end = body.find(b"\r\n", pos, pos + MAX_TRANSPORT_PADDING + 2)
        if line_end < 0 or body[pos:line_end].strip(b" \t"):
            raise MultipartError(MSG_MALFORMED)
        pos = line_end + 2

        end = body.find(separator, pos)
        if end < 0:                                   # no further delimiter: cut short
            raise MultipartError(MSG_MALFORMED)
        parts += 1
        if parts > MAX_PARTS:
            raise MultipartError(MSG_TOO_MANY_PARTS)

        if body.startswith(b"\r\n", pos):            # a part with no headers
            head, content_start = b"", pos + 2
        else:
            limit = min(end, pos + MAX_PART_HEADER_BYTES + 4)
            head_end = body.find(b"\r\n\r\n", pos, limit)
            if head_end < 0:
                raise MultipartError(MSG_HEADERS_TOO_LONG if end - pos > MAX_PART_HEADER_BYTES
                                     else MSG_MALFORMED)
            head, content_start = body[pos:head_end], head_end + 4

        headers = _part_headers(head)
        disposition, params = _split_params(headers.get("content-disposition", ""))
        if disposition != "form-data" or "name" not in params:
            raise MultipartError(MSG_MALFORMED)
        if params["name"] == field:
            if found is not None:
                raise MultipartError(MSG_TWO_FILES)
            found = body[content_start:end]
        pos = end + len(separator)


# --- Re-encoding ------------------------------------------------------------------

def _refuse_too_many_pixels(img: Image.Image) -> None:
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise ImageRefused(413, MSG_TOO_MANY_PIXELS)


def _converted(img: Image.Image, ext: str) -> Image.Image:
    """img in a mode its format can save: JPEG takes RGB or greyscale (CMYK and
    anything else becomes RGB; a transparent image is flattened onto white);
    PNG and WebP keep transparency (as RGBA) when there is any."""
    has_alpha = img.has_transparency_data
    if ext == "jpg":
        if img.mode in ("RGB", "L"):
            return img
        if has_alpha:
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            return flat
        return img.convert("RGB")
    keep = ("RGB", "RGBA", "L", "LA") if ext == "png" else ("RGB", "RGBA")
    if img.mode in keep:
        return img
    return img.convert("RGBA" if has_alpha else "RGB")


def reencode_image(data: bytes, ext: str) -> bytes:
    """A fresh image made from data, in the same format: the first frame only,
    EXIF orientation applied, at most MAX_SIDE px on the long side, and no
    metadata at all. ImageRefused(415) if Pillow cannot decode it as the
    sniffed format, or it is truncated or corrupt; ImageRefused(413) if it has
    more than MAX_PIXELS pixels.

    Animated PNG/WebP: the first frame is kept and the rest dropped, rather
    than the file refused. A product photo is a still, and an animation's
    first frame is a fair picture of it.

    ICC colour profiles are dropped, not converted: parsing a user's profile
    with a colour engine is more attack surface, and a wide-gamut photo shown
    as sRGB loses only a little saturation.
    """
    formats = PIL_FORMATS[ext]
    try:
        with warnings.catch_warnings():
            # Pillow warns (rather than raises) between MAX_IMAGE_PIXELS and twice it.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Pass 1: verify() checks the file's structure (chunk CRCs and the
            # like) without decoding it. It leaves the image unusable, hence pass 2.
            with Image.open(io.BytesIO(data), formats=list(formats)) as probe:
                if probe.format not in formats:
                    raise ImageRefused(415, MSG_UNREADABLE)
                _refuse_too_many_pixels(probe)
                probe.verify()
            # Pass 2: decode it in full. A truncated file fails here.
            with Image.open(io.BytesIO(data), formats=list(formats)) as img:
                if img.format not in formats:
                    raise ImageRefused(415, MSG_UNREADABLE)
                _refuse_too_many_pixels(img)
                img.seek(0)                            # animated: the first frame only
                img.load()
                try:
                    oriented = ImageOps.exif_transpose(img)
                except Exception:
                    oriented = img                     # unreadable EXIF: keep it as stored
                picture = _converted(oriented, ext)
                if max(picture.size) > MAX_SIDE:
                    picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                # A brand-new image from the pixels alone: no info dict, so
                # nothing (EXIF, XMP, ICC, comments, text chunks) can be carried over.
                clean = Image.frombytes(picture.mode, picture.size, picture.tobytes())
    except ImageRefused:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageRefused(413, MSG_TOO_MANY_PIXELS)
    except Exception:
        raise ImageRefused(415, MSG_UNREADABLE)

    out = io.BytesIO()
    if ext == "jpg":
        clean.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    elif ext == "png":
        clean.save(out, "PNG", optimize=True)
    else:
        clean.save(out, "WEBP", quality=WEBP_QUALITY)
    return out.getvalue()


async def read_image_upload(request: Request) -> tuple:
    """The uploaded image, re-encoded, with its extension and content type, or
    the HTTP error: 413 over 5 MB or over 40 megapixels; 415 not a readable
    jpeg/png/webp; 422 no single multipart `file` field, or a malformed body."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES:
        raise HTTPException(status_code=413, detail=MSG_TOO_BIG)

    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES:
            raise HTTPException(status_code=413, detail=MSG_TOO_BIG)
        chunks.append(chunk)

    try:
        data = parse_multipart_file(b"".join(chunks), request.headers.get("content-type", ""))
    except MultipartError as err:
        raise HTTPException(status_code=422, detail=str(err))
    if not data:
        raise HTTPException(status_code=422, detail=MSG_NO_FILE)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=MSG_TOO_BIG)

    kind = sniff_image_type(data)
    if kind is None:
        raise HTTPException(status_code=415, detail=MSG_NOT_AN_IMAGE)
    ext, content_type = kind
    try:
        # Decoding is CPU work: off the event loop, so one upload cannot stall the others.
        clean = await run_in_threadpool(reencode_image, data, ext)
    except ImageRefused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail)
    return clean, ext, content_type


def public_url(path: str) -> str:
    """The public URL of an upload in the product-images bucket. Only a path an
    upload route makes (<submissions|products>/<uuid>.<jpg|png|webp>) is
    accepted, so the URL cannot leave the bucket or point at another object."""
    if not isinstance(path, str) or not PRODUCT_IMAGE_PATH.fullmatch(path):
        raise ValueError("not an image_path made by an upload route")
    return supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)


def store_image(data: bytes, ext: str, content_type: str, folder: str) -> dict:
    """Upload to product-images/<folder>/<uuid>.<ext>; returns image_path and public_url."""
    path = f"{folder}/{uuid.uuid4()}.{ext}"
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path, data, {"content-type": content_type, "upsert": "false"})
    return {"image_path": path, "public_url": public_url(path)}


# --- Deleting uploads nothing uses ------------------------------------------------
#
# The bucket is public, so a stored photo stays readable by anyone holding its
# URL until it is deleted. An upload is deleted once nothing uses it:
#   * POST /submissions/admin/{id}/reject deletes the rejected submission's photo;
#   * PATCH /submissions/admin/{id} and PATCH /products/{id} delete the photo
#     they replaced or cleared, after the change is saved;
#   * POST /submissions/admin/cleanup-images deletes the rest: uploads never
#     used by any submission, photos of submissions rejected before this
#     existed, and any deletion above that failed.
#
# The one rule, checked immediately before every deletion: an upload is never
# deleted while something still uses it:
#   * a product, whose image_url points at it;
#   * a pending submission, whose effective image_path (payload merged with
#     edited_payload) points at it;
#   * an approved submission, but only while the product it created
#     (product_submissions.product_id) still shows that photo. Once an admin
#     replaces or clears the product's photo, the submission no longer keeps it
#     (owner's decision, 2026-10-06), so PATCH /products/{id} deletes it and
#     the cleanup route finds those replaced before this rule existed.
# A rejected submission never keeps a photo.
#
# Only our uploads can ever be deleted: a key of the form
# <submissions|products>/<uuid>.<jpg|png|webp>, exact case. The seed and
# catalogue images (Products/<name>.png, products/<name>.png, <slug>.jpg) and
# external URLs (Open Beauty Facts) never match that shape.
#
# A storage failure while deleting never fails the request that caused it: it
# is printed (the app has no logging framework) and the request answers as it
# would have. What it leaves behind, the cleanup route finds later.

UPLOAD_FOLDERS = ("submissions", "products")
LIST_PAGE_SIZE = 100            # objects per list_v2 page
MAX_LIST_PAGES = 10_000         # a guard against a cursor that never ends
REMOVE_BATCH_SIZE = 100         # keys per remove() call
MAX_REPORTED_PATHS = 100        # paths named in the cleanup report


def upload_path_of_url(url: Any) -> Optional[str]:
    """The upload a product's image_url shows, when that URL is exactly what
    public_url() builds for one of our uploads in our bucket; None for anything
    else: a seed or catalogue image, an external URL, a URL into another
    project or bucket, or one with a query string."""
    if not isinstance(url, str):
        return None
    _, found, path = url.partition(f"/object/public/{STORAGE_BUCKET}/")
    if not found or not PRODUCT_IMAGE_PATH.fullmatch(path):
        return None
    return path if public_url(path) == url else None


def referenced_upload_paths() -> Set[str]:
    """Every upload path still in use: any found in a product's image_url, and
    the effective image_path of every pending submission. An approved
    submission's photo is in use only while the product it created still shows
    it; a rejected submission's never is.

    An approved submission with a product_id adds nothing here, and that is the
    rule, not a gap: its photo is protected exactly when its product's
    image_url still shows it, and every product's image_url is counted below.
    When that product has replaced or cleared the photo, or no longer exists
    (it cannot be deleted through the API), nothing from this submission keeps
    it. Two approved submissions naming one photo keep it while either one's
    product shows it, for the same reason.

    An approved submission with NO product_id keeps its photo. 0013's
    approve_submission sets product_id in the same update that sets 'approved',
    so this should not happen; but a row without it gives no product to check,
    and keeping a file by mistake costs only storage, while deleting one by
    mistake breaks a photo for good.

    Submissions are read BEFORE products. Approving a submission creates its
    product and marks it approved in one transaction, so if that commits
    between the two reads, the submission was seen as pending (keeps its
    photo) and the product is seen too. Read the other way round, the product
    would be missed and the now-approved submission would not keep the photo.

    Read generously on purpose, since a false "in use" only keeps a file: any
    upload-shaped path anywhere in an image_url or image_path counts, and so
    does every status other than approved and rejected (pending is the only
    other today)."""
    used: Set[str] = set()
    submissions = fetch_all_rows(lambda: supabase.table("product_submissions")
                                 .select("id, status, product_id, payload, edited_payload")
                                 .neq("status", "rejected").order("id"))
    for row in submissions:
        if row.get("status") == "approved" and row.get("product_id"):
            continue            # kept only through its product's image_url, counted below
        path = merged_payload(row).get("image_path")
        if isinstance(path, str):
            used.add(path)
            used.update(m.group(0) for m in UPLOAD_PATH_IN_TEXT.finditer(path))
    products = fetch_all_rows(lambda: supabase.table("products").select("id, image_url").order("id"))
    for row in products:
        if isinstance(row.get("image_url"), str):
            used.update(m.group(0) for m in UPLOAD_PATH_IN_TEXT.finditer(row["image_url"]))
    return used


def delete_upload_if_unused(path: Any, context: str) -> bool:
    """Deletes one upload unless something still uses it; True if it was
    deleted. Never raises: a failure, in the reference check or in storage, is
    printed under context (the route's name) and nothing is deleted, so the
    caller's own answer stands."""
    if not isinstance(path, str) or not PRODUCT_IMAGE_PATH.fullmatch(path):
        return False
    try:
        if path in referenced_upload_paths():
            return False
        removed = supabase.storage.from_(STORAGE_BUCKET).remove([path])
        return bool(removed)
    except Exception as e:
        print(f"{context}: could not delete the unused image {path}:", e)
        return False


def _last_written(obj: Any) -> Optional[datetime]:
    """The later of an object's created_at and updated_at, timezone-aware, or
    None if neither is known (such an object is never treated as old)."""
    times = [t for t in (getattr(obj, "created_at", None), getattr(obj, "updated_at", None))
             if isinstance(t, datetime)]
    if not times:
        return None
    return max(t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in times)


def list_uploads() -> List[Tuple[str, Optional[datetime]]]:
    """(key, last written) of every object under submissions/ and products/,
    a page at a time.

    list_v2, not list: list() matches its folder case-insensitively and answers
    bare file names. Live, list("products") returns the seed images stored
    under "Products/", with no way to tell which folder each came from. list_v2
    matches the prefix exactly and returns each object's full key, which is
    what remove() needs."""
    bucket = supabase.storage.from_(STORAGE_BUCKET)
    found: List[Tuple[str, Optional[datetime]]] = []
    for folder in UPLOAD_FOLDERS:
        cursor, seen = None, set()
        for _ in range(MAX_LIST_PAGES):
            options: Dict[str, Any] = {"prefix": f"{folder}/", "limit": LIST_PAGE_SIZE}
            if cursor:
                options["cursor"] = cursor
            page = bucket.list_v2(options)
            found.extend((obj.name, _last_written(obj)) for obj in page.objects)
            cursor = page.nextCursor
            if not page.hasNext or not cursor or cursor in seen:
                break
            seen.add(cursor)
    return found


def cleanup_unused_uploads(older_than_hours: int, dry_run: bool,
                           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Finds the uploads nothing uses that were last written at least
    older_than_hours ago and, unless dry_run, deletes them in batches.

    checked: objects listed under submissions/ and products/ (seed images
    there included; they are never candidates). unreferenced: the uploads
    found, all of which a dry run would delete. deleted: the ones storage
    confirmed deleted. failed: the ones it did not, or a batch that raised. An
    upload that came into use between the listing and its batch is skipped and
    counted in neither. paths: the unreferenced uploads, sorted, at most
    MAX_REPORTED_PATHS.

    The age limit spares a photo whose submission form is still being filled
    in: it is uploaded first and referenced only when the form is sent."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=older_than_hours)
    objects = list_uploads()
    used = referenced_upload_paths()
    candidates = sorted({key for key, written in objects
                         if PRODUCT_IMAGE_PATH.fullmatch(key) and written is not None
                         and written <= cutoff and key not in used})
    deleted = failed = 0
    if not dry_run:
        bucket = supabase.storage.from_(STORAGE_BUCKET)
        for start in range(0, len(candidates), REMOVE_BATCH_SIZE):
            batch = candidates[start:start + REMOVE_BATCH_SIZE]
            try:
                still_used = referenced_upload_paths()      # re-checked right before deleting
                batch = [key for key in batch if key not in still_used]
                if not batch:
                    continue
                removed = bucket.remove(batch)
            except Exception as e:
                print("POST /submissions/admin/cleanup-images: could not delete a batch:", e)
                failed += len(batch)
                continue
            done = min(len(removed), len(batch)) if isinstance(removed, list) else 0
            deleted += done
            failed += len(batch) - done
    return {"checked": len(objects), "unreferenced": len(candidates), "deleted": deleted,
            "failed": failed, "dry_run": dry_run, "paths": candidates[:MAX_REPORTED_PATHS]}
