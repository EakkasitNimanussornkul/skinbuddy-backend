"""API-level tests for the Products router, driven through FastAPI's TestClient.

These exercise real routing, dependency injection, and response-model
serialisation. The Supabase client is replaced with an in-memory fake (see
tests/conftest.py) so nothing touches the live database.
"""

import pytest

from app.core.services.token import get_optional_user_id

PROD_A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROD_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

RETINOL = {"id": "ing-retinol", "name": "Retinol", "functional_group": "Retinoid"}
SALICYLIC = {"id": "ing-sa", "name": "Salicylic Acid",
             "functional_group": "Beta Hydroxy Acid (BHA)"}
GLYCERIN = {"id": "ing-gly", "name": "Glycerin", "functional_group": "Humectant"}
PHENOXY = {"id": "ing-phe", "name": "Phenoxyethanol", "functional_group": "Preservative"}


def product(pid, brand, name, category, ingredients):
    return {
        "id": pid, "brand": brand, "name": name, "category": category,
        "slug": None, "description": None, "price_thb": 500, "price_usd": 15,
        "image_url": None,
        "product_ingredients": [{"ingredients": i} for i in ingredients],
    }


CLEANSER = product(PROD_A_ID, "CeraVe", "Hydrating Facial Cleanser", "Cleanser",
                   [GLYCERIN, PHENOXY])


# --- GET /products/search ----------------------------------------------------

def test_search_returns_enriched_products_anonymously(client, patch_supabase):
    """Returns HTTP 200 and a list of one enriched product carrying its generated
    slug and a preview of its first three ingredient names."""
    patch_supabase({"products": [CLEANSER]}, "app.api.products")

    resp = client.get("/products/search")
    assert resp.status_code == 200

    results = resp.json()
    assert len(results) == 1
    assert results[0]["slug"] == "cerave-hydrating-facial-cleanser"
    assert results[0]["top_ingredients"] == ["Glycerin", "Phenoxyethanol"]


def test_search_omits_personalisation_for_anonymous_callers(client, patch_supabase):
    """Returns skin_match_score=None, empty match reasons and has_conflict=False
    when no user is authenticated, since there is no skin type to score against."""
    patch_supabase({"products": [CLEANSER]}, "app.api.products")

    result = client.get("/products/search").json()[0]
    assert result["skin_match_score"] is None
    assert result["match_reasons"] == []
    assert result["has_conflict"] is False


def test_search_computes_a_skin_match_score_when_authenticated(client, patch_supabase):
    """Returns skin_match_score=85 and a barrier-repair match reason for a
    DSPT user, applying the +10 humectant bonus on the Dry axis."""
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "user-1"
    patch_supabase(
        {"products": [CLEANSER], "users": [{"id": "user-1", "skin_type": "DSPT"}]},
        "app.api.products",
    )

    result = client.get("/products/search").json()[0]
    # Base 75 + 10 for the humectant on a Dry-axis profile.
    assert result["skin_match_score"] == 85
    assert any("Barrier-repair" in r for r in result["match_reasons"])


def test_search_safety_flags_survive_the_full_http_round_trip(client, patch_supabase):
    """Returns alcohol_free=True and fungal_safe=True for a Phenoxyethanol and
    Glycerin formula, confirming the corrected flag logic survives serialisation
    through the HTTP response."""
    patch_supabase({"products": [CLEANSER]}, "app.api.products")

    flags = client.get("/products/search").json()[0]["safety_flags"]
    assert flags["alcohol_free"] is True
    assert flags["fungal_safe"] is True


def test_search_with_no_matching_products_returns_an_empty_list(client, patch_supabase):
    """Returns HTTP 200 and an empty list, not a 404, when the query matches no
    catalogue product."""
    patch_supabase({"products": []}, "app.api.products")

    resp = client.get("/products/search", params={"q": "nonexistent"})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.fixture
def capture_or(monkeypatch):
    """Record the expression handed to PostgREST's or_().

    The fake does not implement or_ — it answers through the no-op chain — so a
    status-code assertion cannot tell a safe filter from an injected one. The
    expression itself is the only observable that can, which is why BE-DEF-06's
    guard asserts on it rather than on the response.

    The class is taken off a live fake rather than imported: pytest loads
    conftest under its own module name, so `import tests.conftest` yields a
    second, unused copy of FakeQuery and patching it records nothing.
    """
    def _capture(fake):
        seen = []
        cls = type(fake.table("products"))
        original = getattr(cls, "or_", None)

        def spy(self, expr):
            seen.append(expr)
            return original(self, expr) if original else self

        monkeypatch.setattr(cls, "or_", spy, raising=False)
        return seen

    return _capture


@pytest.mark.parametrize("query", ["Vitamin C, 10%", "serum, cleanser", "a,b"])
def test_search_keeps_a_comma_inside_the_quoted_filter_value(
    client, patch_supabase, capture_or, query
):
    """Returns HTTP 200 for a query containing a comma, and sends exactly three
    filter conditions, so the comma cannot terminate one condition and start
    another (BE-DEF-06)."""
    fake = patch_supabase({"products": []}, "app.api.products")
    seen = capture_or(fake)

    resp = client.get("/products/search", params={"q": query})
    assert resp.status_code == 200

    expr = seen[0]
    assert expr.count("ilike") == 3
    for column in ("name", "brand", "category"):
        assert f'{column}.ilike."%{query}%"' in expr


def test_search_escapes_a_quote_that_would_break_out_of_the_filter(
    client, patch_supabase, capture_or
):
    """Returns the double quote backslash-escaped inside the filter expression,
    so a query containing one cannot close the value and inject a condition."""
    fake = patch_supabase({"products": []}, "app.api.products")
    seen = capture_or(fake)

    resp = client.get("/products/search", params={"q": 'x"y'})
    assert resp.status_code == 200
    assert 'name.ilike."%x\\"y%"' in seen[0]


# --- GET /products/compare ---------------------------------------------------

RETINOL_VS_SA_RULE = {
    "ingredient_a_id": "ing-retinol", "ingredient_b_id": "ing-sa",
    "severity": "high", "warning_message": "Use on alternate evenings.",
}
RETINOID_VS_BHA_CATEGORY_RULE = {
    "group_a": "Retinoid", "group_b": "Beta Hydroxy Acid (BHA)",
    "severity": "high", "warning_message": "Layering these raises irritation risk.",
}


def _compare_store(conflict_rules, category_conflict_rules):
    return {
        "products": [
            product(PROD_A_ID, "The Ordinary", "Retinol 0.2% in Squalane", "Serum", [RETINOL]),
            product(PROD_B_ID, "Paula's Choice", "2% BHA Liquid Exfoliant", "Exfoliant", [SALICYLIC]),
        ],
        "conflict_rules": conflict_rules,
        "category_conflict_rules": category_conflict_rules,
        "users": [],
    }


@pytest.fixture
def conflicting_catalog(patch_supabase):
    """Retinol vs BHA — the real catalogue's overlap, where the same clash is
    described twice: once as a curated ingredient pair and once as a functional
    group pair."""
    return patch_supabase(
        _compare_store([RETINOL_VS_SA_RULE], [RETINOID_VS_BHA_CATEGORY_RULE]),
        "app.api.products", "app.core.services.compatibility_service")


@pytest.fixture
def category_only_catalog(patch_supabase):
    """The same two products with no curated rule for the pair, so only the
    functional-group rule can report the clash."""
    return patch_supabase(
        _compare_store([], [RETINOID_VS_BHA_CATEGORY_RULE]),
        "app.api.products", "app.core.services.compatibility_service")


def test_compare_runs_the_ingredient_pair_pass(client, conflicting_catalog):
    """Returns a Chemical Interaction Warning for a curated ingredient pair.

    REGRESSION: compare only ran the category pass, so it silently
    under-reported conflicts that Shelf and Routine both surfaced."""
    resp = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID})
    assert resp.status_code == 200

    alert_types = {c["alert_type"] for c in resp.json()["conflicts"]}
    assert "Chemical Interaction Warning" in alert_types


def test_compare_runs_the_category_pass_when_no_curated_rule_covers_the_pair(
        client, category_only_catalog):
    """Returns an Active Routine Clash from the functional-group rule when no
    ingredient-pair rule exists, so the general rule still reaches the user.

    The other half of the three-pass guarantee: the pass is exercised on its own
    here because, where both rules describe the same clash, the general one is
    deliberately suppressed."""
    resp = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID})
    assert resp.status_code == 200

    alert_types = {c["alert_type"] for c in resp.json()["conflicts"]}
    assert "Active Routine Clash" in alert_types


def test_compare_states_one_clash_once_when_both_rule_tables_describe_it(
        client, conflicting_catalog):
    """Returns a single conflict for Retinol against a BHA exfoliant, the curated
    one, rather than restating the same clash as a category conflict.

    Regression guard for BE-DEF-13: conflict_rules and category_conflict_rules
    both carry Retinol + Salicylic Acid at high severity, so the user was shown
    two warnings naming the same product for one problem."""
    conflicts = client.get("/products/compare",
                           params={"product_a_id": PROD_A_ID,
                                   "product_b_id": PROD_B_ID}).json()["conflicts"]

    assert len(conflicts) == 1
    assert conflicts[0]["alert_type"] == "Chemical Interaction Warning"


def test_compare_works_without_authentication(client, conflicting_catalog):
    """Returns HTTP 200 with skin_match_score=None for an unauthenticated caller.
    analyze() previously ran an unconditional user lookup and 500'd on anonymous
    requests."""
    resp = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID})
    assert resp.status_code == 200
    assert resp.json()["product_a"]["skin_match_score"] is None


def test_compare_reports_zero_similarity_for_disjoint_formulas(client, conflicting_catalog):
    """Returns an empty shared_ingredients list and similarity_score=0.0 for two
    products whose ingredient sets do not intersect."""
    body = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID}).json()
    assert body["shared_ingredients"] == []
    assert body["similarity_score"] == 0.0


def test_compare_404s_when_a_product_cannot_be_resolved(client, patch_supabase):
    """Returns HTTP 404 when either product id cannot be resolved to a catalogue
    row, rather than a 500 or an empty comparison."""
    patch_supabase({"products": []}, "app.api.products",
                   "app.core.services.compatibility_service")

    resp = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID})
    assert resp.status_code == 404


# --- GET /products/slug/{slug} — similar products ----------------------------
#
# The widget tells the user these are "alternative formulations matched with
# similar active ingredient profiles", so the ranking has to be real. Before
# this, the endpoint returned whichever four same-category rows Supabase handed
# back and fetched no ingredient data at all.
#
# The fake cannot resolve the product_ingredients(ingredients(*)) join, so each
# candidate is seeded with the already-joined shape.

SERUM_A_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

BASE_SERUM = product(SERUM_A_ID, "The Ordinary", "Base Serum", "Serum",
                     [RETINOL, GLYCERIN, PHENOXY])
# 3 of 3 shared -> 100.0
TWIN = product("twin-id", "Brand T", "Twin Serum", "Serum", [RETINOL, GLYCERIN, PHENOXY])
# 1 shared, union 4 -> 25.0
PARTIAL = product("partial-id", "Brand P", "Partial Serum", "Serum", [RETINOL, SALICYLIC])
# 0 shared -> 0.0
DISJOINT = product("disjoint-id", "Brand D", "Disjoint Serum", "Serum", [SALICYLIC])
# Same ingredients as the base, but a different category.
OTHER_CATEGORY = product("cleanser-id", "Brand C", "Same Formula Cleanser", "Cleanser",
                         [RETINOL, GLYCERIN, PHENOXY])


def similar(resp):
    return resp.json()["similar_products"]


def test_slug_ranks_similar_products_by_ingredient_overlap(client, patch_supabase):
    """Returns the same-category products ordered by descending ingredient
    similarity — the twin formulation first, the partial overlap second, the
    disjoint one last."""
    patch_supabase({"products": [BASE_SERUM, DISJOINT, PARTIAL, TWIN]}, "app.api.products")

    resp = client.get(f"/products/slug/{SERUM_A_ID}")
    assert resp.status_code == 200

    assert [s["name"] for s in similar(resp)] == \
        ["Twin Serum", "Partial Serum", "Disjoint Serum"]


def test_slug_reports_the_similarity_score_it_ranked_by(client, patch_supabase):
    """Returns an ingredient_similarity percentage on each similar product:
    100.0 for an identical ingredient set, 25.0 for one shared of four, and 0.0
    for no overlap."""
    patch_supabase({"products": [BASE_SERUM, DISJOINT, PARTIAL, TWIN]}, "app.api.products")

    scores = {s["name"]: s["ingredient_similarity"] for s in similar(client.get(f"/products/slug/{SERUM_A_ID}"))}
    assert scores == {"Twin Serum": 100.0, "Partial Serum": 25.0, "Disjoint Serum": 0.0}


def test_slug_ranks_rather_than_filters(client, patch_supabase):
    """Returns a product sharing no ingredients at all rather than excluding it,
    so the widget is not left short when the catalogue has few close matches."""
    patch_supabase({"products": [BASE_SERUM, DISJOINT]}, "app.api.products")

    assert [s["name"] for s in similar(client.get(f"/products/slug/{SERUM_A_ID}"))] == ["Disjoint Serum"]


def test_slug_keeps_similar_products_within_the_category(client, patch_supabase):
    """Excludes a product with an identical ingredient set when it belongs to a
    different category, so a cleanser is never offered as an alternative to a
    serum however well its formula matches."""
    patch_supabase({"products": [BASE_SERUM, OTHER_CATEGORY, PARTIAL]}, "app.api.products")

    names = [s["name"] for s in similar(client.get(f"/products/slug/{SERUM_A_ID}"))]
    assert "Same Formula Cleanser" not in names
    assert names == ["Partial Serum"]


def test_slug_never_lists_the_product_itself(client, patch_supabase):
    """Excludes the product being viewed from its own similar-products list."""
    patch_supabase({"products": [BASE_SERUM, TWIN]}, "app.api.products")

    assert SERUM_A_ID not in [s["id"] for s in similar(client.get(f"/products/slug/{SERUM_A_ID}"))]


def test_slug_returns_at_most_four_similar_products(client, patch_supabase):
    """Returns no more than four similar products even when more candidates in
    the category qualify."""
    extras = [product(f"extra-{n}", "Brand E", f"Extra {n}", "Serum", [GLYCERIN])
              for n in range(6)]
    patch_supabase({"products": [BASE_SERUM, *extras]}, "app.api.products")

    assert len(similar(client.get(f"/products/slug/{SERUM_A_ID}"))) == 4


def test_slug_similar_products_carry_the_fields_the_widget_renders(client, patch_supabase):
    """Returns id, brand, name, image_url, price and a generated slug on each
    similar product, so the widget can render and link to it."""
    patch_supabase({"products": [BASE_SERUM, TWIN]}, "app.api.products")

    entry = similar(client.get(f"/products/slug/{SERUM_A_ID}"))[0]
    assert {"id", "brand", "name", "image_url", "price_thb", "price_usd", "slug"} <= set(entry)
    assert entry["slug"] == "brand-t-twin-serum"


# --- Error-message hygiene ---------------------------------------------------

# --- GET /products/{product_id} (BE-DEF-07) ----------------------------------

def test_product_detail_404s_for_an_unknown_id(client, patch_supabase):
    """Returns HTTP 404 for a product id that matches no row, rather than a 500
    or a 200 carrying a null body.

    Regression guard for BE-DEF-07: the handler used .single(), which the real
    client raises PGRST116 on for zero rows, and the generic handler reported
    that as "Failed to fetch product." with status 500."""
    patch_supabase({"products": [], "users": []}, "app.api.products")

    resp = client.get("/products/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Product '00000000-0000-0000-0000-000000000000' not found."


def test_product_detail_returns_the_product_when_it_exists(client, patch_supabase):
    """Returns HTTP 200 and the product enriched with display fields, so the 404
    above is a genuine not-found rather than the endpoint refusing everything."""
    patch_supabase(
        {"products": [product("p1", "CeraVe", "Cleanser", "Cleanser", [])], "users": []},
        "app.api.products",
    )

    resp = client.get("/products/p1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "p1"
    assert "safety_flags" in body


def test_product_detail_survives_an_authenticated_user_with_no_profile_row(
    client, patch_supabase, as_user
):
    """Returns HTTP 200 with no skin match score when the caller's token is valid
    but no users row exists, rather than failing the whole request."""
    from app.core.services.token import get_optional_user_id
    from app.main import app

    app.dependency_overrides[get_optional_user_id] = lambda: "ghost-user"
    try:
        patch_supabase(
            {"products": [product("p1", "CeraVe", "Cleanser", "Cleanser", [])], "users": []},
            "app.api.products",
        )
        resp = client.get("/products/p1")
        assert resp.status_code == 200
        assert resp.json()["skin_match_score"] is None
    finally:
        app.dependency_overrides.pop(get_optional_user_id, None)


def test_internal_errors_are_not_leaked_to_clients(client, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to search products." and no
    trace of the internal exception text, when a database call raises."""
    import app.api.products as products_module

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(products_module.supabase, "table", explode)

    resp = client.get("/products/search")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to search products."
