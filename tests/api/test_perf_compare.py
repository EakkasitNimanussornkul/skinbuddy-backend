"""GET /products/compare reads each thing once, and analyze() takes what a caller
already holds.

Compare used to read the user's skin type three times and each product's tree up to
twice again (9 database calls, audit 2026-10-09 finding 2). It now passes the two
resolved products and the skin type into analyze() through optional parameters.
Every other caller of analyze() (shelf, routine) leaves them out and is unchanged.
"""

import collections
import copy

import pytest

from app.core.services import compatibility_service as svc
from app.core.services.token import get_optional_user_id

A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

RETINOL = {"id": "ing-retinol", "name": "Retinol", "functional_group": "Retinoid"}
SALICYLIC = {"id": "ing-sa", "name": "Salicylic Acid", "functional_group": "Beta Hydroxy Acid (BHA)"}
ALCOHOL = {"id": "ing-alc", "name": "Alcohol Denat.", "functional_group": "Solvent", "bad_for": "Sensitive Skin (S)",
           "ingredient_concerns": [{"target_profile": "Sensitive Skin (S)", "severity": "High",
                                    "concern_title": "Strips oil", "concern_sources": []}]}
RULE = {"ingredient_a_id": "ing-retinol", "ingredient_b_id": "ing-sa", "severity": "high",
        "warning_message": "Alternate evenings.", "conflict_rule_sources": []}
CATEGORY_RULE = {"group_a": "Retinoid", "group_b": "Beta Hydroxy Acid (BHA)", "severity": "high",
                 "warning_message": "Raises irritation.", "category_rule_sources": []}


def product(pid, name, ingredients):
    return {"id": pid, "brand": "Brand", "name": name, "category": "Serum", "slug": None,
            "description": None, "price_thb": 500, "price_usd": 15, "image_url": None,
            "product_sources": [], "product_ingredients": [{"ingredients": i} for i in ingredients]}


def store(skin_type="DSPT"):
    return {
        "products": [product(A_ID, "Retinol Serum", [RETINOL, ALCOHOL]), product(B_ID, "BHA Liquid", [SALICYLIC])],
        "users": [{"id": "u-1", "skin_type": skin_type}],
        "conflict_rules": [copy.deepcopy(RULE)],
        "category_conflict_rules": [copy.deepcopy(CATEGORY_RULE)],
    }


def counting(fake):
    reads = collections.Counter()
    original = fake.table

    def table(name):
        reads[name] += 1
        return original(name)

    fake.table = table
    return reads


MODULES = ("app.api.products", "app.core.services.compatibility_service")


def compare(client, user=None):
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: user
    resp = client.get("/products/compare", params={"product_a_id": A_ID, "product_b_id": B_ID})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the route ---------------------------------------------------------------------

def test_compare_reads_the_user_once_each_product_once_and_each_rule_table_once(client, patch_supabase):
    """Reads the users table once, the products table twice (product A, product B) and
    each of conflict_rules and category_conflict_rules once for a signed-in compare: 6
    reads, where it used to make 9."""
    fake = patch_supabase(store(), *MODULES)
    reads = counting(fake)

    compare(client, "u-1")

    assert dict(reads) == {"users": 1, "products": 2, "conflict_rules": 1, "category_conflict_rules": 1}


def test_a_second_compare_does_not_read_the_rule_tables_again(client, patch_supabase):
    """Reads only the users table once and the products table twice for a second compare
    within 60 seconds, the rule tables coming from the cache."""
    fake = patch_supabase(store(), *MODULES)
    compare(client, "u-1")
    reads = counting(fake)

    compare(client, "u-1")

    assert dict(reads) == {"users": 1, "products": 2}


def test_an_anonymous_compare_reads_no_user(client, patch_supabase):
    """Reads the users table zero times for a caller who is not signed in."""
    fake = patch_supabase(store(), *MODULES)
    reads = counting(fake)

    compare(client, None)

    assert reads["users"] == 0 and reads["products"] == 2


def test_compare_still_reports_the_users_skin_type_conflicts_and_the_pair_clash(client, patch_supabase):
    """Returns, for a user whose stored skin type is DSPT, a Skin Type Conflict for
    Alcohol Denat. (product A's ingredient flagged for sensitive skin) and the
    Retinol/Salicylic Acid clash, both in conflicts; the skin type is the one stored,
    not an empty one."""
    patch_supabase(store("DSPT"), *MODULES)

    conflicts = compare(client, "u-1")["conflicts"]

    alerts = {c["alert_type"] for c in conflicts}
    assert "Skin Type Conflict" in alerts
    assert "Chemical Interaction Warning" in alerts or "Active Routine Clash" in alerts


def test_compare_for_a_user_without_a_clashing_skin_type_has_no_skin_type_conflict(client, patch_supabase):
    """Returns no Skin Type Conflict for a user whose stored skin type is DRNT (not
    sensitive), while the pair clash is still reported, so the skin type reaching the
    analysis is the user's own and not a fixed one."""
    patch_supabase(store("DRNT"), *MODULES)

    conflicts = compare(client, "u-1")["conflicts"]

    alerts = {c["alert_type"] for c in conflicts}
    assert "Skin Type Conflict" not in alerts
    assert alerts


# --- analyze() ---------------------------------------------------------------------

def run_analyze(**optional):
    other = svc.normalize_product(store()["products"][1])
    return svc.analyze(A_ID, "u-1", [other], **optional)


def test_analyze_with_no_optional_arguments_reads_the_user_and_the_target_itself(patch_supabase):
    """Reads the users table once and the products table once when called as the shelf
    and routine code calls it, with three arguments."""
    fake = patch_supabase(store(), "app.core.services.compatibility_service")
    reads = counting(fake)

    result = run_analyze()

    assert reads["users"] == 1 and reads["products"] == 1
    assert {w.alert_type for w in result.warnings} >= {"Skin Type Conflict"}


def test_analyze_given_the_target_and_skin_type_reads_neither(patch_supabase):
    """Reads neither the users table nor the products table when handed the target row
    and the skin type, and returns the same result as the call that reads them, for
    DSPT, DRNT, no skin type and an empty one."""
    for skin in ("DSPT", "DRNT", None, ""):
        fake = patch_supabase(store(skin), "app.core.services.compatibility_service")
        target = copy.deepcopy(fake.store["products"][0])
        reads = counting(fake)

        passed = run_analyze(user_skin_type=skin, target_data=target)
        assert reads["users"] == 0 and reads["products"] == 0

        fetched = run_analyze()
        assert passed == fetched, skin


def test_a_skin_type_of_none_is_an_answer_not_a_request_to_read(patch_supabase):
    """Reads no users row when user_skin_type=None is passed for a signed-in user, and
    reports no Skin Type Conflict although the stored skin type would produce one."""
    fake = patch_supabase(store("DSPT"), "app.core.services.compatibility_service")
    reads = counting(fake)

    result = run_analyze(user_skin_type=None)

    assert reads["users"] == 0
    assert "Skin Type Conflict" not in {w.alert_type for w in result.warnings}


def test_analyze_uses_the_target_it_is_given_not_the_stored_one(patch_supabase):
    """Reports the clash of the product handed in as target_data (Retinol), not of the
    stored product with that id (which has no actives), so the argument is what the
    analysis ran on."""
    data = store("DRNT")
    data["products"][0] = product(A_ID, "Plain", [{"id": "ing-water", "name": "Water", "functional_group": "Solvent"}])
    patch_supabase(data, "app.core.services.compatibility_service")
    handed_in = product(A_ID, "Retinol Serum", [RETINOL])

    stored = run_analyze()
    given = run_analyze(target_data=handed_in)

    assert stored.warnings == []
    assert any("Retinol" in w.message for w in given.warnings)


def test_analyze_does_not_change_the_target_it_is_given(patch_supabase):
    """Leaves the target row and the comparison list equal to copies taken before the
    call, so compare's response carries the product exactly as it was resolved."""
    patch_supabase(store(), "app.core.services.compatibility_service")
    target = copy.deepcopy(store()["products"][0])
    snapshot = copy.deepcopy(target)

    run_analyze(user_skin_type="DSPT", target_data=target)

    assert target == snapshot
