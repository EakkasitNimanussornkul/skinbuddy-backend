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


def clean_and_normalize_ingredient(raw_name: str) -> str:
    name = re.sub(r'[\(\)\*]', '', raw_name.replace("en:", "").replace("-", " ").strip().lower())
    return INCI_ALIAS_MAP.get(name, name.title())


def ingredient_key(raw_name: str) -> str:
    """The case-insensitive key two ingredient names are compared on.

    clean_and_normalize_ingredient title-cases its result, which turns
    "Sodium PCA" into "Sodium Pca", so it cannot be compared with a stored name
    directly. Lower-casing it gives a key that matches regardless of case.
    """
    return clean_and_normalize_ingredient(raw_name).lower()
