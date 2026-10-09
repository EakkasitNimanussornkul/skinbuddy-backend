"""GET /products/search: limit, offset and the X-Total-Count header.

A page is cut from the whole result after it has been filtered and ranked, so
the pages of one query put side by side are the unpaged response, and the total
counts every match, not the page.
"""

import copy

import pytest

from app.core.services.token import get_optional_user_id

ORIGIN = "http://localhost:5173"


def _product(pid, name, brand="Brand", category="Serum", ingredients=()):
    return {
        "id": pid, "brand": brand, "name": name, "category": category, "slug": None,
        "description": "d", "price_thb": 500, "price_usd": 15, "image_url": None,
        "product_sources": [],
        "product_ingredients": [{"ingredients": i} for i in ingredients],
    }


GLYCERIN = {"id": "ing-gly", "name": "Glycerin", "functional_group": "Humectant", "good_for": "Dry Skin"}

# Stored in this order. For q="zinc": z-name and z-name2 match by name (rank 1), z-brand by
# brand (rank 2), z-cat by category only (rank 3); "other" does not match.
STORE = [
    _product("z-cat", "Plain Gel", category="Zinc Care"),
    _product("other", "Moisturizer"),
    _product("z-brand", "Light Lotion", brand="Zinc Labs"),
    _product("z-name", "Zinc Serum"),
    _product("z-name2", "Zinc Cream"),
]


def ids(resp):
    return [p["id"] for p in resp.json()]


def walk(client, params, step):
    """Every page of a query, `step` products at a time, and the totals the headers gave."""
    out, totals, offset = [], set(), 0
    while True:
        resp = client.get("/products/search", params={**params, "limit": step, "offset": offset})
        assert resp.status_code == 200
        totals.add(resp.headers["x-total-count"])
        out += resp.json()
        if len(resp.json()) < step:
            return out, totals
        offset += step


def test_without_limit_or_offset_every_product_is_returned_with_the_total_header(client, patch_supabase):
    """Returns all five products in the order a search always gave them, and
    X-Total-Count 5, when neither limit nor offset is sent."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = client.get("/products/search")

    assert resp.status_code == 200
    assert ids(resp) == ["z-cat", "other", "z-brand", "z-name", "z-name2"]
    assert resp.headers["x-total-count"] == "5"


def test_limit_and_offset_return_the_matching_slice_of_the_unpaged_order(client, patch_supabase):
    """Returns the products at positions 1 and 2 of the unpaged order for limit=2
    offset=1, and the last one alone for limit=2 offset=4."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")
    full = ids(client.get("/products/search"))

    assert ids(client.get("/products/search", params={"limit": 2, "offset": 1})) == full[1:3]
    assert ids(client.get("/products/search", params={"limit": 2, "offset": 4})) == full[4:]


def test_the_pages_of_a_query_put_together_are_the_unpaged_response(client, patch_supabase):
    """Returns, for a signed-in user, pages of 1, 2, 3, 7 and 10 products that join to
    exactly the unpaged response, scores and breakdowns included, with the same
    X-Total-Count on every page."""
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "u-1"
    store = [_product(f"p-{i}", f"Serum {i}", ingredients=[GLYCERIN]) for i in range(7)]
    patch_supabase({"products": store, "users": [{"id": "u-1", "skin_type": "DRNT"}]}, "app.api.products")
    full = client.get("/products/search").json()

    assert full[0]["skin_match_score"] is not None
    for step in (1, 2, 3, 7, 10):
        pages, totals = walk(client, {}, step)
        assert pages == full
        assert totals == {"7"}


def test_the_page_is_cut_after_ranking_not_before(client, patch_supabase):
    """Returns the name match first for limit=1 offset=0 and the brand match for limit=1
    offset=2 under q="zinc", although the category-only match is stored first and the
    name matches last: the order is name matches, then brand, then category, and a page
    is a slice of that order."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    first = client.get("/products/search", params={"q": "zinc", "limit": 1, "offset": 0})
    third = client.get("/products/search", params={"q": "zinc", "limit": 1, "offset": 2})

    assert ids(first) == ["z-name"]
    assert ids(third) == ["z-brand"]
    assert ids(client.get("/products/search", params={"q": "zinc"})) == ["z-name", "z-name2", "z-brand", "z-cat"]


def test_the_total_counts_every_match_not_the_page(client, patch_supabase):
    """Returns X-Total-Count 4 for q="zinc" with limit=1, though one product is in the
    body and five are stored, and 2 for a price band that keeps two."""
    store = copy.deepcopy(STORE)
    for row in store[:3]:
        row["price_thb"] = 2000
    patch_supabase({"products": store}, "app.api.products")

    page = client.get("/products/search", params={"q": "zinc", "limit": 1})
    band = client.get("/products/search", params={"max_price": 600, "limit": 1})

    assert len(page.json()) == 1 and page.headers["x-total-count"] == "4"
    assert len(band.json()) == 1 and band.headers["x-total-count"] == "2"


def test_an_offset_past_the_end_returns_an_empty_list_and_the_total(client, patch_supabase):
    """Returns HTTP 200, [] and X-Total-Count 5 for offset=5 and for offset=50, and
    X-Total-Count 0 for a query that matches nothing."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    for offset in (5, 50):
        resp = client.get("/products/search", params={"offset": offset})
        assert resp.status_code == 200 and resp.json() == [] and resp.headers["x-total-count"] == "5"
    none = client.get("/products/search", params={"q": "nothing like this"})
    assert none.json() == [] and none.headers["x-total-count"] == "0"


def test_products_of_the_same_rank_keep_their_stored_order_across_pages(client, patch_supabase):
    """Returns the products no query matches in the order they are stored, page after
    page, so a product never appears on two pages or on none."""
    patch_supabase({"products": [_product(f"p-{i:02d}", f"Item {i}") for i in range(12)]}, "app.api.products")

    pages, _ = walk(client, {}, 5)

    assert [p["id"] for p in pages] == [f"p-{i:02d}" for i in range(12)]


def test_without_a_limit_at_most_100_products_come_back_and_the_total_says_how_many_match(client, patch_supabase):
    """Returns 100 products and X-Total-Count 120 when 120 match and no limit is sent;
    offset=100 limit=100 returns the remaining 20."""
    patch_supabase({"products": [_product(f"p-{i:03d}", f"Item {i}") for i in range(120)]}, "app.api.products")

    first = client.get("/products/search")
    rest = client.get("/products/search", params={"offset": 100, "limit": 100})

    assert len(first.json()) == 100 and first.headers["x-total-count"] == "120"
    assert ids(rest) == [f"p-{i:03d}" for i in range(100, 120)]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": "x"},
                                    {"offset": -1}, {"offset": "x"}])
def test_an_invalid_limit_or_offset_is_a_422(client, patch_supabase, params):
    """Returns HTTP 422 for limit 0, -1, 101 or a non-number, and for an offset below 0
    or a non-number."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")
    assert client.get("/products/search", params=params).status_code == 422


@pytest.mark.parametrize("params", [{"limit": 1}, {"limit": 100}, {"offset": 0}])
def test_the_edge_values_of_limit_and_offset_are_accepted(client, patch_supabase, params):
    """Returns HTTP 200 for limit 1, limit 100 and offset 0."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")
    assert client.get("/products/search", params=params).status_code == 200


def test_the_card_view_pages_exactly_like_the_default_view(client, patch_supabase):
    """Returns for view=card the same product ids, in the same pages and with the same
    X-Total-Count, as the default view for limit=2 at offsets 0, 2 and 4."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    for offset in (0, 2, 4):
        params = {"q": "zinc", "limit": 2, "offset": offset}
        default = client.get("/products/search", params=params)
        card = client.get("/products/search", params={**params, "view": "card"})
        assert ids(card) == ids(default)
        assert card.headers["x-total-count"] == default.headers["x-total-count"] == "4"
        assert all("product_ingredients" not in p for p in card.json())


def test_x_total_count_is_exposed_to_the_browser_through_cors(client, patch_supabase):
    """Returns Access-Control-Expose-Headers naming X-Total-Count on a search made from
    the frontend origin, with the allow-origin header beside it."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = client.get("/products/search", headers={"Origin": ORIGIN})

    assert resp.headers["access-control-allow-origin"] == ORIGIN
    assert "x-total-count" in resp.headers["access-control-expose-headers"].lower()
