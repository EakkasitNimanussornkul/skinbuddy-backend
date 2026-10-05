"""Checks on what users type: links they cite, and text they submit.

Links
-----
A submission's source links (sources[].url, ingredients[].details.source_url),
an admin's product sources and the URLs ticked for publishing are shown on
product pages as links. The backend NEVER fetches them: no route requests a
user-supplied URL (the only outbound fetches are the offline catalogue loaders,
which accept images from one allow-listed host). So this is not SSRF
protection. It keeps junk and internal links out of what we publish: a link to
someone's router, a LAN name, a URL with a password in it, or a host disguised
as "http://2130706433/" (127.0.0.1) has no business on a product page.

The stored URL is never rewritten (beyond the whitespace trim the schemas
already do). The hostname is normalised to IDNA only for checking, because
UTS 46 mapping turns full-width or circled characters into ASCII: a host
typed as full-width "localhost" IS localhost to a browser.

Text
----
clean_text() removes characters that are invisible but change how text reads.
A right-to-left override (U+202E) can make a name display reversed, and
zero-width characters can make two names look identical while differing, which
matters most on the admin review screen. The set matches what the frontend
strips before sending (U+200B-200F, U+202A-202E, U+2066-2069, U+FEFF), plus a
few more invisible format characters; see INVISIBLE_CHARS.
"""

import ipaddress
from typing import List, Optional
from urllib.parse import urlsplit

import idna

# --- Text ----------------------------------------------------------------------

def _range(first: int, last: int) -> List[str]:
    return [chr(c) for c in range(first, last + 1)]


# Invisible format characters, removed from every submitted text field.
# The first four groups are exactly the frontend's set.
INVISIBLE_CHARS = frozenset(
    _range(0x200B, 0x200F)        # zero-width space, ZWNJ, ZWJ, LRM, RLM
    + _range(0x202A, 0x202E)      # bidi embeddings and overrides (LRE, RLE, PDF, LRO, RLO)
    + _range(0x2066, 0x2069)      # bidi isolates (LRI, RLI, FSI, PDI)
    + ["\uFEFF"]                  # zero-width no-break space / byte-order mark
    # Beyond the frontend's set: other characters that render as nothing.
    + ["\u00AD"]                  # soft hyphen
    + ["\u061C"]                  # Arabic letter mark (a bidi mark)
    + ["\u180E"]                  # Mongolian vowel separator
    + _range(0x2060, 0x2064)      # word joiner, invisible function application/times/separator/plus
    + _range(0x206A, 0x206F)      # deprecated format characters
    + _range(0xFFF9, 0xFFFB)      # interlinear annotation marks
    + _range(0xE0000, 0xE007F)    # tag characters ("ASCII smuggling")
)

# C0 and C1 control characters: U+0000-001F and U+007F-009F.
CONTROL_CHARS = frozenset(_range(0x00, 0x1F) + _range(0x7F, 0x9F))
LINE_SEPARATORS = frozenset("\u2028\u2029")


def clean_text(value, multiline: bool = False):
    """value with INVISIBLE_CHARS removed, and control characters handled:

    - single-line fields (names, brands, titles, benefits): every control
      character, tab and line break included, and U+2028/2029 become a space,
      so "Glow\\nLab" reads "Glow Lab" rather than "GlowLab";
    - multi-line fields (notes, descriptions): line breaks are kept as "\\n"
      (CRLF, CR, U+2028 and U+2029 all become "\\n") and tabs are kept; any
      other control character is removed.

    Lone surrogates (which cannot be stored as UTF-8) are removed too. A value
    that is not a string is returned as it is, for the schema to refuse.
    """
    if not isinstance(value, str):
        return value
    if multiline:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in value:
        if ch in INVISIBLE_CHARS or "\uD800" <= ch <= "\uDFFF":
            continue
        if ch in LINE_SEPARATORS:
            out.append("\n" if multiline else " ")
        elif ch in CONTROL_CHARS:
            if multiline and ch in "\n\t":
                out.append(ch)
            elif not multiline:
                out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# --- Links ---------------------------------------------------------------------

DEFAULT_PORTS = {"http": 80, "https": 443}
# Names that only ever mean "this machine" or "this network": RFC 6761
# (localhost), RFC 6762 (.local), RFC 8375 (home.arpa), ICANN's reserved
# .internal, and the common "localdomain".
LOCAL_NAMES = ("localhost", "local", "internal", "home.arpa", "localdomain")
HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")

MSG_SCHEME = "must be an http(s) URL"
MSG_SPACE = "must not contain spaces, control characters or invisible characters"
MSG_BACKSLASH = "must not contain a backslash"
MSG_CREDENTIALS = "must not contain a user name or password"
MSG_HOST = "must have a valid host name"
MSG_PORT = "must not name a port other than the default (80 for http, 443 for https)"
MSG_LOCAL = "must not point at a local or internal host"
MSG_SINGLE_LABEL = "must use a full public host name, like example.com"
MSG_PRIVATE_IP = "must not point at a private, loopback, link-local or reserved IP address"
MSG_OBFUSCATED_IP = "must write an IP address as four decimal numbers (a.b.c.d), not in integer, hex or octal form"


def _ipv4_number(part: str) -> Optional[int]:
    """One part of a WHATWG IPv4 host: decimal, 0x-hex or 0-led octal. None if invalid."""
    if part == "":
        return None
    base, digits = 10, "0123456789"
    if part[:2] == "0x":
        part, base, digits = part[2:], 16, "0123456789abcdef"
    elif len(part) > 1 and part[0] == "0":
        part, base, digits = part[1:], 8, "01234567"
    if part == "":
        return 0
    if not all(c in digits for c in part):
        return None
    return int(part, base)


def _ends_in_number(labels: List[str]) -> bool:
    """The WHATWG "ends in a number" test: browsers parse such a host as IPv4."""
    last = labels[-1]
    return last != "" and (last.isdigit() or _ipv4_number(last) is not None)


def _whatwg_ipv4(labels: List[str]) -> ipaddress.IPv4Address:
    """The address a browser reads from a host that ends in a number, as the
    WHATWG URL standard's IPv4 parser does: 127.1, 0x7f.1, 0177.0.0.1 and
    2130706433 are all 127.0.0.1. ValueError(MSG_HOST) where a browser fails."""
    numbers = [_ipv4_number(p) for p in labels]
    if len(numbers) > 4 or any(n is None for n in numbers):
        raise ValueError(MSG_HOST)
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise ValueError(MSG_HOST)
    value = numbers[-1]
    for i, n in enumerate(numbers[:-1]):
        value += n * 256 ** (3 - i)
    return ipaddress.IPv4Address(value)


def _refuse_non_public_ip(ip) -> None:
    """ValueError unless ip is a public unicast address.

    IPv6 forms that carry an IPv4 address need no special case: ipaddress
    judges an IPv4-mapped address (::ffff:0:0/96) by the IPv4 address it
    carries, counts every 6to4 (2002::/16) and Teredo (2001::/32) address as
    private, and every NAT64 (64:ff9b::/96) address as reserved.
    tests/unit/test_input_safety.py pins this, so a Python upgrade that
    changed it would fail there."""
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or (ip.version == 6 and ip.is_site_local)):
        raise ValueError(MSG_PRIVATE_IP)


def check_public_url(value: str) -> str:
    """value, unchanged, if it is an http(s) link fit to publish; ValueError
    with the reason otherwise. See the module docstring for why."""
    for ch in value:
        if ch.isspace() or ch in CONTROL_CHARS or ch in INVISIBLE_CHARS or ch == "\u00A0":
            raise ValueError(MSG_SPACE)
    # Browsers read "\" as "/" in an http URL, and URL parsers disagree on it,
    # so "http://a.example\@b.example" means different hosts to different readers.
    if "\\" in value:
        raise ValueError(MSG_BACKSLASH)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError(MSG_HOST)
    if parts.scheme not in DEFAULT_PORTS or not parts.netloc:
        raise ValueError(MSG_SCHEME)
    if "@" in parts.netloc:
        raise ValueError(MSG_CREDENTIALS)
    host = parts.hostname or ""
    if not host:
        raise ValueError(MSG_HOST)
    # Port: a published source is a public web page, on the default port. A
    # link to :8080 or :3000 is almost always a development or internal service.
    if port is not None and port != DEFAULT_PORTS[parts.scheme]:
        raise ValueError(MSG_PORT)

    if parts.netloc.startswith("["):                    # an IPv6 literal
        try:
            ip = ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError(MSG_HOST)
        _refuse_non_public_ip(ip)
        return value

    # A percent-encoded host is decoded by browsers; refuse rather than decode.
    if "%" in host:
        raise ValueError(MSG_HOST)
    try:
        ascii_host = idna.encode(host.rstrip("."), uts46=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError, ValueError):
        raise ValueError(MSG_HOST)
    labels = ascii_host.split(".")
    if not ascii_host or "" in labels or not set(ascii_host) <= HOST_CHARS:
        raise ValueError(MSG_HOST)

    if _ends_in_number(labels):
        ip = _whatwg_ipv4(labels)
        # Only the plain dotted-quad form is accepted. The integer, hex and
        # octal forms are refused whatever address they spell: no real source
        # link is written that way, and it is a classic way to hide 127.0.0.1.
        canonical = len(labels) == 4 and all(p.isdigit() and str(int(p)) == p for p in labels)
        if not canonical:
            raise ValueError(MSG_OBFUSCATED_IP)
        _refuse_non_public_ip(ip)
        return value

    if any(ascii_host == name or ascii_host.endswith("." + name) for name in LOCAL_NAMES):
        raise ValueError(MSG_LOCAL)
    if len(labels) < 2:
        raise ValueError(MSG_SINGLE_LABEL)
    return value
