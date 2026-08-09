"""Unit tests for compute_baumann_compatibility (app/api/products.py).

Pure scoring function: Baumann code + ingredient list -> score/reasons.
Base score is 75; each axis adjusts it, and the result is clamped to 15..99.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.api.products import compute_baumann_compatibility


def ing(name, group=None):
    return {"name": name, "functional_group": group} if group else {"name": name}


# --- Guard clause ------------------------------------------------------------

@pytest.mark.parametrize("bad_code", ["", None, "DS", "X"])
def test_missing_or_short_code_returns_neutral_default(bad_code):
    """Returns the neutral default score of 85 with no caution reasons when the
    skin-type code is absent or shorter than four characters."""
    result = compute_baumann_compatibility(bad_code, [])
    assert result["score"] == 85
    assert result["caution_reasons"] == []


# --- Axis 1: Oily (O) vs Dry (D) ---------------------------------------------

def test_dry_skin_rewards_humectants():
    """Returns 85 (base 75 + 10) and a barrier-repair match reason for a
    humectant scored against a Dry-axis profile."""
    result = compute_baumann_compatibility("DSPT", [ing("Glycerin", "Humectant")])
    assert result["score"] == 85  # 75 base + 10
    assert any("Barrier-repair" in r for r in result["match_reasons"])


def test_dry_skin_penalises_drying_alcohol():
    """Returns 55 (base 75 - 20) and a drying-risk caution for a volatile alcohol
    scored against a Dry-axis profile."""
    result = compute_baumann_compatibility("DSPT", [ing("Alcohol Denat.", "Solvent")])
    assert result["score"] == 55  # 75 base - 20
    assert any("Drying risk" in r for r in result["caution_reasons"])


def test_oily_skin_penalises_heavy_occlusives():
    """Returns 60 (base 75 - 15) and a heavy-texture caution for an occlusive
    scored against an Oily-axis profile."""
    result = compute_baumann_compatibility("OSPT", [ing("Petrolatum", "Heavy Occlusive")])
    assert result["score"] == 60  # 75 base - 15
    assert any("Heavy texture" in r for r in result["caution_reasons"])


def test_oily_skin_rewards_oil_control_actives():
    """Returns 95 with two match reasons: niacinamide scores on two axes at once,
    +10 for sebum control on the O axis and +10 for tone refining on the P axis."""
    result = compute_baumann_compatibility("OSPT", [ing("Niacinamide", "Vitamin B3")])
    assert result["score"] == 95  # 75 base + 10 (O) + 10 (P)
    assert len(result["match_reasons"]) == 2


# --- Axis 2: Sensitive (S) vs Resistant (R) ----------------------------------

def test_sensitive_skin_penalises_fragrance_heavily():
    """Returns 50 (base 75 - 25) and a sensitivity-trigger caution for fragrance
    scored against a Sensitive-axis profile."""
    result = compute_baumann_compatibility("DSPT", [ing("Parfum", "Fragrance Component")])
    assert result["score"] == 50  # 75 base - 25
    assert any("Sensitivity trigger" in r for r in result["caution_reasons"])


def test_resistant_skin_gets_small_bonus():
    """Returns 80 (base 75 + 5) for a Resistant-axis profile with no ingredients."""
    assert compute_baumann_compatibility("ORNT", [])["score"] == 80  # 75 base + 5


# --- Axis 4: Wrinkle-prone (W) -----------------------------------------------

def test_wrinkle_prone_rewards_retinoids():
    """Returns 90 (base 75 + 5 Resistant + 10 Wrinkle-prone) for a retinoid."""
    result = compute_baumann_compatibility("DRNW", [ing("Retinol", "Retinoid")])
    assert result["score"] == 90  # 75 base + 5 (R) + 10 (W)


# --- Invariants --------------------------------------------------------------

def test_score_is_always_clamped_between_15_and_99():
    """Returns a score within 15..99 for every skin-type code, even when several
    penalties stack on the same product."""
    stacked_penalties = [ing("Alcohol Denat.", "Solvent"), ing("Parfum", "Fragrance Component")]
    for code in ("DSPT", "OSPW", "DRNW", "ORNT"):
        score = compute_baumann_compatibility(code, stacked_penalties)["score"]
        assert 15 <= score <= 99


def test_match_reasons_never_empty():
    """Returns the fallback reason "Suitable for daily routine wear." rather than
    an empty list when no axis rule matches. The UI renders this list directly."""
    result = compute_baumann_compatibility("ORNT", [ing("Water", "Solvent")])
    assert result["match_reasons"] == ["Suitable for daily routine wear."]


def test_ingredients_without_functional_group_are_tolerated():
    """Returns a valid score without raising when an ingredient has no
    functional_group, which is common for newly ingested catalogue rows."""
    result = compute_baumann_compatibility("DSPT", [ing("Mystery Compound")])
    assert 15 <= result["score"] <= 99
