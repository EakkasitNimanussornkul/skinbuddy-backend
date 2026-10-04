"""The shared ingredient-name normaliser (app/core/services/ingredient_names.py).

It moved out of app/db/ingest_catalog.py so search, the paste matcher and
submission approval use the importer's rules. These tests pin that the importer
still uses the same function and that its output did not change in the move.
The expected values below were read off the original function in ingest_catalog
at origin/dev af4cd1e.
"""

import pytest

from app.core.services import ingredient_names
from app.core.services.ingredient_names import clean_and_normalize_ingredient, ingredient_key
from app.db import ingest_catalog


def test_ingest_catalog_uses_the_shared_normaliser():
    """ingest_catalog's clean_and_normalize_ingredient and INCI_ALIAS_MAP are the shared
    module's own objects, not copies."""
    assert ingest_catalog.clean_and_normalize_ingredient is ingredient_names.clean_and_normalize_ingredient
    assert ingest_catalog.INCI_ALIAS_MAP is ingredient_names.INCI_ALIAS_MAP


@pytest.mark.parametrize("raw, expected", [
    ("en:aqua", "Water"),
    ("Aqua", "Water"),
    ("Water (Aqua)", "Water Aqua"),
    ("(Aqua)", "Water"),
    ("Aqua *", "Aqua "),
    ("Sodium PCA", "Sodium Pca"),
    ("Beta-Glucan", "Beta Glucan"),
    ("  Glycerol  ", "Glycerin"),
    ("VITAMIN C", "Ascorbic Acid"),
    ("Alcool Cétylique", "Cetyl Alcohol"),
    ("Cholestérol", "Cholesterol"),
    ("en:sodium-hyaluronate", "Sodium Hyaluronate"),
    ("Camellia Oleifera (Green Tea) Leaf Extract", "Camellia Oleifera Green Tea Leaf Extract"),
    ("d-panthenol", "D Panthenol"),
    ("1,2-Hexanediol", "1,2 Hexanediol"),
    ("Tocopherol*", "Tocopherol"),
])
def test_normaliser_output_is_unchanged_by_the_move(raw, expected):
    """clean_and_normalize_ingredient gives the same result as the original function in
    ingest_catalog did for each listed name: prefixes, hyphens, brackets and asterisks
    handled, INCI aliases mapped, everything else title-cased."""
    assert clean_and_normalize_ingredient(raw) == expected


def test_ingredient_key_ignores_letter_case():
    """ingredient_key gives one lower-case key for "Sodium PCA", "sodium pca" and "SODIUM PCA",
    and maps an alias to its canonical name's key ("aqua" -> "water")."""
    assert {ingredient_key(n) for n in ("Sodium PCA", "sodium pca", "SODIUM PCA")} == {"sodium pca"}
    assert ingredient_key("Aqua") == "water"
