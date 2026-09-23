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


def test_search_still_answers_when_the_callers_profile_row_is_missing(client, patch_supabase):
    """Returns HTTP 200 and an unpersonalised result for an authenticated caller
    who has no row in users, rather than failing the whole catalogue search.

    Regression guard: the profile lookup used .single(), which the real client
    raises PGRST116 on for zero rows, and the handler's generic clause reported
    that as a 500. A missing profile means there is no skin type to score
    against, which is what the empty fallback already meant."""
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "user-with-no-row"
    patch_supabase({"products": [CLEANSER], "users": []}, "app.api.products")

    resp = client.get("/products/search")
    assert resp.status_code == 200
    assert resp.json()[0]["skin_match_score"] is None


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


# --- Ranking ------------------------------------------------------------------
#
# search_products assigns rank 1 to a query found in the product name, 2 to one
# found only in the brand, and 3 to everything else, then sorts by it. The
# PostgREST or_ filter that selects the rows is a no-op in the fake, so every
# seeded product comes back and these tests see ordering alone - which is what
# they are about. Selection is covered by the capture_or tests below.

def priced(pid, brand, name, price_thb):
    row = product(pid, brand, name, "Serum", [GLYCERIN])
    row["price_thb"] = price_thb
    return row


def names_from(resp):
    return [p["name"] for p in resp.json()]


def test_search_ranks_a_name_match_above_a_brand_match_above_neither(client, patch_supabase):
    """Returns the product whose name contains the query first, the one whose
    brand contains it second, and the one matching neither last.

    Seeded in the opposite order, so passing requires the sort to have reordered
    them rather than the store to have been listed conveniently."""
    patch_supabase({"products": [
        priced("p-none", "The Ordinary", "Niacinamide 10% + Zinc 1%", 500),
        priced("p-brand", "CeraVe", "Moisturising Lotion", 500),
        priced("p-name", "Generic Labs", "CeraVe Style Cleanser", 500),
    ]}, "app.api.products")

    resp = client.get("/products/search", params={"q": "cerave"})
    assert resp.status_code == 200
    assert names_from(resp) == [
        "CeraVe Style Cleanser",      # rank 1, name
        "Moisturising Lotion",        # rank 2, brand
        "Niacinamide 10% + Zinc 1%",  # rank 3, neither
    ]


def test_search_ranks_a_product_matching_both_fields_by_its_name_match(client, patch_supabase):
    """Returns a product whose name and brand both contain the query ahead of
    one matching on brand alone.

    The two rank branches are if/elif on the same query, so a product matching
    both takes the name rank. Without this, swapping them to elif/if would go
    unnoticed."""
    patch_supabase({"products": [
        priced("p-brand", "CeraVe", "Moisturising Lotion", 500),
        priced("p-both", "CeraVe", "CeraVe Foaming Cleanser", 500),
    ]}, "app.api.products")

    resp = client.get("/products/search", params={"q": "cerave"})
    assert names_from(resp) == ["CeraVe Foaming Cleanser", "Moisturising Lotion"]


# --- Price bounds -------------------------------------------------------------
#
# Unlike or_, gte and lte are implemented in the fake (tests/conftest.py), so
# these assert which products come back rather than which builder calls were
# made.

PRICE_CATALOGUE = [
    priced("p-cheap", "Brand A", "Budget Cleanser", 250),
    priced("p-mid", "Brand B", "Mid Serum", 600),
    priced("p-dear", "Brand C", "Premium Cream", 1200),
]


def test_search_excludes_products_below_min_price(client, patch_supabase):
    """Returns only products priced at or above min_price, with the bound
    inclusive."""
    patch_supabase({"products": PRICE_CATALOGUE}, "app.api.products")

    resp = client.get("/products/search", params={"min_price": 600})
    assert resp.status_code == 200
    assert sorted(names_from(resp)) == ["Mid Serum", "Premium Cream"]


def test_search_excludes_products_above_max_price(client, patch_supabase):
    """Returns only products priced at or below max_price, with the bound
    inclusive."""
    patch_supabase({"products": PRICE_CATALOGUE}, "app.api.products")

    resp = client.get("/products/search", params={"max_price": 600})
    assert resp.status_code == 200
    assert sorted(names_from(resp)) == ["Budget Cleanser", "Mid Serum"]


def test_search_applies_both_price_bounds_together(client, patch_supabase):
    """Returns only the product inside the band when min_price and max_price are
    both supplied, so the two filters combine rather than one replacing the
    other."""
    patch_supabase({"products": PRICE_CATALOGUE}, "app.api.products")

    resp = client.get("/products/search", params={"min_price": 400, "max_price": 900})
    assert names_from(resp) == ["Mid Serum"]


def test_search_returns_an_empty_list_when_no_product_is_in_the_price_band(
        client, patch_supabase):
    """Returns HTTP 200 and an empty list, not a 404, when the bounds exclude
    every product."""
    patch_supabase({"products": PRICE_CATALOGUE}, "app.api.products")

    resp = client.get("/products/search", params={"min_price": 2000})
    assert resp.status_code == 200
    assert resp.json() == []


def test_search_without_price_bounds_returns_every_product(client, patch_supabase):
    """Returns all three products when neither bound is supplied, confirming the
    filters are applied only when the caller asks for them.

    Positive control for the four cases above, which would all pass against an
    endpoint that returned nothing at all."""
    patch_supabase({"products": PRICE_CATALOGUE}, "app.api.products")

    resp = client.get("/products/search")
    assert len(resp.json()) == 3


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


ALCOHOL = {"id": "ing-alc", "name": "Alcohol Denat.", "functional_group": "Solvent"}


@pytest.fixture
def overlapping_catalog(patch_supabase):
    """Two products sharing one ingredient of three distinct ones, so similarity
    is neither 0 nor 100."""
    store = _compare_store([], [])
    store["products"] = [
        product(PROD_A_ID, "The Ordinary", "Retinol 0.2% in Squalane", "Serum",
                [RETINOL, GLYCERIN]),
        product(PROD_B_ID, "Paula's Choice", "2% BHA Liquid Exfoliant", "Exfoliant",
                [SALICYLIC, GLYCERIN]),
    ]
    return patch_supabase(store, "app.api.products",
                          "app.core.services.compatibility_service")


def test_compare_reports_a_partial_overlap_with_the_shared_ingredient(
        client, overlapping_catalog):
    """Returns similarity_score=33.3 and a shared_ingredients list naming the one
    ingredient both products contain.

    Jaccard over the ingredient id sets: one shared of three distinct. The
    disjoint case below covers 0.0, but a score that is neither 0 nor 100 is the
    one that shows the ratio is computed rather than a membership flag."""
    body = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID}).json()

    assert body["similarity_score"] == 33.3
    assert [i["name"] for i in body["shared_ingredients"]] == ["Glycerin"]


@pytest.fixture
def scored_catalog(patch_supabase):
    """Two products a DSPT profile scores differently: a humectant earns +10, a
    volatile alcohol costs -20."""
    store = _compare_store([], [])
    store["products"] = [
        product(PROD_A_ID, "CeraVe", "Hydrating Lotion", "Moisturizer", [GLYCERIN]),
        product(PROD_B_ID, "Brand B", "Astringent Toner", "Toner", [ALCOHOL]),
    ]
    store["users"] = [{"id": "user-1", "skin_type": "DSPT"}]
    return patch_supabase(store, "app.api.products",
                          "app.core.services.compatibility_service")


def test_compare_scores_each_product_against_the_callers_skin_type(client, scored_catalog):
    """Returns skin_match_score=85 for the humectant product and 55 for the
    alcohol one, for an authenticated DSPT caller.

    Both are computed from the same profile but from each product's own
    ingredients, so the two scores must differ. Asserting only that they are
    non-null would pass against a handler that scored product A twice."""
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "user-1"

    body = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID}).json()

    assert body["product_a"]["skin_match_score"] == 85   # 75 base + 10 humectant
    assert body["product_b"]["skin_match_score"] == 55   # 75 base - 20 drying alcohol


# Shaped as the live catalogue shapes bad_for, e.g. "Extremely Dry Skin (D)";
# the skin-type pass matches on the parenthesised letter.
IRRITANT = {"id": "ing-irritant", "name": "Denatured Alcohol",
            "functional_group": "Solvent", "bad_for": "Extremely Dry Skin (D)"}


@pytest.fixture
def one_sided_irritant_catalog(patch_supabase):
    """A DSPT caller, an inert product A, and a product B carrying an ingredient
    their skin type reacts to. No conflict rule of any kind, so the skin-type
    pass is the only thing that can produce a warning."""
    store = _compare_store([], [])
    store["products"] = [
        product(PROD_A_ID, "CeraVe", "Hydrating Lotion", "Moisturizer", [GLYCERIN]),
        product(PROD_B_ID, "Brand B", "Astringent Toner", "Toner", [IRRITANT]),
    ]
    store["users"] = [{"id": "user-1", "skin_type": "DSPT"}]
    return patch_supabase(store, "app.api.products",
                          "app.core.services.compatibility_service")


def test_compare_reports_a_skin_type_conflict_carried_by_product_b_alone(
        client, one_sided_irritant_catalog):
    """Returns a Skin Type Conflict naming product B's ingredient when only
    product B carries something the caller's skin type reacts to.

    The skin-type pass walks the *target* product's own ingredients, so
    analyze(A, [B]) never examines B's. The handler makes a second call,
    analyze(B, []), for exactly this reason, and its warnings are appended to
    the same conflicts list. Without that call the comparison would report
    whatever product A triggers and stay silent about product B, so a user
    would be told a product is fine because the one beside it is."""
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "user-1"

    conflicts = client.get("/products/compare",
                           params={"product_a_id": PROD_A_ID,
                                   "product_b_id": PROD_B_ID}).json()["conflicts"]

    assert [c["alert_type"] for c in conflicts] == ["Skin Type Conflict"]
    assert "Denatured Alcohol" in conflicts[0]["message"]


PEPTIDES = [{"id": f"ing-pep-{n}", "name": name, "functional_group": "Peptide"}
            for n, name in enumerate(["Multi-Peptide Complex", "Acetyl Hexapeptide-8",
                                      "Pentapeptide-18"])]
BHA_VS_PEPTIDE_RULE = {
    "group_a": "Beta Hydroxy Acid (BHA)", "group_b": "Peptide",
    "severity": "medium", "warning_message": "Low-pH BHA exfoliants can break down peptides.",
}


def test_compare_reports_every_clash_with_product_b_as_one_conflict(client, patch_supabase):
    """Returns a single conflict naming product B, with all three of its
    clashing pairs in details, when product A's acid clashes with three of
    product B's peptides."""
    store = _compare_store([], [BHA_VS_PEPTIDE_RULE])
    store["products"] = [
        product(PROD_A_ID, "Paula's Choice", "2% BHA Liquid Exfoliant", "Exfoliant", [SALICYLIC]),
        product(PROD_B_ID, "Brand B", "Peptide Serum", "Serum", PEPTIDES),
    ]
    patch_supabase(store, "app.api.products", "app.core.services.compatibility_service")

    conflicts = client.get("/products/compare",
                           params={"product_a_id": PROD_A_ID,
                                   "product_b_id": PROD_B_ID}).json()["conflicts"]

    [conflict] = conflicts
    assert conflict["conflicting_product"] == "Peptide Serum"
    assert len(conflict["details"]) == 3


def test_compare_reports_an_internal_failure_as_a_400(client, patch_supabase, monkeypatch):
    """Returns HTTP 400 with the detail "Failed to compare products." when the
    handler raises something other than an HTTPException.

    Distinct from the 404 below, which is a resolved outcome rather than a
    failure. The handler re-raises HTTPException ahead of its generic clause, so
    this asserts the generic clause is still reachable by everything else."""
    import app.api.products as products_module

    patch_supabase({"products": [], "users": []}, "app.api.products",
                   "app.core.services.compatibility_service")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(products_module.supabase, "table", explode)

    resp = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Failed to compare products."
    assert "hunter2" not in resp.text


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


# --- GET /products/slug/{slug} — the resolved product itself -----------------
#
# The cases above all assert on similar_products. These assert on the target
# product the route resolved: its own display fields, and the two failure
# outcomes.
#
# SLUG_TARGET carries a real `slug` value, unlike the product() default of None.
# That matters: resolve_product_record tries an indexed exact-slug lookup before
# falling back to a full scan, and with every seeded product carrying slug=None
# that fast path never returned a hit under test.

SLUG_TARGET = product("slug-target-id", "CeraVe", "Hydrating Cleanser", "Cleanser",
                      [GLYCERIN, PHENOXY])
SLUG_TARGET["slug"] = "cerave-hydrating-cleanser"

TARGET_SLUG = "cerave-hydrating-cleanser"


def test_slug_omits_personalisation_for_an_anonymous_caller(client, patch_supabase):
    """Returns skin_match_score=None with empty match and caution reasons, and a
    fully populated safety_flags object, for an unauthenticated caller.

    safety_flags is computed from the product's ingredients alone, so it is
    present with or without a caller; the score is not."""
    patch_supabase({"products": [SLUG_TARGET], "users": []}, "app.api.products")

    body = client.get(f"/products/slug/{TARGET_SLUG}").json()

    assert body["skin_match_score"] is None
    assert body["match_reasons"] == []
    assert body["caution_reasons"] == []
    assert body["safety_flags"]["paraben_free"] is True


def test_slug_scores_the_resolved_product_for_an_authenticated_caller(client, patch_supabase):
    """Returns skin_match_score=85 and a barrier-repair match reason for a DSPT
    caller, applying the +10 humectant bonus on the Dry axis to the resolved
    product's own ingredients.

    The personalising branch of this route - the users lookup feeding
    compute_product_display_fields - was previously never taken by any test."""
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: "user-1"
    patch_supabase(
        {"products": [SLUG_TARGET], "users": [{"id": "user-1", "skin_type": "DSPT"}]},
        "app.api.products",
    )

    body = client.get(f"/products/slug/{TARGET_SLUG}").json()

    assert body["skin_match_score"] == 85   # 75 base + 10 humectant
    assert any("Barrier-repair" in r for r in body["match_reasons"])


def test_slug_404s_for_an_identifier_that_resolves_to_nothing(client, patch_supabase):
    """Returns HTTP 404 naming the slug it could not resolve, rather than a 200
    carrying a null body.

    This route has its own 404, separate from the one on GET /products/{id}."""
    patch_supabase({"products": [SLUG_TARGET], "users": []}, "app.api.products")

    resp = client.get("/products/slug/no-such-product")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Product matching slug 'no-such-product' not found."


def test_slug_reports_an_internal_failure_as_a_500(client, patch_supabase, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to fetch product." and no
    trace of the internal exception text, when the query raises.

    The handler re-raises HTTPException ahead of its generic clause, so this
    also confirms the generic clause is still reachable by everything else."""
    import app.api.products as products_module

    patch_supabase({"products": [SLUG_TARGET], "users": []}, "app.api.products")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(products_module.supabase, "table", explode)

    resp = client.get(f"/products/slug/{TARGET_SLUG}")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to fetch product."
    assert "hunter2" not in resp.text


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
