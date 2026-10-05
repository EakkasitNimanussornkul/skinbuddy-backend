"""POST /submissions/images and POST /products/{id}/image.

A file is judged by its first bytes, never by its name or declared type: jpeg,
png and webp are accepted, anything else is 415, more than 5 MB is 413. What is
stored is the image re-encoded by Pillow, never the bytes sent (the hardening
tests are in test_upload_hardening.py). Storage is a fake (tests/conftest.py),
so nothing reaches the live bucket.
"""

import asyncio
import io
import os
import re

import httpx
from PIL import Image

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.services import image_upload

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FIVE_MB = 5 * 1024 * 1024



def _image(fmt):
    """A real 8x8 image in the given Pillow format: uploads are decoded now."""
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 120, 40)).save(out, fmt)
    return out.getvalue()


JPEG = _image("JPEG")
PNG = _image("PNG")
WEBP = _image("WEBP")
GIF = b"GIF89a" + b"\x04" * 64

MODULES = ("app.core.services.image_upload", "app.core.services.token", "app.api.products")


@pytest.fixture
def storage(patch_backend, as_user):
    as_user("admin-1")
    return patch_backend({"users": [{"id": "admin-1", "role": "admin"}],
                          "products": [{"id": PROD_ID}]}, *MODULES)


def upload(client, data, filename="photo.jpg", content_type="image/jpeg", path="/submissions/images"):
    return client.post(path, files={"file": (filename, data, content_type)})


@pytest.mark.parametrize("data, ext, content_type", [
    (JPEG, "jpg", "image/jpeg"), (PNG, "png", "image/png"), (WEBP, "webp", "image/webp"),
], ids=["jpeg", "png", "webp"])
def test_upload_accepts_jpeg_png_and_webp(client, storage, data, ext, content_type):
    """Returns HTTP 200 with image_path "submissions/<uuid>.<ext>" and its public URL,
    and stores a re-encoded image of the same format and size in the product-images
    bucket under that path, with the content type the bytes show (even though every
    file is sent named photo.jpg)."""
    resp = upload(client, data)
    assert resp.status_code == 200
    body = resp.json()
    assert re.fullmatch(rf"submissions/{UUID_RE}\.{ext}", body["image_path"])
    assert body["public_url"].endswith(f"/product-images/{body['image_path']}")
    [stored] = storage.storage.uploads
    assert stored["bucket"] == "product-images" and stored["path"] == body["image_path"]
    assert image_upload.sniff_image_type(stored["data"]) == (ext, content_type)
    assert Image.open(io.BytesIO(stored["data"])).size == (8, 8)
    assert stored["options"]["content-type"] == content_type


@pytest.mark.parametrize("data, filename, declared", [
    (GIF, "photo.png", "image/png"),
    (b"just some text, not an image", "photo.jpg", "image/jpeg"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "photo.webp", "image/webp"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", "photo.webp", "image/webp"),
], ids=["gif-named-png", "text-named-jpg", "svg-named-webp", "riff-wave-named-webp"])
def test_upload_judges_the_file_by_its_bytes_not_its_name_or_type(client, storage, data, filename, declared):
    """Returns HTTP 415 for a file that is not jpeg, png or webp, even when its name and
    declared content type say it is one, and stores nothing."""
    resp = upload(client, data, filename, declared)
    assert resp.status_code == 415
    assert storage.storage.uploads == []


def test_upload_over_5_mb_is_refused_with_413(client, storage):
    """Returns HTTP 413 for a valid PNG one byte over 5 MB, and stores nothing."""
    resp = upload(client, PNG + b"\x00" * (FIVE_MB + 1 - len(PNG)), "big.png", "image/png")
    assert resp.status_code == 413
    assert storage.storage.uploads == []


def test_upload_of_exactly_5_mb_is_accepted(client, storage):
    """Returns HTTP 200 for a PNG of exactly 5 MB: the limit is inclusive."""
    resp = upload(client, PNG + b"\x00" * (FIVE_MB - len(PNG)), "limit.png", "image/png")
    assert resp.status_code == 200
    assert image_upload.sniff_image_type(storage.storage.uploads[0]["data"]) == ("png", "image/png")


def test_upload_far_over_the_limit_is_refused_while_streaming(client, storage):
    """Returns HTTP 413 for a body well past 5 MB plus the multipart allowance, without
    parsing it, and stores nothing."""
    resp = upload(client, JPEG + b"\x00" * (FIVE_MB + 64 * 1024), "huge.jpg")
    assert resp.status_code == 413
    assert storage.storage.uploads == []


def test_upload_refuses_a_declared_oversize_body_before_reading_it():
    """read_image_upload raises HTTP 413 from the Content-Length header alone when it
    declares more than 5 MB plus the multipart allowance; the body is never read."""
    async def receive():
        raise AssertionError("the body must not be read")

    request = Request({"type": "http", "method": "POST", "path": "/submissions/images",
                       "headers": [(b"content-type", b"multipart/form-data; boundary=x"),
                                   (b"content-length", str(FIVE_MB + 16 * 1024 + 1).encode())]},
                      receive)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(image_upload.read_image_upload(request))
    assert caught.value.status_code == 413


def test_upload_without_a_length_header_is_cut_off_once_past_the_limit():
    """read_image_upload raises HTTP 413 for a streamed body with no Content-Length header
    as soon as it passes 5 MB plus the multipart allowance, without reading the rest."""
    sent = []

    async def receive():
        sent.append(1)
        return {"type": "http.request", "body": b"\x00" * (1024 * 1024), "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/submissions/images",
                       "headers": [(b"content-type", b"multipart/form-data; boundary=x")]}, receive)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(image_upload.read_image_upload(request))
    assert caught.value.status_code == 413
    assert len(sent) == 6


def test_upload_without_a_file_field_is_refused_with_422(client, storage):
    """Returns HTTP 422 for a multipart body whose field is not named "file", and for a
    JSON body, and stores nothing."""
    assert client.post("/submissions/images", files={"photo": ("a.png", PNG, "image/png")}).status_code == 422
    assert client.post("/submissions/images", json={"file": "aGVsbG8="}).status_code == 422
    assert storage.storage.uploads == []


def test_upload_keeps_binary_content_byte_for_byte():
    """The multipart reader returns exactly the bytes sent, for a file whose body holds
    CR, LF, CRLF, NUL and "--" sequences that a careless multipart parser would cut or
    alter, in a body built by httpx as a browser would send it."""
    data = JPEG + b"\r\n--\r\n\r\n\x00\r\r\n\n--x" + os.urandom(200_000) + b"\r\n"
    request = httpx.Request("POST", "http://test/submissions/images",
                            files={"file": ("photo.jpg", data, "image/jpeg")})
    body = request.read()
    assert image_upload.parse_multipart_file(body, request.headers["content-type"]) == data


def test_parse_multipart_file_reads_the_named_field_among_others():
    """parse_multipart_file returns the bytes of the field named "file" when other fields
    come first, and None when there is no such field."""
    body = (b"--BOUND\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nhello\r\n"
            b"--BOUND\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n"
            b"Content-Type: image/png\r\n\r\nPNGDATA\r\n--BOUND--\r\n")
    assert image_upload.parse_multipart_file(body, "multipart/form-data; boundary=BOUND") == b"PNGDATA"
    assert image_upload.parse_multipart_file(body, "multipart/form-data; boundary=BOUND", "other") is None


@pytest.mark.parametrize("disposition", [b'form-data; name="photo"; filename="file"',
                                         b'form-data; filename="file"; name="photo"'],
                         ids=["name first", "filename first"])
def test_parse_multipart_file_does_not_take_filename_for_name(disposition):
    """parse_multipart_file does not mistake filename="file" for the field name: a field
    named "photo" with filename "file" is not the "file" field, whichever comes first."""
    body = b"--B\r\nContent-Disposition: " + disposition + b"\r\n\r\nX\r\n--B--\r\n"
    assert image_upload.parse_multipart_file(body, "multipart/form-data; boundary=B") is None


def test_product_image_upload_stores_under_the_products_folder(client, storage):
    """POST /products/{id}/image returns HTTP 200 with image_path "products/<uuid>.png"
    for an admin, and leaves the product row unchanged."""
    resp = upload(client, PNG, "x.png", "image/png", path=f"/products/{PROD_ID}/image")
    assert resp.status_code == 200
    assert re.fullmatch(rf"products/{UUID_RE}\.png", resp.json()["image_path"])
    assert storage.store["products"] == [{"id": PROD_ID}]


def test_product_image_upload_checks_the_bytes_and_the_size(client, storage):
    """POST /products/{id}/image applies the same checks: 415 for a GIF named .png and
    413 over 5 MB, storing nothing."""
    path = f"/products/{PROD_ID}/image"
    assert upload(client, GIF, "x.png", "image/png", path=path).status_code == 415
    assert upload(client, PNG + b"\x00" * FIVE_MB, "x.png", "image/png", path=path).status_code == 413
    assert storage.storage.uploads == []


def test_product_image_upload_answers_404_for_an_unknown_product(client, storage):
    """Returns HTTP 404 for a product id that matches no product, or is not a UUID, and
    stores nothing."""
    other = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert upload(client, PNG, path=f"/products/{other}/image").status_code == 404
    assert upload(client, PNG, path="/products/not-a-uuid/image").status_code == 404
    assert storage.storage.uploads == []
