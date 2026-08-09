"""Unit tests for the shared ingredient dictionary.

The most important test here is the Baumann-marker invariant. The skin-type
conflict check in compatibility_service.analyze() matches on the literal
substring "(S)" / "(D)" / etc. inside `bad_for`. A copy of this dictionary once
omitted those markers, which silently disabled every skin-type warning for
anything seeded through that path — no error, no log, just no warnings. This
test makes that class of bug loud.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.core.services.ingredient_dictionary import (
    CLINICAL_DICTIONARY,
    FRAGRANCE_ALLERGENS,
    GENERAL_SOURCE_NOTE,
    HEURISTIC_SOURCE_NOTE,
    parse_ingredient_data,
)

REQUIRED_KEYS = {"group", "tier", "benefits", "good_for", "bad_for", "source"}

# Axis word in bad_for -> the marker compatibility_service.analyze() looks for.
AXIS_MARKERS = {"sensitive": "(S)", "dry": "(D)"}


def assert_baumann_markers_present(bad_for, context):
    lowered = bad_for.lower()
    for word, marker in AXIS_MARKERS.items():
        if word in lowered:
            assert marker in bad_for, (
                f"{context}: bad_for={bad_for!r} names the '{word}' axis but is missing "
                f"the {marker} marker, so compatibility_service.analyze() will never "
                f"raise a skin-type warning for it."
            )


# --- Structure ---------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CLINICAL_DICTIONARY))
def test_every_entry_has_all_required_keys(name):
    """Confirms every dictionary entry defines all six required keys (group,
    tier, benefits, good_for, bad_for, source), so seeding cannot fail partway
    through on a missing key."""
    assert REQUIRED_KEYS <= set(CLINICAL_DICTIONARY[name]), \
        f"{name} is missing {REQUIRED_KEYS - set(CLINICAL_DICTIONARY[name])}"


@pytest.mark.parametrize("name", sorted(CLINICAL_DICTIONARY))
def test_keys_are_lowercase_so_lookup_works(name):
    """Confirms every dictionary key is lowercase. parse_ingredient_data()
    lowercases before lookup, so a capitalised key would be unreachable."""
    assert name == name.lower()


@pytest.mark.parametrize("name", sorted(CLINICAL_DICTIONARY))
def test_tier_is_a_known_value(name):
    """Confirms every entry's awareness tier is one of low, medium or high."""
    assert CLINICAL_DICTIONARY[name]["tier"] in {"low", "medium", "high"}


# --- The invariant that matters ----------------------------------------------

@pytest.mark.parametrize("name", sorted(CLINICAL_DICTIONARY))
def test_dictionary_bad_for_carries_baumann_markers(name):
    """Confirms any curated bad_for text naming the sensitive or dry axis carries
    its (S) or (D) marker, which is the exact substring the conflict engine
    matches on when raising a skin-type warning."""
    assert_baumann_markers_present(CLINICAL_DICTIONARY[name]["bad_for"], name)


@pytest.mark.parametrize("probe", [
    "Parfum", "Limonene", "Some Unknown Compound", "Ceramide XYZ",
    "Butylene Glycol", "Cyclopentasiloxane", "Isobutylparaben", "Rosemary Extract",
])
def test_heuristic_fallback_bad_for_carries_baumann_markers(probe):
    """Confirms the same (S) / (D) marker rule holds for every heuristic fallback
    branch, not only for curated dictionary entries."""
    assert_baumann_markers_present(parse_ingredient_data(probe)["bad_for"], probe)


# --- Lookup behaviour --------------------------------------------------------

def test_known_ingredient_lookup_is_case_insensitive():
    """Returns the Vitamin B3 profile for Niacinamide regardless of the casing
    used in the catalogue."""
    assert parse_ingredient_data("Niacinamide")["group"] == "Vitamin B3"
    assert parse_ingredient_data("NIACINAMIDE")["group"] == "Vitamin B3"


def test_known_ingredients_are_marked_as_curated_not_guessed():
    """Returns the general-consensus source note for a curated entry, marking it
    as looked up rather than inferred from its name."""
    assert parse_ingredient_data("Retinol")["source"] == GENERAL_SOURCE_NOTE


def test_unknown_ingredients_are_marked_as_heuristic():
    """Returns source="uncategorized-heuristic" for an unrecognised ingredient,
    so the data itself distinguishes a lookup from a name-pattern guess."""
    assert parse_ingredient_data("Zzz Unknown Compound")["source"] == HEURISTIC_SOURCE_NOTE


@pytest.mark.parametrize("name, expected_group", [
    ("Parfum", "Fragrance Component"),
    ("Fragrance", "Fragrance Component"),
    ("Ceramide XYZ", "Skin-Identical Lipid"),
    ("Xanthan Gum", "Texture Enhancer"),
    ("Rosemary Leaf Extract", "Botanical Nutrient"),
    ("Butylene Glycol", "Solvent & Humectant"),
    ("Cyclopentasiloxane", "Silicone"),
    ("Isobutylparaben", "Preservative"),
])
def test_heuristic_branches_classify_as_expected(name, expected_group):
    """Returns the expected functional group for each name-pattern heuristic
    branch (fragrance, ceramide, gum, extract, glycol, silicone, paraben)."""
    assert parse_ingredient_data(name)["group"] == expected_group


@pytest.mark.parametrize("allergen", FRAGRANCE_ALLERGENS)
def test_all_listed_fragrance_allergens_classify_as_fragrance(allergen):
    """Returns the Fragrance Component group for every allergen in the declared
    fragrance-allergen list, with none falling through to another branch."""
    assert parse_ingredient_data(allergen)["group"] == "Fragrance Component"


def test_exact_dictionary_entry_wins_over_heuristic():
    """Returns the curated source note for Ceramide NP, confirming an exact
    dictionary entry takes precedence over the generic ceramide heuristic that
    would otherwise mislabel its source."""
    assert parse_ingredient_data("Ceramide NP")["source"] == GENERAL_SOURCE_NOTE


def test_result_is_always_usable_by_the_seeder():
    """Returns all six required keys even for a completely novel ingredient, so
    catalog_service.upsert_ingredient() can index them without a KeyError."""
    assert REQUIRED_KEYS <= set(parse_ingredient_data("Totally Novel Substance"))
