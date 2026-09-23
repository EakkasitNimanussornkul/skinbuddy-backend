"""Tests for _skin_type_reasons (app/core/services/compatibility_service.py).

The skin-type pass flags an ingredient when its bad_for carries a "(<letter>)"
marker matching a letter of the caller's Baumann code. This helper says why:
one reason per matched trait, explained and graded by the ingredient_concerns
row whose target_profile carries the same marker.

Called directly with plain dicts in the joined shape analyze() reads:
{name, bad_for, ingredient_concerns:[{concern_title, concern_description,
target_profile, severity}]}.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.core.services.compatibility_service import _skin_type_reasons


def ingredient(bad_for, *concerns):
    return {"name": "Test Ingredient", "bad_for": bad_for, "ingredient_concerns": list(concerns)}


def concern(target_profile, severity, title="A concern"):
    return {"concern_title": title, "concern_description": "Because.",
            "target_profile": target_profile, "severity": severity}


def test_no_matching_trait_gives_no_reasons():
    """Returns an empty list when bad_for flags no trait of the caller's code,
    which is what keeps the ingredient from raising an alert at all."""
    assert _skin_type_reasons(ingredient("Extremely Dry Skin (D)"), "ORNT") == []


def test_an_ingredient_with_no_bad_for_gives_no_reasons():
    """Returns an empty list for an ingredient whose bad_for is missing."""
    assert _skin_type_reasons({"name": "Water"}, "DSPT") == []


def test_the_trait_is_the_bad_for_entry_holding_the_marker():
    """Returns the one bad_for entry that carries the matched marker as the
    trait, not the whole bad_for list."""
    [reason] = _skin_type_reasons(ingredient("Active Acne, Highly Sensitive Skin (S)"), "DSPT")
    assert reason.trait == "Highly Sensitive Skin (S)"


@pytest.mark.parametrize("concern_grade, warning_grade", [
    ("High", "High"), ("Moderate", "Medium"), ("Low", "Low"), ("moderate", "Medium"),
])
def test_a_concern_grade_is_put_on_the_warnings_scale(concern_grade, warning_grade):
    """Returns the concern's severity as High, Medium or Low: the concerns
    table's Moderate becomes Medium, the grade the rule tables and the frontend
    use, whatever its case."""
    [reason] = _skin_type_reasons(
        ingredient("Extremely Dry Skin (D)", concern("Extremely Dry Skin (D)", concern_grade)), "DSPT")
    assert reason.severity == warning_grade


def test_an_unrecognised_concern_grade_is_kept_title_cased():
    """Returns an unrecognised concern severity title-cased rather than replaced
    by a guess, as rule severities are."""
    [reason] = _skin_type_reasons(
        ingredient("Extremely Dry Skin (D)", concern("Extremely Dry Skin (D)", "critical")), "DSPT")
    assert reason.severity == "Critical"


def test_a_concern_for_a_different_profile_does_not_explain_the_trait():
    """Returns a reason with no title, graded High, when the ingredient's only
    concern targets a profile without the matched marker. A concern about
    allergy-prone skin does not explain a sensitive-skin flag."""
    [reason] = _skin_type_reasons(
        ingredient("Allergy-Prone Skin, Highly Sensitive Skin (S)",
                   concern("Allergy-Prone Skin", "Moderate", title="Contact Allergy")),
        "DSPT")
    assert reason.title is None
    assert reason.severity == "High"


def test_the_most_severe_of_several_concerns_for_one_trait_is_used():
    """Returns the explanation of the most severe concern when several carry
    the matched marker, since that concern sets the grade."""
    [reason] = _skin_type_reasons(
        ingredient("Sensitive Skin (S)",
                   concern("Sensitive Skin (S)", "Low", title="Mild Tingle"),
                   concern("Highly Sensitive Skin (S)", "High", title="Stinging & Burn"),
                   concern("Sensitive Skin (S)", "Moderate", title="Redness")),
        "DSPT")
    assert (reason.title, reason.severity) == ("Stinging & Burn", "High")


def test_each_matched_trait_gets_its_own_reason_in_code_order():
    """Returns one reason per matched trait, ordered as the letters of the
    Baumann code, each explained by its own concern or by none.

    OSNT, with bad_for listing (N) before (S): the code puts S second and N
    third, which is neither bad_for's order nor alphabetical, so only reading
    the code's own order passes."""
    reasons = _skin_type_reasons(
        ingredient("Non-Pigmented Skin (N), Highly Sensitive Skin (S)",
                   concern("Highly Sensitive Skin (S)", "High", title="Sting")),
        "OSNT")
    assert [(r.trait, r.title) for r in reasons] == [
        ("Highly Sensitive Skin (S)", "Sting"),
        ("Non-Pigmented Skin (N)", None),
    ]
