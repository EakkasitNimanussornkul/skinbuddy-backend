"""Unit tests for warning de-duplication in app/core/services/compatibility_service.py.

A shelf holds rows, not distinct products: nothing stops the same product being
added twice, and people do it. The comparison maps and the warning list are
built from those rows, so an unguarded build stated one conflict once per row -
three bottles of the same serum produced the same warning three times.

Both functions under test are pure and take plain dicts, so none of this
touches Supabase.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

from app.schemas import WarningAlert
from app.core.services.compatibility_service import (
    _build_comparison_maps,
    _dedupe_warnings,
)


def product(name, *ingredients):
    """Build the normalize_product() shape: {"name": ..., "ingredients": [...]}."""
    return {
        "name": name,
        "ingredients": [
            {"id": ing_id, "name": ing_name, "functional_group": group}
            for ing_id, ing_name, group in ingredients
        ],
    }


RETINOL = ("ing-retinol", "Retinol", "Retinoid")


# --- _build_comparison_maps --------------------------------------------------

def test_the_same_product_held_twice_contributes_one_comparison_pair():
    """Returns one (product, ingredient) pair per functional group. A shelf
    holding two rows of the same product describes one product, and PASS 2
    appends one warning per pair."""
    shelf = [product("Night Serum", RETINOL), product("Night Serum", RETINOL)]

    groups, _ = _build_comparison_maps(shelf)

    assert groups["Retinoid"] == [("Night Serum", "Retinol")]


def test_the_same_product_held_five_times_still_contributes_one_pair():
    """Returns one pair however many rows there are - the count of rows must not
    reach the warning list at all."""
    shelf = [product("Night Serum", RETINOL) for _ in range(5)]

    groups, _ = _build_comparison_maps(shelf)

    assert len(groups["Retinoid"]) == 1


def test_two_different_products_that_clash_each_contribute_a_pair():
    """Returns a pair for each distinct product. Two different products both
    conflicting is two facts the user needs, not a duplicate."""
    shelf = [product("Night Serum", RETINOL), product("Retinal Cream", ("ing-retinal", "Retinal", "Retinoid"))]

    groups, _ = _build_comparison_maps(shelf)

    assert groups["Retinoid"] == [("Night Serum", "Retinol"), ("Retinal Cream", "Retinal")]


def test_one_product_with_two_ingredients_in_a_group_contributes_both():
    """Returns a pair per distinct ingredient. The pair is the unit of
    de-duplication, not the product, so a product is not collapsed with itself
    when it genuinely carries two actives from the same group."""
    shelf = [product("Double Serum", RETINOL, ("ing-retinal", "Retinal", "Retinoid"))]

    groups, _ = _build_comparison_maps(shelf)

    assert len(groups["Retinoid"]) == 2


def test_comparison_pairs_keep_the_order_the_shelf_supplied():
    """Preserves insertion order, so warning order does not vary between runs
    for the same shelf."""
    shelf = [product("B Serum", ("i-b", "B", "Retinoid")), product("A Serum", ("i-a", "A", "Retinoid"))]

    groups, _ = _build_comparison_maps(shelf)

    assert [name for name, _ in groups["Retinoid"]] == ["B Serum", "A Serum"]


def test_an_ingredient_with_no_functional_group_contributes_no_pair():
    """Returns no group entry for an ungrouped ingredient, which PASS 2 has
    nothing to match against."""
    shelf = [product("Plain Lotion", ("ing-water", "Water", None))]

    groups, ingredient_ids = _build_comparison_maps(shelf)

    assert groups == {}
    assert ingredient_ids == {"ing-water": "Plain Lotion"}


# --- _dedupe_warnings --------------------------------------------------------

def alert(message, alert_type="Active Routine Clash", severity="High"):
    return WarningAlert(alert_type=alert_type, severity=severity, message=message)


def test_identical_warnings_are_collapsed_to_one():
    """Returns the warning once. The caller never receives the same sentence
    twice, whatever the passes produced."""
    result = _dedupe_warnings([alert("Conflict with Night Serum"), alert("Conflict with Night Serum")])

    assert len(result) == 1


def test_warnings_naming_different_products_both_survive():
    """Returns both. Warnings that differ only by which product they name are
    different warnings, and suppressing one would hide a real conflict."""
    result = _dedupe_warnings([alert("Conflict with Night Serum"), alert("Conflict with Day Cream")])

    assert len(result) == 2


def test_the_same_message_at_a_different_severity_is_kept():
    """Returns both. Identity is the whole alert - type, severity and message -
    so a rule restated at another severity is not silently dropped."""
    result = _dedupe_warnings([alert("Same text", severity="High"), alert("Same text", severity="Low")])

    assert len(result) == 2


def test_the_same_message_under_a_different_alert_type_is_kept():
    """Returns both. A skin-type conflict and a chemical interaction are
    different findings even where their wording coincides."""
    result = _dedupe_warnings([
        alert("Same text", alert_type="Skin Type Conflict"),
        alert("Same text", alert_type="Chemical Interaction Warning"),
    ])

    assert len(result) == 2


def test_the_first_occurrence_is_the_one_kept():
    """Preserves order and keeps the earliest of a duplicated pair, so the
    surviving warning is the one the passes produced first."""
    result = _dedupe_warnings([alert("first"), alert("second"), alert("first")])

    assert [w.message for w in result] == ["first", "second"]


def test_an_empty_warning_list_stays_empty():
    """Returns an empty list, which analyze() reads as is_safe=True - the
    de-duplication must not manufacture or lose that verdict."""
    assert _dedupe_warnings([]) == []
