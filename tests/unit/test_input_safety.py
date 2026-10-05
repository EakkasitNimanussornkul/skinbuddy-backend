"""The link rules and text cleaning in app/core/input_safety.py, and the
image_path checks in app/schemas.py. Pure functions: no app, no database."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core import input_safety
from app.core.input_safety import check_public_url, clean_text
from app.schemas import HttpUrlText, ProductImagePath, SubmissionImagePath

M = input_safety
UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


# --- Links ------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://brand.example/gel", "http://s1.example", "https://www.watsons.co.th/th/p/BP_123?x=1#y",
    "https://b\u00FCcher.de/seite", "https://xn--bcher-kva.de/", "http://8.8.8.8/", "https://[2606:4700::1111]/",
    "http://a.example:80/", "https://a.example:443/", "https://example.com.", "HTTPS://Example.COM/",
    "http://[::ffff:8.8.8.8]/",
])
def test_public_url_rules_accept_ordinary_public_links(url):
    """check_public_url returns the URL unchanged for ordinary public http(s) links: a
    path, query and fragment, an internationalised or punycode host, a public IP, the
    default port written out, a trailing-dot host, upper-case letters, and an
    IPv4-mapped IPv6 address of a public IPv4 address."""
    assert check_public_url(url) == url


REFUSED = [
    ("ftp://a.example/x", M.MSG_SCHEME), ("javascript:alert(1)", M.MSG_SCHEME), ("https:///path", M.MSG_SCHEME),
    ("http://user:pw@a.example/", M.MSG_CREDENTIALS), ("http://a.example@b.example/", M.MSG_CREDENTIALS),
    ("http://localhost/", M.MSG_LOCAL), ("http://LOCALHOST./", M.MSG_LOCAL), ("http://app.localhost/", M.MSG_LOCAL),
    ("http://printer.local/", M.MSG_LOCAL), ("http://db.internal/", M.MSG_LOCAL), ("http://nas.home.arpa/", M.MSG_LOCAL),
    ("http://\uFF4C\uFF4F\uFF43\uFF41\uFF4C\uFF48\uFF4F\uFF53\uFF54/", M.MSG_LOCAL),
    ("http://intranet/", M.MSG_SINGLE_LABEL), ("https://router/admin", M.MSG_SINGLE_LABEL),
    ("http://127.0.0.1/", M.MSG_PRIVATE_IP), ("http://10.0.0.5/", M.MSG_PRIVATE_IP),
    ("http://172.16.0.1/", M.MSG_PRIVATE_IP), ("http://192.168.1.1/", M.MSG_PRIVATE_IP),
    ("http://169.254.169.254/latest/meta-data/", M.MSG_PRIVATE_IP), ("http://0.0.0.0/", M.MSG_PRIVATE_IP),
    ("http://100.64.0.1/", M.MSG_PRIVATE_IP), ("http://224.0.0.1/", M.MSG_PRIVATE_IP),
    ("http://240.0.0.1/", M.MSG_PRIVATE_IP), ("http://\u2460\u2461\u2466.\uFF10.\uFF10.\uFF11/", M.MSG_PRIVATE_IP),
    ("http://[::1]/", M.MSG_PRIVATE_IP), ("http://[fe80::1%25eth0]/", M.MSG_PRIVATE_IP),
    ("http://[fc00::1]/", M.MSG_PRIVATE_IP), ("http://[::ffff:127.0.0.1]/", M.MSG_PRIVATE_IP),
    ("http://[::ffff:7f00:1]/", M.MSG_PRIVATE_IP), ("http://[2002:c0a8:101::]/", M.MSG_PRIVATE_IP),
    ("http://[64:ff9b::a00:1]/", M.MSG_PRIVATE_IP), ("http://[::]/", M.MSG_PRIVATE_IP),
    ("http://[2002:808:808::]/", M.MSG_PRIVATE_IP), ("http://[64:ff9b::808:808]/", M.MSG_PRIVATE_IP),
    ("http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/", M.MSG_PRIVATE_IP),
    ("http://2130706433/", M.MSG_OBFUSCATED_IP), ("http://0x7f000001/", M.MSG_OBFUSCATED_IP),
    ("http://0177.0.0.1/", M.MSG_OBFUSCATED_IP), ("http://127.1/", M.MSG_OBFUSCATED_IP),
    ("http://0x7f.0.0.1/", M.MSG_OBFUSCATED_IP), ("http://134744072/", M.MSG_OBFUSCATED_IP),
    ("http://a.example/x y", M.MSG_SPACE), ("http://a.example/\tx", M.MSG_SPACE), ("http://a.example/\x00", M.MSG_SPACE),
    ("http://a.example/\x7f", M.MSG_SPACE), ("http://a.example/\u00A0", M.MSG_SPACE),
    ("http://a.example/\u202Egnp.exe", M.MSG_SPACE), ("http://a.ex\u200Bample/", M.MSG_SPACE),
    ("http://a.example\\@b.example/", M.MSG_BACKSLASH), ("http://a.example/\\x", M.MSG_BACKSLASH),
    ("https://a.example:8443/", M.MSG_PORT), ("http://a.example:443/", M.MSG_PORT), ("http://a.example:0/", M.MSG_PORT),
    ("http://%6c%6fcalhost/", M.MSG_HOST), ("http://a..example/", M.MSG_HOST), ("http://-a.example/", M.MSG_HOST),
    ("http://a_b.example/", M.MSG_HOST), ("http://1.2.3.4.5/", M.MSG_HOST), ("http://foo.123/", M.MSG_HOST),
    ("http://256.1.1.1/", M.MSG_HOST), ("http://a.example:99999/", M.MSG_HOST), ("http://[::1/", M.MSG_HOST),
    ("http://[not-an-ip]/", M.MSG_HOST), ("http://1.2.3.256/", M.MSG_HOST), ("http://4294967296/", M.MSG_HOST),
]


@pytest.mark.parametrize("url, message", REFUSED, ids=[ascii(u) for u, _ in REFUSED])
def test_public_url_rules_refuse_internal_and_disguised_links(url, message):
    """check_public_url raises ValueError with the reason for: a scheme other than
    http(s); a user name or password; localhost, *.localhost, *.local, *.internal and
    *.home.arpa (also typed full-width); a single-label host; an IP in a private,
    loopback, link-local, shared, multicast, reserved or unspecified range, v4 or v6,
    an IPv4-mapped IPv6 address of a private IPv4 address, and every 6to4, Teredo or
    NAT64 address, even one carrying a public address like 8.8.8.8; an IP written as an
    integer, in hex or in octal; spaces, control or invisible characters; a backslash;
    a port other than the scheme's default; and a host that is not a valid name."""
    with pytest.raises(ValueError) as caught:
        check_public_url(url)
    assert str(caught.value) == message


def test_http_url_text_strips_surrounding_whitespace_only():
    """HttpUrlText stores the URL exactly as sent apart from surrounding whitespace: it
    is not lower-cased, re-encoded to punycode or otherwise rewritten."""
    adapter = TypeAdapter(HttpUrlText)
    assert adapter.validate_python("  https://B\u00FCcher.DE/Seite?Q=1  ") == "https://B\u00FCcher.DE/Seite?Q=1"
    with pytest.raises(ValidationError):
        adapter.validate_python("http://localhost/")


def test_the_backend_never_fetches_a_user_supplied_url():
    """No route module or request-time service makes an outbound HTTP request except
    LINE login (auth.py, line_service.py): the source links users submit are stored and
    shown, never fetched, so the link rules are about what is published, not SSRF."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    fetching = set()
    for path in list((root / "api").glob("*.py")) + list((root / "core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(word in text for word in ("httpx", "requests.", "urlopen", "aiohttp")):
            fetching.add(path.name)
    # image_service.py is used only by the offline catalogue loaders (app/db), and
    # fetches only from its allow-listed host.
    assert fetching == {"auth.py", "line_service.py", "image_service.py"}


# --- Text -------------------------------------------------------------------------

FRONTEND_SET = ([chr(c) for c in range(0x200B, 0x2010)] + [chr(c) for c in range(0x202A, 0x202F)]
                + [chr(c) for c in range(0x2066, 0x206A)] + ["\uFEFF"])
EXTRA_SET = (["\u00AD", "\u061C", "\u180E"] + [chr(c) for c in range(0x2060, 0x2065)]
             + [chr(c) for c in range(0x206A, 0x2070)] + [chr(c) for c in range(0xFFF9, 0xFFFC)]
             + [chr(0xE0000), chr(0xE0041), chr(0xE007F)])


@pytest.mark.parametrize("ch", FRONTEND_SET + EXTRA_SET, ids=lambda c: f"U+{ord(c):04X}")
def test_clean_text_removes_each_invisible_character(ch):
    """clean_text removes every character the frontend strips (U+200B-200F,
    U+202A-202E, U+2066-2069, U+FEFF) and the other invisible format characters
    (U+00AD, U+061C, U+180E, U+2060-2064, U+206A-206F, U+FFF9-FFFB and the tag
    characters U+E0000-E007F), in single- and multi-line fields."""
    assert clean_text(f"Glow{ch}Lab") == "GlowLab"
    assert clean_text(f"Glow{ch}Lab", multiline=True) == "GlowLab"


def test_clean_text_turns_control_characters_into_spaces_on_one_line():
    """In a single-line field, tab, CR, LF, NUL, the other C0 and C1 controls and
    U+2028/2029 each become a space."""
    assert clean_text("Glow\tLab\r\nGel\x00X\x1fY\x85Z\u2028W") == "Glow Lab  Gel X Y Z W"


def test_clean_text_keeps_line_breaks_and_tabs_in_multi_line_fields():
    """In a multi-line field (note, review_notes, description), line breaks are kept as
    "\\n" (CRLF, CR, U+2028 and U+2029 included) and tabs are kept; NUL, other control
    characters and an RLO are removed."""
    assert clean_text("a\r\nb\rc\u2028d\u2029e\tf\x00g\x07h\u202Ei", multiline=True) == "a\nb\nc\nd\ne\tfghi"


def test_clean_text_removes_lone_surrogates_and_leaves_ordinary_text_alone():
    """clean_text removes a lone surrogate (which cannot be stored as UTF-8), and leaves
    Thai, accented letters, emoji and punctuation untouched; a non-string is returned
    as it is."""
    assert clean_text("a\ud800b") == "ab"
    text = "\u0E40\u0E0B\u0E23\u0E31\u0E48\u0E21 Caf\u00E9 \U0001F33F 10% (SPF50+)"
    assert clean_text(text) == text
    assert clean_text(5) == 5


# --- image_path -------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "../submissions/{u}.png", "submissions/../{u}.png", "submissions/{u}.png/../../x",
    "submissions/%2e%2e/{u}.png", "submissions%2f{u}.png", "submissions\\{u}.png", "/submissions/{u}.png",
    "https://evil.example/submissions/{u}.png", "submissions/{u}.png\n", "submissions/{u}.png?x=1",
    "submissions/{u}.PNG", "submissions/{u}.svg", "submissions/{u}.png ", " submissions/{u}.png",
    "submissions//{u}.png", "submissions/{U}.png",
])
def test_image_paths_refuse_traversal_encoding_and_foreign_locations(path):
    """SubmissionImagePath and ProductImagePath refuse "..", percent-encoded dots or
    slashes, backslashes, a leading slash, an absolute URL, a trailing newline or
    space, a query string, another extension or case, and an upper-case UUID: only
    "<folder>/<lower-case uuid>.<jpg|png|webp>" passes."""
    value = path.format(u=UUID, U=UUID.upper())
    for kind in (SubmissionImagePath, ProductImagePath):
        with pytest.raises(ValidationError):
            TypeAdapter(kind).validate_python(value)


def test_image_paths_accept_exactly_the_upload_route_shapes():
    """SubmissionImagePath accepts "submissions/<uuid>.<jpg|png|webp>" only;
    ProductImagePath accepts that and "products/<uuid>.<ext>"."""
    for ext in ("jpg", "png", "webp"):
        assert TypeAdapter(SubmissionImagePath).validate_python(f"submissions/{UUID}.{ext}")
        assert TypeAdapter(ProductImagePath).validate_python(f"products/{UUID}.{ext}")
    with pytest.raises(ValidationError):
        TypeAdapter(SubmissionImagePath).validate_python(f"products/{UUID}.png")
