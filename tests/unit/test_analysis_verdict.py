"""Tests for what is_safe actually asserts (compatibility_service.analyze()).

is_safe is the verdict every caller of the safety feature acts on, and the
frontend is obliged to believe it: evaluateSafety (src/api/safety.ts) reads
is_safe=true as "cleared" and shows a green result. So the question these tests
ask is not "were warnings produced" but "was the product assessed at all".

Supabase is replaced with the in-memory fake (tests/conftest.py); the comparison
set is passed to analyze() directly, so no shelf rows are needed.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

from app.core.services import compatibility_service

TARGET_ID = "prod-retinol"
USER_ID = "user-1"

RETINOL_TARGET = {
    "id": TARGET_ID,
    "name": "Retinol 0.2% in Squalane",
    "product_ingredients": [
        {"ingredients": {"id": "ing-retinol", "name": "Retinol",
                         "functional_group": "Retinoid", "bad_for": None}}
    ],
}

NO_INGREDIENT_TARGET = {
    "id": "prod-empty",
    "name": "Mystery Cream",
    "product_ingredients": [],
}

BHA_COMPARISON = {
    "name": "2% BHA Liquid",
    "ingredients": [{"id": "ing-sa", "name": "Salicylic Acid",
                     "functional_group": "Beta Hydroxy Acid (BHA)"}],
}


def store(products):
    return {
        "products": products,
        "users": [{"id": USER_ID, "skin_type": "ORNT"}],
        "conflict_rules": [],
        "category_conflict_rules": [],
    }


# --- FE-DEF-28: is_safe must mean "assessed and clear" -----------------------

def test_a_product_with_no_ingredients_is_not_reported_safe(patch_supabase):
    """Returns is_safe=False with an empty warnings list for a product that has
    no ingredient rows, because nothing about it was ever examined.

    Regression guard for FE-DEF-28: is_safe was "no warnings were produced", and
    a product with nothing to check produces none, so it was presented to the
    user as assessed and clear."""
    patch_supabase(store([NO_INGREDIENT_TARGET]), "app.core.services.compatibility_service")

    result = compatibility_service.analyze("prod-empty", USER_ID, [BHA_COMPARISON])

    assert result.warnings == []
    assert result.is_safe is False


def test_a_product_that_does_not_exist_is_not_reported_safe(patch_supabase):
    """Returns is_safe=False with an empty warnings list when the product id
    matches no catalogue row.

    Shelf and compare both 404 before reaching here, but the routine pre-check
    (app/api/routine.py) does not, so an unknown id still arrives."""
    patch_supabase(store([]), "app.core.services.compatibility_service")

    result = compatibility_service.analyze("prod-does-not-exist", USER_ID, [BHA_COMPARISON])

    assert result.warnings == []
    assert result.is_safe is False


def test_an_unassessable_product_is_not_reported_safe_with_an_empty_comparison(patch_supabase):
    """Returns is_safe=False on the early return taken when there is nothing to
    compare against, so the shorter path reaches the same verdict as the long
    one rather than clearing the product on its way out."""
    patch_supabase(store([NO_INGREDIENT_TARGET]), "app.core.services.compatibility_service")

    assert compatibility_service.analyze("prod-empty", USER_ID, []).is_safe is False


def test_an_assessed_product_with_an_empty_comparison_is_still_reported_safe(patch_supabase):
    """Returns is_safe=True for a product that has ingredients and nothing to
    compare against, which is a real clear result and not a missing assessment.

    The distinction FE-DEF-28 turns on: the condition is whether the TARGET was
    assessable, never whether the comparison set was empty. GET /products/compare
    calls analyze(product_b, user_id, []) with an empty comparison on purpose."""
    patch_supabase(store([RETINOL_TARGET]), "app.core.services.compatibility_service")

    result = compatibility_service.analyze(TARGET_ID, USER_ID, [])

    assert result.warnings == []
    assert result.is_safe is True


def test_an_assessed_product_with_no_conflicts_is_still_reported_safe(patch_supabase):
    """Returns is_safe=True for a product that was compared against a shelf and
    genuinely clashed with nothing, so rejecting unassessed products cannot be
    satisfied by refusing to clear anything."""
    patch_supabase(store([RETINOL_TARGET]), "app.core.services.compatibility_service")

    result = compatibility_service.analyze(TARGET_ID, USER_ID, [BHA_COMPARISON])

    assert result.warnings == []
    assert result.is_safe is True
