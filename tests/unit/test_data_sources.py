"""Tests for the helpers that carry data sources (migration 0009).

obf_product_url (app/db/ingest_catalog.py) turns the barcode Open Beauty Facts
returns into the product page stored as products.source_url. linked_sources
(app/core/services/compatibility_service.py) reads the source rows out of a
junction join such as concern_sources(sources(*)).

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.core.services.compatibility_service import linked_sources
from app.db.ingest_catalog import obf_product_url


def test_a_barcode_becomes_its_open_beauty_facts_page():
    """Returns the product's Open Beauty Facts page for a numeric barcode, with
    surrounding whitespace ignored and an integer barcode accepted."""
    assert obf_product_url(" 3606000537576 ") == "https://world.openbeautyfacts.org/product/3606000537576"
    assert obf_product_url(8809598454354) == "https://world.openbeautyfacts.org/product/8809598454354"


@pytest.mark.parametrize("code", [None, "", "   ", "not-a-code", "3606 000", "36060005375x6"])
def test_no_usable_barcode_gives_no_link(code):
    """Returns None rather than a malformed link when the barcode is missing or
    not all digits, so a product is never credited to the wrong page."""
    assert obf_product_url(code) is None


def test_linked_sources_keeps_only_real_source_rows():
    """Returns the source row of each link, in order, skipping links whose
    source row is missing, and an empty list for no links at all."""
    a = {"id": "a", "title": "A"}
    b = {"id": "b", "title": "B"}
    assert linked_sources([{"sources": a}, {"sources": None}, {"sources": b}]) == [a, b]
    assert linked_sources(None) == []
    assert linked_sources([]) == []
