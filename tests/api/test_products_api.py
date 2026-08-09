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


# --- GET /products/compare ---------------------------------------------------

@pytest.fixture
def conflicting_catalog(patch_supabase):
    """Retinol vs BHA — clashing on both an explicit ingredient pair rule and a
    functional-group rule."""
    store = {
        "products": [
            product(PROD_A_ID, "The Ordinary", "Retinol 0.2% in Squalane", "Serum", [RETINOL]),
            product(PROD_B_ID, "Paula's Choice", "2% BHA Liquid Exfoliant", "Exfoliant", [SALICYLIC]),
        ],
        "conflict_rules": [{
            "ingredient_a_id": "ing-retinol", "ingredient_b_id": "ing-sa",
            "severity": "high", "warning_message": "Use on alternate evenings.",
        }],
        "category_conflict_rules": [{
            "group_a": "Retinoid", "group_b": "Beta Hydroxy Acid (BHA)",
            "severity": "high", "warning_message": "Layering these raises irritation risk.",
        }],
        "users": [],
    }
    return patch_supabase(store, "app.api.products",
                          "app.core.services.compatibility_service")


def test_compare_runs_the_full_three_pass_conflict_analysis(client, conflicting_catalog):
    """REGRESSION: compare only ran the category pass, so it silently
    under-reported conflicts that Shelf and Routine both surfaced."""
    resp = client.get("/products/compare",
                      params={"product_a_id": PROD_A_ID, "product_b_id": PROD_B_ID})
    assert resp.status_code == 200

    alert_types = {c["alert_type"] for c in resp.json()["conflicts"]}
    assert "Chemical Interaction Warning" in alert_types
    assert "Active Routine Clash" in alert_types


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


# --- Error-message hygiene ---------------------------------------------------

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
