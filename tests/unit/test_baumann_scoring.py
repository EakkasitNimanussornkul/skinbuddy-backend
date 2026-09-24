"""Unit tests for compute_baumann_compatibility (app/api/products.py).

The skin match score: how well a product's ingredients suit a Baumann code, as
a percentage. Each ingredient counts as helpful when its good_for describes a
trait of the code, and as a concern when its bad_for flags one, weighted by
its ingredient_concerns grade (High 1.0, Medium 0.6, Low 0.3). The score is the
plain share helpful / (helpful + weighted concerns) x 100, to one decimal
place, and None when no ingredient says anything about the code. The working
comes back beside it as a breakdown.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.api.products import GOOD_FOR_TRAITS, NEUTRAL_GOOD_FOR, compute_baumann_compatibility
from app.core.services.ingredient_dictionary import CLINICAL_DICTIONARY, parse_ingredient_data


def ing(name, good_for=None, bad_for=None, *concerns):
    return {"name": name, "good_for": good_for, "bad_for": bad_for,
            "ingredient_concerns": list(concerns)}


def concern(target_profile, severity, title="A concern"):
    return {"concern_title": title, "concern_description": "Because.",
            "target_profile": target_profile, "severity": severity}


def score(code, *ingredients):
    return compute_baumann_compatibility(code, list(ingredients))["score"]


def breakdown(code, *ingredients):
    return compute_baumann_compatibility(code, list(ingredients))["breakdown"]


GLYCERIN = ing("Glycerin", "Dry Skin, Dehydrated Skin")
WATER = ing("Water", "All Skin Types")


def flagged_for_dry(grade):
    return ing("Salicylic Acid", None, "Extremely Dry Skin (D)",
               concern("Extremely Dry Skin (D)", grade))


# --- Guard clause ------------------------------------------------------------

# "dspt", "XXXX" and "DSPTX" are the cases a length-based guard lets through:
# four or more characters, but not a Baumann code. Every invalid code must get
# the same answer as a missing one. (FE-DEF-10)
@pytest.mark.parametrize(
    "invalid_code", ["", None, "DS", "X", "dspt", "XXXX", "DSPTX", "1234"]
)
def test_invalid_skin_type_code_returns_no_score(invalid_code):
    """Returns no score, no breakdown, and neither match nor caution reasons,
    when the skin-type code is not one of the sixteen valid Baumann codes."""
    result = compute_baumann_compatibility(invalid_code, [GLYCERIN])
    assert result == {"score": None, "breakdown": None, "match_reasons": [], "caution_reasons": []}


def test_valid_skin_type_code_is_still_scored():
    """Returns a numeric score for a valid Baumann code and an ingredient that
    suits it, so that rejecting invalid codes cannot be satisfied by refusing to
    score anything."""
    assert isinstance(score("DSPT", GLYCERIN), float)


# --- No evidence -------------------------------------------------------------

def test_a_product_saying_nothing_about_the_code_is_not_scored():
    """Returns no score and no reasons, but a breakdown of 0 considered out of
    the product's ingredients, when no ingredient suits or is flagged for any
    trait of the code, rather than a number that would only be a guess."""
    result = compute_baumann_compatibility("DSPT", [WATER, ing("Mystery Compound")])
    assert result["score"] is None
    assert (result["match_reasons"], result["caution_reasons"]) == ([], [])
    assert result["breakdown"]["considered"] == 0
    assert result["breakdown"]["total_ingredients"] == 2


def test_good_for_a_trait_outside_the_code_does_not_count():
    """Returns no score for an oily-skin code and a product whose only described
    benefit is for dry skin."""
    assert score("OSPT", GLYCERIN) is None


def test_all_skin_types_counts_for_no_one():
    """Returns no score for a product of ingredients tagged "All Skin Types",
    which describes no trait, so it cannot lift every product for every user."""
    assert score("DSPT", WATER, WATER) is None


# --- The formula -------------------------------------------------------------

def test_only_helpful_ingredients_score_100():
    """Returns 100.0 when every ingredient that says anything about the code
    suits it: the plain share, 1 / (1 + 0), not smoothed toward the middle."""
    assert score("DSPT", GLYCERIN) == 100.0


def test_only_concerns_score_0():
    """Returns 0.0 when every ingredient that says anything about the code is
    flagged for it, whatever the grade."""
    assert score("DSPT", flagged_for_dry("Low")) == 0.0


@pytest.mark.parametrize("grade, expected", [("High", 50.0), ("Moderate", 62.5), ("Low", 76.9)])
def test_a_concern_is_weighed_by_its_grade(grade, expected):
    """Returns 50.0, 62.5 or 76.9 for one helpful ingredient against one
    concern graded High, Moderate or Low: 1 / (1 + weight), weights 1.0, 0.6
    and 0.3. A Low concern costs less than a High one."""
    assert score("DSPT", GLYCERIN, flagged_for_dry(grade)) == expected


def test_an_unrecognised_concern_grade_weighs_as_high():
    """Returns 50.0, the High weight, for one helpful ingredient against a
    concern whose grade the scale does not know."""
    assert score("DSPT", GLYCERIN, flagged_for_dry("Critical")) == 50.0


def test_an_ingredient_flagged_for_two_traits_weighs_its_worst_grade():
    """Returns 50.0, the High weight, for one helpful ingredient against one
    flagged Moderate for dry skin and High for sensitive skin, scored for DSPT:
    it counts once, at its more serious grade, not at whichever trait comes
    first in the code."""
    alcohol = ing("Alcohol Denat.", None, "Extremely Dry Skin (D), Highly Sensitive Skin (S)",
                  concern("Extremely Dry Skin (D)", "Moderate"),
                  concern("Highly Sensitive Skin (S)", "High"))
    assert score("DSPT", GLYCERIN, alcohol) == 50.0


def test_an_ingredient_can_suit_one_trait_and_be_flagged_for_another():
    """Returns 62.5 for niacinamide alone scored for OSPT: it suits oily skin
    and is flagged Moderate for sensitive skin, so it counts on both sides:
    1 / (1 + 0.6)."""
    niacinamide = ing("Niacinamide", "Oily Skin, Acne-Prone", "Highly Sensitive Skin (S)",
                      concern("Highly Sensitive Skin (S)", "Moderate"))
    assert score("OSPT", niacinamide) == 62.5


def test_the_score_is_not_confined_to_steps_of_five():
    """Returns 61.0 for five helpful ingredients against two High and two
    Moderate concerns, 5 / (5 + 3.2): a value the old scorer, which moved only
    in fixed steps, could not produce."""
    flagged_high = ing("Retinol", None, "Sensitive Skin (S)", concern("Sensitive Skin (S)", "High"))
    flagged_moderate = ing("Niacinamide", None, "Highly Sensitive Skin (S)",
                           concern("Highly Sensitive Skin (S)", "Moderate"))
    result = score("DSPT", *[GLYCERIN] * 5, flagged_high, flagged_high, flagged_moderate, flagged_moderate)
    assert result == 61.0
    assert result % 5 != 0


# --- The working ---------------------------------------------------------------

def test_the_breakdown_carries_the_counts_behind_the_score():
    """Returns the working beside the score: helpful and concern counts, the
    summed concern weight, how many ingredients were considered, and the
    product's total. An ingredient counted on both sides is considered once."""
    niacinamide = ing("Niacinamide", "Oily Skin", "Highly Sensitive Skin (S)",
                      concern("Highly Sensitive Skin (S)", "Moderate"))
    retinol = ing("Retinol", None, "Sensitive Skin (S)", concern("Sensitive Skin (S)", "Low"))
    assert breakdown("OSPT", niacinamide, retinol, WATER, ing("Silica", "Oily Skin")) == {
        "helpful": 2, "concerns": 2, "concern_weight": 0.9,
        "considered": 3, "total_ingredients": 4, "limited": False,
    }


@pytest.mark.parametrize("considered, limited", [(1, True), (2, True), (3, False)])
def test_a_score_resting_on_fewer_than_three_ingredients_is_limited(considered, limited):
    """Returns limited=True when fewer than three ingredients say anything about
    the code, and False from three: a 100% resting on one ingredient must be
    distinguishable from one resting on many."""
    result = breakdown("DSPT", *[GLYCERIN] * considered, WATER)
    assert (result["considered"], result["limited"]) == (considered, limited)


def test_the_concern_weight_is_rounded_to_two_places():
    """Returns concern_weight 0.9 for three Low concerns, not the floating-point
    sum 0.8999999999999999."""
    assert breakdown("DSPT", *[flagged_for_dry("Low")] * 3)["concern_weight"] == 0.9


# --- The vocabulary ----------------------------------------------------------

def test_every_good_for_phrase_the_dictionary_writes_is_accounted_for():
    """Returns every good_for phrase the ingredient dictionary can store as
    either mapped to a trait or deliberately neutral, so a phrase added there
    cannot silently count for nothing - the way functional-group names the
    catalogue never used once went unnoticed in the old scorer."""
    phrases = {part.strip().lower()
               for entry in CLINICAL_DICTIONARY.values()
               for part in entry["good_for"].split(",")}
    for probe in ("fragrance", "ceramide x", "xanthan gum", "rose extract",
                  "butylene glycol", "cyclopentasiloxane", "propylparaben", "unknown thing"):
        phrases |= {part.strip().lower() for part in parse_ingredient_data(probe)["good_for"].split(",")}
    unaccounted = phrases - set(GOOD_FOR_TRAITS) - NEUTRAL_GOOD_FOR
    assert unaccounted == set()


def test_salicylic_acid_counts_for_oily_skin_through_its_dictionary_tag():
    """Returns 100.0 for salicylic acid, tagged by the dictionary as good for
    acne-prone skin, scored for an oily code: the BHA credit the old scorer's
    group names failed to give."""
    tagged = parse_ingredient_data("Salicylic Acid")
    salicylic = ing("Salicylic Acid", tagged["good_for"], tagged["bad_for"])
    assert score("ORNT", salicylic) == 100.0


# --- Reasons -----------------------------------------------------------------

def test_match_reasons_name_the_ingredients_for_each_trait_in_code_order():
    """Returns one match reason per trait of the code that something suits, in
    the order of the code, naming up to three ingredients and counting the
    rest."""
    result = compute_baumann_compatibility("DSPW", [
        ing("Retinol", "Aging Skin"),
        ing("Glycerin", "Dry Skin"), ing("Squalane", "Dry Skin"),
        ing("Ceramide NP", "Dry Skin"), ing("Cholesterol", "Dry Skin"),
    ])
    assert result["match_reasons"] == [
        "Suits dry skin: Glycerin, Squalane, Ceramide NP and 1 more.",
        "Suits wrinkle-prone skin: Retinol.",
    ]


def test_a_reason_ending_in_an_abbreviation_gets_one_full_stop():
    """Returns "Suits oily skin: Alcohol Denat." with a single full stop when
    the last ingredient named ends in one, not "Denat.."."""
    result = compute_baumann_compatibility("OSPT", [ing("Alcohol Denat.", "Oily Skin")])
    assert result["match_reasons"] == ["Suits oily skin: Alcohol Denat."]


def test_caution_reasons_name_each_concern_most_serious_first():
    """Returns one caution per flagged ingredient, most serious first, naming
    the concern and its grade, or the flagged trait when no concern explains
    it."""
    result = compute_baumann_compatibility("DSPT", [
        ing("Caprylyl Glycol", None, "Extremely Sensitive Skin (S)",
            concern("Extremely Sensitive Skin (S)", "Low", title="Preservative Sensitivity")),
        ing("Alcohol", None, "Extremely Dry Skin (D)"),
    ])
    assert result["caution_reasons"] == [
        "Alcohol: flagged for Extremely Dry Skin (D) (High)",
        "Caprylyl Glycol: Preservative Sensitivity (Low)",
    ]
