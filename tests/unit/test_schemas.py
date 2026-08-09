"""Unit tests for Pydantic request validation (app/schemas.py).

users.skin_type is the single value every personalisation path reads
(compute_baumann_compatibility, the skin-type pass in compatibility_service).
A malformed value doesn't crash anything — it just silently matches nothing —
so input validation is the only thing keeping garbage out.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

from itertools import product

import pytest
from pydantic import ValidationError

from app.schemas import QuizResultCreate

ALL_VALID_CODES = ["".join(c) for c in product("OD", "SR", "PN", "WT")]


@pytest.mark.parametrize("code", ALL_VALID_CODES)
def test_all_sixteen_baumann_codes_are_accepted(code):
    """Returns a valid model with skinType unchanged for every one of the sixteen
    Baumann codes (one letter from each of the O/D, S/R, P/N and W/T axes)."""
    assert QuizResultCreate(skinType=code, scores={"hydration": 1.0}).skinType == code


def test_there_are_exactly_sixteen_valid_codes():
    """Confirms the Baumann classification yields exactly 16 valid codes, so the
    acceptance test above covers the whole valid input space."""
    assert len(ALL_VALID_CODES) == 16


@pytest.mark.parametrize("bad_code", [
    "XYZ1",      # nonsense
    "dspt",      # lowercase
    "DSP",       # too short
    "DSPTW",     # too long
    "",          # empty
    "DDPT",      # two letters from the same axis
    "SDPT",      # axes out of order
    "DS PT",     # whitespace
    "DSPT ",     # trailing space
])
def test_malformed_skin_type_is_rejected(bad_code):
    """Raises ValidationError for any code that is not one of the sixteen valid
    ones, covering wrong casing, wrong length, wrong axis order and whitespace."""
    with pytest.raises(ValidationError):
        QuizResultCreate(skinType=bad_code, scores={"hydration": 1.0})


def test_scores_must_be_numeric():
    """Raises ValidationError when a quiz score is a non-numeric string that
    cannot be coerced to float."""
    with pytest.raises(ValidationError):
        QuizResultCreate(skinType="DSPT", scores={"hydration": "very high"})


def test_skin_type_is_required():
    """Raises ValidationError when skinType is omitted entirely, so a quiz result
    can never be saved without a skin type."""
    with pytest.raises(ValidationError):
        QuizResultCreate(scores={"hydration": 1.0})
