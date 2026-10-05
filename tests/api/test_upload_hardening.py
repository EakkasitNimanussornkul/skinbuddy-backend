"""Upload hardening: POST /submissions/images and POST /products/{id}/image.

Every accepted file is re-encoded with Pillow, and only the fresh image is
stored: metadata and anything hidden in or after the image are gone, broken
files and decompression bombs are refused. The multipart reader enforces
parser limits, and uploads are rate-limited per user. Every image here is made
by Pillow inside the test; storage is the fake in tests/conftest.py.
"""

import io
import struct
import time
import zlib

import pytest
from PIL import Image, ImageCms, PngImagePlugin

from app.core.services import image_upload
from app.core.services.rate_limit import SlidingWindowLimiter

PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MODULES = ("app.core.services.image_upload", "app.core.services.token", "app.api.products")
UNREADABLE = "The image could not be read. It may be damaged, or not really a JPEG, PNG or WebP file."
TOO_MANY_PIXELS = "The image has too many pixels: at most 40 megapixels."
GPS_IFD = 0x8825
MAKE = 0x010F
ORIENTATION = 0x0112


@pytest.fixture
def storage(patch_backend, as_user):
    as_user("user-1")
    return patch_backend({"users": [{"id": "user-1", "role": "admin"}], "products": [{"id": PROD_ID}]}, *MODULES)


def upload(client, data, path="/submissions/images", name="photo.jpg", ctype="image/jpeg"):
    return client.post(path, files={"file": (name, data, ctype)})


def make(fmt, size=(16, 16), mode="RGB", color=(200, 120, 40), **save):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, fmt, **save)
    return out.getvalue()


def stored_image(storage, index=0):
    return Image.open(io.BytesIO(storage.storage.uploads[index]["data"]))


def png_declaring(width, height):
    """A tiny, structurally valid PNG whose header declares width x height pixels."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))


def gps_exif():
    exif = Image.Exif()
    exif[MAKE] = "SpyCam"
    gps = exif.get_ifd(GPS_IFD)
    gps[1], gps[2] = "N", (13.0, 45.0, 0.0)
    gps[3], gps[4] = "E", (100.0, 30.0, 0.0)
    return exif


def multipart(parts, boundary="BOUND"):
    """A multipart body from (headers bytes, content bytes) pairs, closed properly."""
    body = b"".join(b"--" + boundary.encode() + b"\r\n" + head + b"\r\n\r\n" + content + b"\r\n"
                    for head, content in parts)
    return body + b"--" + boundary.encode() + b"--\r\n"


FILE_HEAD = b'Content-Disposition: form-data; name="file"; filename="a.png"\r\nContent-Type: image/png'
CT = "multipart/form-data; boundary=BOUND"


# --- Re-encoding --------------------------------------------------------------------

def test_upload_strips_gps_exif_comments_and_colour_profile_from_a_jpeg(client, storage):
    """Returns HTTP 200 for a JPEG carrying GPS coordinates, a camera make, a comment and
    an ICC profile, and stores a JPEG with none of them: no EXIF at all, no comment, no
    ICC profile, and neither "SpyCam" nor the comment text anywhere in its bytes."""
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    sent = make("JPEG", exif=gps_exif(), comment=b"secret comment", icc_profile=icc)
    assert Image.open(io.BytesIO(sent)).getexif().get_ifd(GPS_IFD)      # the GPS really is there
    assert upload(client, sent).status_code == 200
    data = storage.storage.uploads[0]["data"]
    img = stored_image(storage)
    assert len(img.getexif()) == 0 and not img.getexif().get_ifd(GPS_IFD)
    assert not {"exif", "icc_profile", "comment", "xmp"} & set(img.info)
    assert b"SpyCam" not in data and b"secret comment" not in data and b"Exif\x00\x00" not in data


def test_upload_strips_text_chunks_and_xmp_from_png_and_exif_xmp_from_webp(client, storage):
    """Stores a PNG without the tEXt and iTXt (XMP) chunks it was sent with, and a WebP
    without its EXIF and XMP: the stored files contain none of the metadata text."""
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Home address 12/3")
    info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta>GPS 13.75</x:xmpmeta>")
    assert upload(client, make("PNG", pnginfo=info), name="a.png", ctype="image/png").status_code == 200
    webp = make("WEBP", exif=gps_exif(), xmp=b"<x:xmpmeta>GPS 13.75</x:xmpmeta>")
    assert upload(client, webp, name="a.webp", ctype="image/webp").status_code == 200
    for i in range(2):
        data = storage.storage.uploads[i]["data"]
        assert b"Home address" not in data and b"xmpmeta" not in data and b"SpyCam" not in data
        assert not {"exif", "xmp", "XML:com.adobe.xmp", "Author", "icc_profile"} & set(stored_image(storage, i).info)


def test_upload_applies_the_exif_orientation_before_dropping_it(client, storage):
    """Stores a 40x20 JPEG tagged "rotate 90" (EXIF orientation 6) as a 20x40 image with
    no orientation tag, so the photo shows the right way up without its EXIF."""
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    assert upload(client, make("JPEG", size=(40, 20), exif=exif)).status_code == 200
    img = stored_image(storage)
    assert img.size == (20, 40)
    assert ORIENTATION not in img.getexif()


def test_upload_drops_a_script_payload_appended_to_a_valid_jpeg(client, storage):
    """Returns HTTP 200 for a valid JPEG with an HTML/script payload appended after the
    image, and for a PNG carrying one in a text chunk, and stores re-encoded images in
    which neither "<script" nor "<html" appears."""
    payload = b"<html><script>alert(document.cookie)</script></html>"
    assert upload(client, make("JPEG") + payload).status_code == 200
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", payload.decode())
    assert upload(client, make("PNG", pnginfo=info), name="a.png", ctype="image/png").status_code == 200
    for stored in storage.storage.uploads:
        assert b"<script" not in stored["data"] and b"<html" not in stored["data"]


@pytest.mark.parametrize("data", [
    b"\xff\xd8\xff\xe0<html><script>alert(1)</script></html>",
    b"\x89PNG\r\n\x1a\n<html><script>alert(1)</script></html>",
    b"RIFF\x24\x00\x00\x00WEBP<html><script>alert(1)</script></html>",
], ids=["jpeg-magic-then-html", "png-magic-then-html", "webp-magic-then-html"])
def test_upload_of_html_behind_image_magic_bytes_is_refused(client, storage, data):
    """Returns HTTP 415 {"detail": "The image could not be read. It may be damaged, or
    not really a JPEG, PNG or WebP file."} for a file whose first bytes say jpeg, png or
    webp but which is HTML after them, and stores nothing."""
    resp = upload(client, data)
    assert resp.status_code == 415
    assert resp.json() == {"detail": UNREADABLE}
    assert storage.storage.uploads == []


def _corrupt_png_crc():
    data = bytearray(make("PNG", size=(32, 32)))
    idat = data.index(b"IDAT")
    data[idat + 6] ^= 0xFF                                  # a byte inside IDAT: its CRC no longer matches
    return bytes(data)


def _png_chunk(kind, data, crc=None):
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _png_with_tail(tail_chunks, end=True):
    """A valid 4x4 PNG's chunks, then tail_chunks, then IEND unless end is False.
    Decoding the pixels succeeds; only verify() reads past them."""
    header = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"".join(b"\x00" + b"\x80" * 12 for _ in range(4)))
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", pixels)
            + b"".join(tail_chunks) + (_png_chunk(b"IEND", b"") if end else b""))


@pytest.mark.parametrize("data", [
    make("JPEG", size=(64, 64))[:300],
    make("PNG", size=(64, 64), color=None)[:-40],
    make("WEBP", size=(64, 64))[:60],
    _corrupt_png_crc(),
    _png_with_tail([_png_chunk(b"tEXt", b"k\x00<script>", crc=1)]),
    _png_with_tail([], end=False),
], ids=["truncated-jpeg", "truncated-png", "truncated-webp", "png-bad-crc", "png-bad-crc-after-pixels",
        "png-no-end-chunk"])
def test_upload_of_a_truncated_or_corrupt_image_is_refused(client, storage, data):
    """Returns HTTP 415 with the "could not be read" detail for a JPEG, PNG or WebP cut
    short, for a PNG whose data fails its checksum, for a PNG whose pixels decode but
    whose later chunk fails its checksum, and for a PNG with no end chunk; nothing is
    stored."""
    resp = upload(client, data)
    assert resp.status_code == 415
    assert resp.json() == {"detail": UNREADABLE}
    assert storage.storage.uploads == []


@pytest.mark.parametrize("width, height", [(8000, 8000), (10000, 10000), (50000, 50000)],
                         ids=["64MP-over-our-cap", "100MP-pillow-warns", "2.5GP-pillow-refuses"])
def test_upload_of_a_decompression_bomb_is_refused(client, storage, width, height):
    """Returns HTTP 413 {"detail": "The image has too many pixels: at most 40
    megapixels."} for a PNG of a few hundred bytes declaring 64, 100 or 2,500
    megapixels, without decoding it, and stores nothing."""
    data = png_declaring(width, height)
    assert len(data) < 200
    resp = upload(client, data, name="a.png", ctype="image/png")
    assert resp.status_code == 413
    assert resp.json() == {"detail": TOO_MANY_PIXELS}
    assert storage.storage.uploads == []


def test_upload_pixel_cap_is_inclusive(client, storage, monkeypatch):
    """With the cap set to 10,000 pixels, a 100x100 image (exactly 10,000) is accepted
    and a 101x100 image is refused with HTTP 413."""
    monkeypatch.setattr(image_upload, "MAX_PIXELS", 10_000)
    assert upload(client, make("PNG", size=(100, 100)), name="a.png", ctype="image/png").status_code == 200
    assert upload(client, make("PNG", size=(101, 100)), name="a.png", ctype="image/png").status_code == 413


@pytest.mark.parametrize("fmt, size, expected", [
    ("PNG", (3000, 1000), (2048, 683)),
    ("JPEG", (1000, 3000), (683, 2048)),
    ("WEBP", (2049, 2049), (2048, 2048)),
    ("PNG", (2048, 10), (2048, 10)),
], ids=["wide-png", "tall-jpeg", "square-webp", "exactly-2048-unchanged"])
def test_upload_larger_than_2048_px_is_downscaled(client, storage, fmt, size, expected):
    """Stores an image more than 2048 px on its long side scaled down to 2048 px on that
    side, keeping its proportions (3000x1000 becomes 2048x683), and leaves one of exactly
    2048 px as it is."""
    assert upload(client, make(fmt, size=size)).status_code == 200
    assert stored_image(storage).size == expected


@pytest.mark.parametrize("fmt, mode, color, ext, stored_mode", [
    ("JPEG", "RGB", (200, 120, 40), "jpg", "RGB"),
    ("JPEG", "L", 90, "jpg", "L"),
    ("JPEG", "CMYK", (0, 128, 255, 0), "jpg", "RGB"),
    ("PNG", "RGBA", (10, 200, 30, 128), "png", "RGBA"),
    ("PNG", "LA", (90, 128), "png", "LA"),
    ("WEBP", "RGBA", (10, 200, 30, 128), "webp", "RGBA"),
    ("WEBP", "RGB", (10, 200, 30), "webp", "RGB"),
], ids=["jpeg-rgb", "jpeg-grey", "jpeg-cmyk", "png-rgba", "png-grey-alpha", "webp-rgba", "webp-rgb"])
def test_upload_round_trips_each_format_and_colour_mode(client, storage, fmt, mode, color, ext, stored_mode):
    """Stores each format as the same format (jpeg as jpeg, png as png, webp as webp)
    with the sniffed content type; a CMYK JPEG becomes RGB, and PNG and WebP keep their
    transparency."""
    assert upload(client, make(fmt, mode=mode, color=color)).status_code == 200
    stored = storage.storage.uploads[0]
    assert image_upload.sniff_image_type(stored["data"])[0] == ext
    assert stored["options"]["content-type"] == {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
    assert stored["path"].endswith("." + ext)
    assert stored_image(storage).mode == stored_mode


def test_upload_of_a_palette_png_with_transparency_keeps_it(client, storage):
    """Stores a palette PNG with a transparent colour as an RGBA PNG whose transparent
    pixels are still transparent."""
    img = Image.new("P", (8, 8), 0)
    img.putpalette([255, 0, 0, 0, 255, 0])
    out = io.BytesIO()
    img.save(out, "PNG", transparency=0)
    assert upload(client, out.getvalue(), name="a.png", ctype="image/png").status_code == 200
    stored = stored_image(storage)
    assert stored.mode == "RGBA" and stored.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_upload_of_an_animated_image_keeps_only_the_first_frame(client, storage, fmt):
    """Stores an animated PNG or WebP of three frames (red, green, blue) as a still
    image of the first, red frame."""
    frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    assert Image.open(io.BytesIO(out.getvalue())).n_frames == 3
    assert upload(client, out.getvalue()).status_code == 200
    stored = stored_image(storage)
    assert getattr(stored, "n_frames", 1) == 1
    red, green, blue = stored.convert("RGB").getpixel((8, 8))
    assert red > 200 and green < 50 and blue < 50


def test_upload_of_a_multi_picture_jpeg_is_accepted_as_a_jpeg(client, storage):
    """Returns HTTP 200 for a multi-picture JPEG (MPO, as some cameras save) and stores
    a plain single-image JPEG of its first picture."""
    first, second = Image.new("RGB", (16, 16), (255, 0, 0)), Image.new("RGB", (16, 16), (0, 0, 255))
    out = io.BytesIO()
    first.save(out, "MPO", save_all=True, append_images=[second])
    assert Image.open(io.BytesIO(out.getvalue())).format == "MPO"
    assert upload(client, out.getvalue()).status_code == 200
    stored = stored_image(storage)
    assert stored.format == "JPEG" and stored.convert("RGB").getpixel((8, 8))[0] > 200


def test_product_image_upload_is_re_encoded_the_same_way(client, storage):
    """POST /products/{id}/image stores the re-encoded image too: a JPEG sent with GPS
    EXIF is stored without it, and a 50,000 x 50,000 bomb is refused with HTTP 413."""
    path = f"/products/{PROD_ID}/image"
    assert upload(client, make("JPEG", exif=gps_exif()), path=path).status_code == 200
    assert b"SpyCam" not in storage.storage.uploads[0]["data"]
    assert len(stored_image(storage).getexif()) == 0
    assert upload(client, png_declaring(50000, 50000), path=path).status_code == 413


# --- The multipart reader ------------------------------------------------------------

@pytest.mark.parametrize("boundary", ["B" * 71, "", "bad<char", "caf\u00E9", "ends-with-space "],
                         ids=["71-chars", "empty", "angle-bracket", "non-ascii", "trailing-space"])
def test_multipart_boundary_must_be_1_to_70_permitted_characters(boundary):
    """parse_multipart_file refuses a boundary of 71 characters, an empty one, one with
    a character RFC 2046 does not allow, and one ending in a space, with "The multipart
    boundary must be 1 to 70 characters, of those RFC 2046 allows." """
    with pytest.raises(image_upload.MultipartError) as caught:
        image_upload.parse_multipart_file(b"", f'multipart/form-data; boundary="{boundary}"')
    assert str(caught.value) == "The multipart boundary must be 1 to 70 characters, of those RFC 2046 allows."


def test_multipart_boundary_of_70_permitted_characters_is_accepted():
    """parse_multipart_file reads a body whose boundary is exactly 70 characters, using
    every punctuation mark RFC 2046 allows, quoted because it holds a space."""
    boundary = ("'()+_,-./:=? " + "x" * 70)[:69] + "Z"
    assert len(boundary) == 70
    body = multipart([(FILE_HEAD, b"DATA")], boundary)
    assert image_upload.parse_multipart_file(body, f'multipart/form-data; boundary="{boundary}"') == b"DATA"


def test_multipart_allows_10_parts_and_refuses_11():
    """parse_multipart_file reads a body of 10 parts (9 other fields and the file) and
    refuses one of 11 with "The upload has more than 10 parts." """
    other = (b'Content-Disposition: form-data; name="note"', b"x")
    assert image_upload.parse_multipart_file(multipart([other] * 9 + [(FILE_HEAD, b"F")]), CT) == b"F"
    with pytest.raises(image_upload.MultipartError, match="^The upload has more than 10 parts.$"):
        image_upload.parse_multipart_file(multipart([other] * 10 + [(FILE_HEAD, b"F")]), CT)


def test_multipart_part_headers_are_limited_in_size_and_number():
    """parse_multipart_file refuses a part whose headers exceed 8 KB ("A part's headers
    are too long.") or number more than 8 ("A part has too many headers."), and accepts
    8 headers."""
    long_head = FILE_HEAD + b"\r\nX-Pad: " + b"a" * (8 * 1024)
    with pytest.raises(image_upload.MultipartError, match="^A part's headers are too long.$"):
        image_upload.parse_multipart_file(multipart([(long_head, b"F")]), CT)
    eight = FILE_HEAD + b"".join(b"\r\nX-H%d: v" % i for i in range(6))
    assert image_upload.parse_multipart_file(multipart([(eight, b"F")]), CT) == b"F"
    nine = eight + b"\r\nX-H9: v"
    with pytest.raises(image_upload.MultipartError, match="^A part has too many headers.$"):
        image_upload.parse_multipart_file(multipart([(nine, b"F")]), CT)


def test_upload_with_two_file_fields_is_refused_with_422(client, storage):
    """Returns HTTP 422 {"detail": "Send exactly one file, in a field named 'file'."}
    for a body with two parts named "file", and stores nothing."""
    png = make("PNG")
    resp = client.post("/submissions/images", files=[("file", ("a.png", png, "image/png")),
                                                     ("file", ("b.png", png, "image/png"))])
    assert resp.status_code == 422
    assert resp.json() == {"detail": "Send exactly one file, in a field named 'file'."}
    assert storage.storage.uploads == []


@pytest.mark.parametrize("body", [
    b"--BOUND\r\n" + FILE_HEAD + b"\r\n\r\nDATA",                               # no closing delimiter
    b"--BOUND\r\n" + FILE_HEAD + b"\r\n\r\nDATA\r\n--BOUND",                     # delimiter, never closed
    b"--BOUND\r\nContent-Disposition form-data\r\n\r\nDATA\r\n--BOUND--",       # header with no colon
    b'--BOUND\r\nContent-Disposition: form-data; name="file\r\n\r\nX\r\n--BOUND--',   # unclosed quote
    b'--BOUND\r\nContent-Disposition: form-data; name="x"; name="file"\r\n\r\nX\r\n--BOUND--',
    b"--BOUND\r\nContent-Type: image/png\r\n\r\nX\r\n--BOUND--",                 # no Content-Disposition
    b"no delimiter at all",
    b"--BOUNDjunk\r\n" + FILE_HEAD + b"\r\n\r\nX\r\n--BOUND--",                # text glued to the delimiter
    b"--BOUND\r\nX-Junk\r\n" + FILE_HEAD + b"\r\n\r\nX\r\n--BOUND--",          # a bare header word, no colon
    b'--BOUND\r\nContent-Disposition: attachment; name="file"\r\n\r\nX\r\n--BOUND--',   # not form-data
], ids=["no-closing", "unclosed", "header-no-colon", "unclosed-quote", "name-twice", "no-disposition",
        "no-delimiter", "junk-after-delimiter", "bare-header-word", "attachment-not-form-data"])
def test_malformed_multipart_body_is_refused_with_422_not_500(client, storage, body):
    """Returns HTTP 422 {"detail": "The upload is malformed or incomplete."} for a body
    cut off before its closing delimiter, a header with no colon, an unclosed quote, a
    name given twice, a part with no Content-Disposition, no delimiter at all, text
    glued to a delimiter, or a disposition other than form-data; never 500, and nothing
    is stored."""
    resp = client.post("/submissions/images", content=body, headers={"content-type": CT})
    assert resp.status_code == 422
    assert resp.json() == {"detail": "The upload is malformed or incomplete."}
    assert storage.storage.uploads == []


@pytest.mark.parametrize("content_type, body", [
    ('multipart/form-data; boundary="' + "a" * 200_000, b""),
    ("multipart/form-data; boundary=B; " + "; " * 200_000 + "x", b"--B--"),
    ("multipart/form-data; boundary=B", b"--B\r\n" * 1_000_000),
    ("multipart/form-data; boundary=B", b"--B" + b" " * 4_000_000),
    ("multipart/form-data; boundary=B", b"--B\r\nContent-Disposition: form-data; " + b"; " * 4000 + b"\r\n\r\nX\r\n--B--"),
    ("multipart/form-data; boundary=B", b"--B\r\n" + b"a" * 5_000_000),
    ("multipart/form-data; boundary=B", b"--B\r\n" + FILE_HEAD + b"\r\n\r\n" + b"\r\n-" * 2_000_000),
], ids=["unclosed-quoted-boundary", "many-empty-params", "a-million-delimiters", "4MB-padding",
        "many-disposition-params", "5MB-header-no-end", "2M-near-delimiters"])
def test_multipart_reader_stays_fast_on_hostile_input(content_type, body):
    """parse_multipart_file answers each hostile input (huge or unclosed parameters, a
    million delimiters, megabytes of padding or header with no end, millions of near
    delimiters) within 2 seconds, with the file or a MultipartError: it has no regex
    that could backtrack."""
    start = time.perf_counter()
    try:
        image_upload.parse_multipart_file(body, content_type)
    except image_upload.MultipartError:
        pass
    assert time.perf_counter() - start < 2.0


# --- Upload rate limit ----------------------------------------------------------------

def test_the_21st_upload_in_an_hour_is_refused_with_429_and_retry_after(client, storage, as_user):
    """Returns HTTP 200 for a user's first 20 uploads and HTTP 429 {"detail": "Too many
    image uploads: at most 20 an hour. Try again later."} with a Retry-After header of
    whole seconds (1 to 3600) for the 21st, which is not stored; another user can still
    upload."""
    png = make("PNG")
    for _ in range(20):
        assert upload(client, png, name="a.png", ctype="image/png").status_code == 200
    resp = upload(client, png, name="a.png", ctype="image/png")
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Too many image uploads: at most 20 an hour. Try again later."}
    assert 1 <= int(resp.headers["retry-after"]) <= 3600
    assert len(storage.storage.uploads) == 20
    as_user("user-2")
    assert upload(client, png, name="a.png", ctype="image/png").status_code == 200


def test_the_admin_product_image_upload_is_not_rate_limited(client, storage):
    """POST /products/{id}/image is not counted: an admin's 21st product image upload in
    an hour returns HTTP 200."""
    png = make("PNG")
    for _ in range(21):
        assert upload(client, png, path=f"/products/{PROD_ID}/image").status_code == 200


def test_rate_limiter_frees_a_slot_once_the_oldest_hit_is_an_hour_old():
    """SlidingWindowLimiter(20, 3600), given 20 hits one second apart from t=1000,
    refuses the 21st at t=1019 with 3581 seconds to wait (until the first is an hour
    old) and at t=4599 with 1; at t=4600 it allows one hit, then refuses the next with
    1 second, until the second hit is an hour old."""
    now = [1000.0]
    limiter = SlidingWindowLimiter(20, 3600, clock=lambda: now[0])
    for i in range(20):
        now[0] = 1000.0 + i
        assert limiter.hit("u") is None
    assert limiter.hit("u") == 3581
    now[0] = 4599.0
    assert limiter.hit("u") == 1
    now[0] = 4600.0
    assert limiter.hit("u") is None
    assert limiter.hit("u") == 1


# --- public_url -------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "../secret.png", "submissions/../../etc/passwd", "https://evil.example/a.png",
    "submissions/0f8fad5b-d9cb-469f-a165-70867728950e.png\n", "other/0f8fad5b-d9cb-469f-a165-70867728950e.png",
    "submissions\\0f8fad5b-d9cb-469f-a165-70867728950e.png", "", None,
])
def test_public_url_refuses_anything_but_an_upload_path(path):
    """image_upload.public_url raises ValueError for a path that is not
    "<submissions|products>/<uuid>.<jpg|png|webp>": traversal, an absolute URL, a
    trailing newline, another folder, a backslash, empty or null."""
    with pytest.raises(ValueError):
        image_upload.public_url(path)


def test_public_url_built_by_the_real_storage_client_stays_in_our_bucket(monkeypatch):
    """With the real storage3 client (no network), the public URL of every upload path
    shape is <project>/storage/v1/object/public/product-images/<the path>."""
    from storage3 import SyncStorageClient

    real = SyncStorageClient("https://test-project.supabase.co/storage/v1/", {})

    class Client:
        storage = real

    monkeypatch.setattr(image_upload, "supabase", Client)
    base = "https://test-project.supabase.co/storage/v1/object/public/product-images/"
    for folder in ("submissions", "products"):
        for ext in ("jpg", "png", "webp"):
            path = f"{folder}/0f8fad5b-d9cb-469f-a165-70867728950e.{ext}"
            assert image_upload.public_url(path) == base + path


def test_upload_treats_pillows_bomb_warning_as_a_refusal(client, storage, monkeypatch):
    """With our own cap lifted and Pillow's threshold set to 1,000 pixels, a 40x40 image
    (1,600 pixels: over the threshold, under twice it, where Pillow only warns) is
    refused with HTTP 413 and the "too many pixels" detail, not decoded."""
    monkeypatch.setattr(image_upload, "MAX_PIXELS", 10 ** 12)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    resp = upload(client, make("PNG", size=(40, 40)), name="a.png", ctype="image/png")
    assert resp.status_code == 413
    assert resp.json() == {"detail": TOO_MANY_PIXELS}
    assert storage.storage.uploads == []


def test_upload_with_no_photo_says_so_in_plain_words(client, storage):
    """Returns HTTP 422 {"detail": "No photo was received. Please choose a photo and try
    again."} when the multipart body has no "file" field, since the page shows this
    text to the person uploading as it is, and stores nothing."""
    resp = client.post("/submissions/images", files={"photo": ("a.png", b"x", "image/png")})
    assert resp.status_code == 422
    assert resp.json() == {"detail": "No photo was received. Please choose a photo and try again."}
    assert storage.storage.uploads == []
