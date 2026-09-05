"""Unit tests for calculate_safety_flags (app/core/services/ingredientcheck_service.py).

Pure function, no DB. Several of these are regression tests pinning bugs that
were fixed — they exist so the bug cannot silently return.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.core.services.ingredientcheck_service import calculate_safety_flags


def ings(*names):
    """Build the Supabase join shape: [{"ingredients": {"name": ...}}, ...]"""
    return [{"ingredients": {"name": n}} for n in names]


# --- Regressions -------------------------------------------------------------

def test_phenoxyethanol_is_not_an_alcohol():
    """REGRESSION: "ethanol" was matched as a bare substring, so the common
    preservative Phenoxyethanol wrongly cleared alcohol_free."""
    assert calculate_safety_flags(ings("Phenoxyethanol"))["alcohol_free"] is True


def test_fatty_alcohols_are_not_drying_alcohols():
    """Returns alcohol_free=True. Cetyl and Cetearyl Alcohol are emollients, not
    the drying alcohols the flag is meant to detect."""
    flags = calculate_safety_flags(ings("Cetyl Alcohol", "Cetearyl Alcohol"))
    assert flags["alcohol_free"] is True


def test_real_fungal_acne_triggers_are_detected():
    """REGRESSION: the check looked for the literal substring "ester", which
    appears in none of the actual trigger ingredient names."""
    for trigger in ("Isopropyl Myristate", "Ethylhexyl Palmitate", "Glyceryl Stearate",
                    "Polysorbate 60", "Sorbitan Oleate"):
        flags = calculate_safety_flags(ings(trigger))
        assert flags["fungal_safe"] is False, f"{trigger} should not be fungal-safe"


# --- True positives ----------------------------------------------------------

@pytest.mark.parametrize("name, flag", [
    ("Alcohol Denat.", "alcohol_free"),
    ("SD Alcohol 40", "alcohol_free"),
    ("Ethanol", "alcohol_free"),
    ("Parfum", "fragrance_free"),
    ("Limonene", "fragrance_free"),
    ("Methylparaben", "paraben_free"),
    ("Dimethicone", "silicone_free"),
    ("Sodium Lauryl Sulfate", "sulfate_free"),
    ("Beeswax", "vegan"),
    ("Lanolin", "vegan"),
])
def test_flag_trips_on_known_ingredient(name, flag):
    """Returns the corresponding flag as False for each ingredient the flag targets."""
    assert calculate_safety_flags(ings(name))[flag] is False


# --- Clean baselines ---------------------------------------------------------

def test_clean_formula_passes_every_flag():
    """Returns all seven flags as True for a formula containing none of the
    targeted ingredient classes."""
    flags = calculate_safety_flags(ings("Water", "Glycerin", "Niacinamide", "Ceramide NP"))
    assert all(flags.values()), f"unexpected failures: {flags}"


def test_empty_ingredient_list_is_all_clear():
    """Returns all seven flags as True when no ingredients are supplied."""
    assert all(calculate_safety_flags([]).values())


# --- Input-shape robustness --------------------------------------------------

def test_accepts_flat_ingredient_dicts():
    """Returns fragrance_free=False for a flat ingredient dict. Some callers pass
    ingredients already flattened, without the Supabase join wrapper."""
    assert calculate_safety_flags([{"name": "Parfum"}])["fragrance_free"] is False


def test_ignores_null_and_malformed_rows():
    """Returns all flags True without raising when rows are null or lack a name,
    skipping the malformed entries rather than failing."""
    rows = [{"ingredients": None}, {"ingredients": {}}, {"ingredients": {"name": "Water"}}]
    assert all(calculate_safety_flags(rows).values())


def test_matching_is_case_insensitive():
    """Returns silicone_free=False for an uppercase ingredient name, confirming
    matching does not depend on the casing used in the catalogue."""
    assert calculate_safety_flags(ings("DIMETHICONE"))["silicone_free"] is False


# --- Word-boundary terms (BE-DEF-09) -----------------------------------------

def test_honeysuckle_extract_is_not_treated_as_honey():
    """Returns vegan=True for Lonicera Japonica (Honeysuckle) Flower Extract,
    which is plant-derived.

    Regression guard for BE-DEF-09: "honey" was matched as a bare substring and
    so matched inside "honeysuckle", marking a plant extract non-vegan."""
    flags = calculate_safety_flags(ings("Lonicera Japonica (Honeysuckle) Flower Extract"))
    assert flags["vegan"] is True


@pytest.mark.parametrize("name", ["Honey", "Honey Extract", "Manuka Honey"])
def test_actual_honey_is_still_flagged_non_vegan(name):
    """Returns vegan=False for honey itself, so excluding "honeysuckle" does not
    also excuse the ingredient the rule exists for."""
    assert calculate_safety_flags(ings(name))["vegan"] is False
