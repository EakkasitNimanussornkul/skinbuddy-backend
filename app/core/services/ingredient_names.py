"""The one place ingredient names are normalised.

Moved here from app/db/ingest_catalog.py so the Open Beauty Facts importer,
ingredient search, the paste matcher and product-submission approval all apply
the same rules. ingest_catalog imports both names from here, unchanged.

Migration 0013 mirrors `ingredient_key` in SQL, as public.ingredient_name_key(),
because approve_submission() and admin_update_product() re-check a new name
against the ingredients table inside their own transaction. The two must give
the same key: tests/postgres/test_submission_functions.py compares them for every
alias below, so a change here has to be made in 0013 as well.
"""

import re

INCI_ALIAS_MAP = {
    "aqua": "Water", "eau": "Water", "purified water": "Water",
    "glycerol": "Glycerin", "l ascorbic acid": "Ascorbic Acid",
    "vitamin c": "Ascorbic Acid", "vitamin e": "Tocopherol",
    "provitamin b5": "Panthenol", "d-panthenol": "Panthenol",
    "bha": "Salicylic Acid", "aha": "Glycolic Acid",
    "hyaluronate sodium": "Sodium Hyaluronate", "centella asiatica extract": "Centella Asiatica",
    "alcool cétylique": "Cetyl Alcohol", "cholestérol": "Cholesterol"
}


def clean_ingredient_text(raw_name: str) -> str:
    """The name with its prefix, hyphens, brackets and asterisks dealt with,
    lower-cased, before any alias is applied."""
    return re.sub(r'[\(\)\*]', '', raw_name.replace("en:", "").replace("-", " ").strip().lower())


def clean_and_normalize_ingredient(raw_name: str) -> str:
    name = clean_ingredient_text(raw_name)
    return INCI_ALIAS_MAP.get(name, name.title())


def matched_alias(raw_name: str):
    """The INCI_ALIAS_MAP entry this name was mapped through ("aqua" for "Aqua"),
    or None when no alias applied. Ingredient search and the paste matcher
    return it, so the page can say why "aqua" found Water."""
    name = clean_ingredient_text(raw_name)
    return name if name in INCI_ALIAS_MAP else None


def ingredient_key(raw_name: str) -> str:
    """The case-insensitive key two ingredient names are compared on.

    clean_and_normalize_ingredient title-cases its result, which turns
    "Sodium PCA" into "Sodium Pca", so it cannot be compared with a stored name
    directly. Lower-casing it gives a key that matches regardless of case.
    """
    return clean_and_normalize_ingredient(raw_name).lower()
