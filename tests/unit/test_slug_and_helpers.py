"""Unit tests for create_slug and the pure helpers in compatibility_service.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import pytest

from app.core.utils import create_slug
from app.api.products import postgrest_quote
from app.core.services.compatibility_service import (
    _build_comparison_maps,
    compute_ingredient_similarity,
    normalize_product,
    normalize_text_accents,
)


# --- create_slug -------------------------------------------------------------

@pytest.mark.parametrize("brand, name, expected", [
    ("COSRX", "Advanced Snail 96 Mucin Power Essence",
     "cosrx-advanced-snail-96-mucin-power-essence"),
    ("CeraVe", "Hydrating Facial Cleanser", "cerave-hydrating-facial-cleanser"),
    ("The Ordinary", "Niacinamide 10% + Zinc 1%", "the-ordinary-niacinamide-10-zinc-1"),
    ("Paula's Choice", "2% BHA Liquid Exfoliant", "paula-s-choice-2-bha-liquid-exfoliant"),
])
def test_create_slug_examples(brand, name, expected):
    """Returns the expected lowercase hyphenated slug for each real catalogue
    product, collapsing punctuation and percent signs into single separators."""
    assert create_slug(brand, name) == expected


def test_slug_never_has_leading_or_trailing_dashes():
    """Returns a slug with no leading or trailing separator even when the brand
    and product name both begin and end with punctuation."""
    slug = create_slug("!!Brand!!", "!!Product!!")
    assert not slug.startswith("-") and not slug.endswith("-")


def test_slug_is_url_safe():
    """Returns a slug containing only lowercase alphanumerics and hyphens, with
    trademark symbols and dots removed."""
    slug = create_slug("Dr. Jart+", "Cicapair™ Tiger Grass Re.Pair Serum")
    assert all(c.isalnum() or c == "-" for c in slug), slug


def test_slug_is_stable_across_calls():
    """Returns an identical slug on repeated calls with the same inputs.
    products.slug is a unique column, so an unstable slug means duplicate rows."""
    assert create_slug("CeraVe", "Foaming Cleanser") == create_slug("CeraVe", "Foaming Cleanser")


# --- normalize_text_accents --------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Céramide", "ceramide"),
    ("  Retinoid  ", "retinoid"),
    ("BETA HYDROXY ACID (BHA)", "beta hydroxy acid (bha)"),
    ("", ""),
    (None, ""),
])
def test_normalize_text_accents(raw, expected):
    """Returns the text lowercased, trimmed and stripped of accent marks, and an
    empty string for empty or null input."""
    assert normalize_text_accents(raw) == expected


# --- normalize_product -------------------------------------------------------

def test_normalize_product_flattens_the_supabase_join():
    """Returns {name, ingredients} with the nested product_ingredients join
    flattened into a flat ingredient list in its original order."""
    joined = {
        "name": "Test Serum",
        "product_ingredients": [
            {"ingredients": {"id": "i1", "name": "Retinol", "functional_group": "Retinoid"}},
            {"ingredients": {"id": "i2", "name": "Squalane", "functional_group": "Emollient"}},
        ],
    }
    result = normalize_product(joined)
    assert result["name"] == "Test Serum"
    assert [i["name"] for i in result["ingredients"]] == ["Retinol", "Squalane"]


@pytest.mark.parametrize("payload", [
    {"name": "Bare Product"},
    {"name": "Empty", "product_ingredients": []},
    {"name": "Nulls", "product_ingredients": None},
    {"name": "Null rows", "product_ingredients": [{"ingredients": None}]},
])
def test_normalize_product_tolerates_missing_joins(payload):
    """Returns an empty ingredient list without raising when the join is absent,
    empty, null, or contains null rows."""
    assert normalize_product(payload)["ingredients"] == []


# --- _build_comparison_maps --------------------------------------------------

def test_build_comparison_maps_indexes_groups_and_ids():
    """Returns a functional-group map of {group: [(product, ingredient)]} and an
    ingredient-id map of {ingredient_id: [product names]}."""
    products = [{
        "name": "Retinol Serum",
        "ingredients": [{"id": "i1", "name": "Retinol", "functional_group": "Retinoid"}],
    }]
    groups, ingredient_ids = _build_comparison_maps(products)
    assert groups == {"retinoid": [("Retinol Serum", "Retinol")]}
    assert ingredient_ids == {"i1": ["Retinol Serum"]}


def test_build_comparison_maps_normalises_group_case_and_accents():
    """Returns the functional group as a normalised lowercase, accent-free key.
    Matching against category_conflict_rules is done on normalised text."""
    products = [{
        "name": "P", "ingredients": [{"id": "i1", "name": "X", "functional_group": "Rétinoid"}],
    }]
    groups, _ = _build_comparison_maps(products)
    assert "retinoid" in groups


def test_build_comparison_maps_skips_ingredients_without_a_group():
    """Returns an empty group map but still indexes the ingredient id, so an
    ingredient with no functional_group cannot trigger a category conflict."""
    products = [{"name": "P", "ingredients": [{"id": "i1", "name": "Water"}]}]
    groups, ingredient_ids = _build_comparison_maps(products)
    assert groups == {}
    assert ingredient_ids == {"i1": ["P"]}


# --- compute_ingredient_similarity -------------------------------------------
#
# Jaccard over ingredient ID sets. Exercised throughout the suite via compare,
# the similar-products ranking and dupe detection, but those all reach it
# through a handler; these state its own contract.


def ings(*ids):
    return [{"id": i, "name": i.upper()} for i in ids]


def test_similarity_of_identical_ingredient_sets_is_100():
    """Returns 100.0 for two products whose ingredient ids are the same set."""
    assert compute_ingredient_similarity(ings("a", "b", "c"), ings("a", "b", "c")) == 100.0


def test_similarity_of_disjoint_ingredient_sets_is_zero():
    """Returns 0.0 when the two products share no ingredient."""
    assert compute_ingredient_similarity(ings("a", "b"), ings("c", "d")) == 0.0


def test_similarity_is_the_intersection_over_the_union():
    """Returns 50.0 for {a,b,c} against {b,c,d}: two shared over a union of
    four. Jaccard, not the share of either list on its own."""
    assert compute_ingredient_similarity(ings("a", "b", "c"), ings("b", "c", "d")) == 50.0


def test_similarity_is_rounded_to_one_decimal_place():
    """Returns 33.3 for one shared ingredient over a union of three, rather than
    the unrounded 33.33... The value is rendered as a percentage."""
    assert compute_ingredient_similarity(ings("a", "b"), ings("b", "c")) == 33.3


def test_similarity_ignores_ingredients_carrying_no_id():
    """Returns 100.0 when the only difference between two lists is an entry with
    no id, since the comparison is over ids and an entry without one cannot be
    matched against anything."""
    a = ings("a", "b") + [{"name": "Unidentified"}]
    assert compute_ingredient_similarity(a, ings("a", "b")) == 100.0


def test_similarity_of_two_empty_lists_is_zero_not_a_missing_answer():
    """Returns 0.0 when neither product has ingredient rows.

    Deliberate, and worth stating because the value is ambiguous: 0.0 here means
    "there was nothing to compare", which is not the same answer as two
    populated products sharing nothing. The function keeps returning a float
    because callers feed it to a `<` threshold and a sort key; a caller that
    displays the figure has to separate the two cases from the ingredient lists
    it already has."""
    assert compute_ingredient_similarity([], []) == 0.0


def test_similarity_with_one_empty_list_is_a_true_zero():
    """Returns 0.0 when one product has ingredients and the other has none,
    which unlike the case above is a real share-nothing result."""
    assert compute_ingredient_similarity(ings("a", "b"), []) == 0.0


# --- postgrest_quote (BE-DEF-06) ---------------------------------------------

def test_postgrest_quote_escapes_the_double_quote_that_would_end_the_value():
    """Returns the double quote backslash-escaped, so a value containing one
    cannot terminate the quoted PostgREST filter it is embedded in."""
    assert postgrest_quote('say "hi"') == 'say \\"hi\\"'


def test_postgrest_quote_escapes_backslashes_before_quotes():
    """Returns a doubled backslash, so a trailing backslash cannot escape the
    closing quote of the filter value."""
    assert postgrest_quote("back\\slash") == "back\\\\slash"
    assert postgrest_quote('a\\"b') == 'a\\\\\\"b'


@pytest.mark.parametrize("value", ["Vitamin C, 10%", "serum, cleanser", "a,b"])
def test_postgrest_quote_leaves_commas_intact_for_the_quotes_to_contain(value):
    """Returns the comma unchanged. It is neutralised by the surrounding quotes
    rather than by stripping it, so the user still searches for what they
    typed."""
    assert postgrest_quote(value) == value


@pytest.mark.parametrize("value", ["niacinamide 10%", "2% BHA", "under_score"])
def test_postgrest_quote_preserves_like_wildcards(value):
    """Returns % and _ unchanged. They have always behaved as SQL LIKE
    wildcards in catalogue search, and this escaping deliberately does not
    change that."""
    assert postgrest_quote(value) == value
