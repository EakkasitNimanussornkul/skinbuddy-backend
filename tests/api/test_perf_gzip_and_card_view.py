"""Response compression (GZipMiddleware) and GET /products/search?view=card.

The card view must return the same personalised numbers as the default view, so
most tests below compare the two responses for the same data instead of
restating expected values.
"""


from app.core.services.token import get_optional_user_id
from tests.conftest import FakeQuery, FakeSupabase

ORIGIN = "http://localhost:5173"


def _ingredient(n, **extra):
    return {"id": f"ing-{n}", "name": f"Ingredient {n}", "functional_group": "Humectant", **extra}


def _product(pid, name, ingredients, **extra):
    return {
        "id": pid, "brand": "Brand", "name": name, "category": "Serum", "slug": None,
        "description": "d", "price_thb": 500, "price_usd": 15, "image_url": None,
        "product_sources": [{"claim": "listing", "sources": {"id": "s1", "title": "Brand page"}}],
        "product_ingredients": [{"ingredients": i} for i in ingredients],
        **extra,
    }


# Five ingredients: one suits dry skin, one is flagged for sensitive skin with a graded
# concern, one carries a fragrance (a safety flag), two are neutral.
RICH = _product("p-rich", "Rich Serum", [
    _ingredient(1, name="Glycerin", good_for="Dry Skin",
                ingredient_sources=[{"claim": "good_for", "sources": {"id": "s2", "title": "Study"}}]),
    _ingredient(2, name="Alcohol Denat.", bad_for="Sensitive Skin (S)",
                ingredient_concerns=[{"target_profile": "Sensitive Skin (S)", "severity": "High",
                                      "concern_title": "Strips oil", "concern_sources": []}]),
    _ingredient(3, name="Parfum"),
    _ingredient(4, name="Water"),
    _ingredient(5, name="Panthenol"),
])
BARE = _product("p-bare", "Bare Serum", [], product_sources=[])


def _as_user(skin_type="DSPT"):
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "user-1"
    return [{"id": "user-1", "skin_type": skin_type}]


# --- compression -------------------------------------------------------------------

def _many_products(count=40):
    return [_product(f"p-{i}", f"Serum {i}", [_ingredient(1, name="Glycerin")]) for i in range(count)]


def test_a_large_json_response_is_gzipped_and_decodes_to_the_same_json(client, patch_supabase):
    """Returns Content-Encoding gzip for a large /products/search response when the
    client sends Accept-Encoding gzip; the body is smaller on the wire than decoded,
    and decodes to exactly the JSON the same request returns without compression."""
    patch_supabase({"products": _many_products()}, "app.api.products")

    plain = client.get("/products/search", headers={"Accept-Encoding": "identity"})
    zipped = client.get("/products/search", headers={"Accept-Encoding": "gzip"})

    assert zipped.status_code == 200
    assert zipped.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in zipped.headers["vary"]
    assert zipped.num_bytes_downloaded < len(zipped.content) / 3
    assert zipped.json() == plain.json()


def test_a_response_is_not_compressed_when_the_client_does_not_ask(client, patch_supabase):
    """Returns no Content-Encoding header, and the same JSON, for a large response
    to a request with Accept-Encoding identity."""
    patch_supabase({"products": _many_products()}, "app.api.products")

    resp = client.get("/products/search", headers={"Accept-Encoding": "identity"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.num_bytes_downloaded == len(resp.content)


def test_a_small_response_is_not_compressed_even_when_the_client_asks(client, patch_supabase):
    """Returns no Content-Encoding header for a response under 1000 bytes, even with
    Accept-Encoding gzip, because compressing it would cost more than it saves."""
    patch_supabase({"products": []}, "app.api.products")

    resp = client.get("/products/search", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.json() == []
    assert "content-encoding" not in resp.headers


def test_cors_headers_are_present_on_a_gzipped_response(client, patch_supabase):
    """Returns the CORS allow-origin and allow-credentials headers for the frontend
    origin on a response that is also gzipped, with Vary naming both Origin and
    Accept-Encoding."""
    patch_supabase({"products": _many_products()}, "app.api.products")

    resp = client.get("/products/search", headers={"Accept-Encoding": "gzip", "Origin": ORIGIN})

    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["access-control-allow-origin"] == ORIGIN
    assert resp.headers["access-control-allow-credentials"] == "true"
    vary = resp.headers["vary"]
    assert "Origin" in vary and "Accept-Encoding" in vary


def test_a_cors_preflight_still_answers_with_the_middleware_in_place(client):
    """Returns HTTP 200 with the allow-origin and allow-methods headers for an OPTIONS
    preflight from the frontend origin, so adding compression did not disturb CORS."""
    resp = client.options("/products/search", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "GET",
        "Accept-Encoding": "gzip",
    })

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN
    assert "GET" in resp.headers["access-control-allow-methods"]


# --- ?view=card --------------------------------------------------------------------

def test_card_view_returns_the_default_items_fields_minus_the_trees(client, patch_supabase):
    """Returns, for view=card, every field the default item has except
    product_ingredients and product_sources, plus ingredient_count; no other field
    is added or dropped."""
    patch_supabase({"products": [RICH]}, "app.api.products")

    default = client.get("/products/search").json()[0]
    card = client.get("/products/search", params={"view": "card"}).json()[0]

    assert set(card) == (set(default) - {"product_ingredients", "product_sources"}) | {"ingredient_count"}
    assert "product_ingredients" not in card and "product_sources" not in card


def test_card_view_keeps_identical_scores_flags_and_scalars_for_a_signed_in_user(client, patch_supabase):
    """Returns the same skin_match_score, match_breakdown, match_reasons,
    caution_reasons, safety_flags, has_conflict, slug and product columns in card
    view as in the default view, for a user whose profile makes both a helpful and
    a flagged ingredient count."""
    patch_supabase({"products": [RICH, BARE], "users": _as_user("DSPT")}, "app.api.products")

    default = client.get("/products/search").json()
    card = client.get("/products/search", params={"view": "card"}).json()

    assert default[0]["skin_match_score"] is not None and default[0]["has_conflict"] is True
    assert default[0]["safety_flags"]["fragrance_free"] is False
    assert len(card) == len(default) == 2
    for d, c in zip(default, card):
        shared = set(c) - {"ingredient_count"}
        assert {k: c[k] for k in shared} == {k: d[k] for k in shared}


def test_card_view_lists_the_first_three_ingredient_names_in_order_and_counts_them_all(client, patch_supabase):
    """Returns top_ingredients ["Glycerin", "Alcohol Denat.", "Parfum"] (the first
    three names, in the order the default ingredient list uses) and
    ingredient_count 5 for a product with five ingredients."""
    patch_supabase({"products": [RICH]}, "app.api.products")

    card = client.get("/products/search", params={"view": "card"}).json()[0]
    default = client.get("/products/search").json()[0]

    assert card["top_ingredients"] == ["Glycerin", "Alcohol Denat.", "Parfum"]
    assert card["top_ingredients"] == [i["ingredients"]["name"] for i in default["product_ingredients"][:3]]
    assert card["ingredient_count"] == 5


def test_card_view_counts_zero_ingredients_for_a_product_without_any(client, patch_supabase):
    """Returns top_ingredients [] and ingredient_count 0 for a product with no
    ingredient rows, and skips a link row whose ingredient is missing when counting."""
    orphan = _product("p-orphan", "Orphan", [])
    orphan["product_ingredients"] = [{"ingredients": None}, {"ingredients": _ingredient(1)}]
    patch_supabase({"products": [BARE, orphan]}, "app.api.products")

    bare, orph = client.get("/products/search", params={"view": "card"}).json()

    assert (bare["top_ingredients"], bare["ingredient_count"]) == ([], 0)
    assert (orph["top_ingredients"], orph["ingredient_count"]) == (["Ingredient 1"], 1)


def test_card_view_is_anonymous_safe_and_keeps_its_unscored_fields(client, patch_supabase):
    """Returns skin_match_score null, match_breakdown null and has_conflict false in
    card view for a caller who is not signed in, as the default view does."""
    patch_supabase({"products": [RICH]}, "app.api.products")

    card = client.get("/products/search", params={"view": "card"}).json()[0]

    assert card["skin_match_score"] is None
    assert card["match_breakdown"] is None
    assert card["has_conflict"] is False


def test_card_view_keeps_the_search_ranking_and_filters(client, patch_supabase):
    """Returns the card items in the same order as the default view for a text query
    (a name match first), and applies the price bounds the same way."""
    by_brand = {**_product("p-b", "Other", []), "brand": "Zinc"}
    by_name = _product("p-n", "Zinc Serum", [])
    cheap = {**_product("p-c", "Zinc Cheap", []), "price_thb": 100}
    patch_supabase({"products": [by_brand, by_name, cheap]}, "app.api.products")

    for params in ({"q": "zinc"}, {"min_price": 300}):
        default = client.get("/products/search", params=params).json()
        card = client.get("/products/search", params={**params, "view": "card"}).json()
        assert [p["id"] for p in card] == [p["id"] for p in default]
    assert [p["id"] for p in client.get("/products/search", params={"q": "zinc", "view": "card"}).json()][0] == "p-n"


def test_the_default_view_is_unchanged_and_carries_no_card_only_field(client, patch_supabase):
    """Returns product_ingredients and product_sources in full and no
    ingredient_count when view is omitted, so the default response keeps its shape."""
    patch_supabase({"products": [RICH]}, "app.api.products")

    item = client.get("/products/search").json()[0]

    assert len(item["product_ingredients"]) == 5
    assert item["product_sources"] == RICH["product_sources"]
    assert "ingredient_count" not in item


def test_an_unknown_view_is_rejected_with_422(client, patch_supabase):
    """Returns HTTP 422 for view=banana, as an unknown status does on
    /submissions/admin, instead of silently answering the default shape."""
    patch_supabase({"products": [RICH]}, "app.api.products")

    assert client.get("/products/search", params={"view": "banana"}).status_code == 422


class _SelectRecorder(FakeSupabase):
    """Fake client that remembers every select string the products table was given."""
    def __init__(self, store):
        super().__init__(store)
        self.selects = []

    def table(self, name):
        owner = self

        class Q(FakeQuery):
            def select(self, columns="*", **kw):
                if name == "products":
                    owner.selects.append(columns)
                return self
        return Q(name, self.store)


def test_card_view_reads_the_slim_select_and_the_default_reads_the_full_one(client, monkeypatch):
    """Selects CARD_PRODUCT_SELECT (no product_sources, no source bodies) from the
    products table for view=card, and PRODUCT_SELECT for the default view."""
    from app.api import products
    fake = _SelectRecorder({"products": [RICH]})
    monkeypatch.setattr(products, "supabase", fake)

    client.get("/products/search", params={"view": "card"})
    client.get("/products/search")

    assert fake.selects == [products.CARD_PRODUCT_SELECT, products.PRODUCT_SELECT]
    assert "product_sources" not in products.CARD_PRODUCT_SELECT
    assert "sources(*)" not in products.CARD_PRODUCT_SELECT
