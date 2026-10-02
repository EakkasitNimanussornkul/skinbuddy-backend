"""Tests for the catalogue-expansion loader (app/db/catalog_expansion.py).

The loader checks the documentation session's data file against the spec and
the live catalogue, then generates additive SQL for the owner to review. These
build the "live catalogue" by hand as a Snapshot, so nothing touches the
database.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import copy

import pytest

from app.db.catalog_expansion import Snapshot, generate_sql, q, validate


def snapshot(**overrides):
    snap = Snapshot(
        products={("CeraVe", "Moisturising Cream")},
        slugs={"cerave-moisturising-cream"},
        ingredients={"Water": 1, "Glycerin": 1, "Salicylic Acid": 1, "Sodium Hydroxide": 2},
        ingredient_bad_for={"Water": "None", "Glycerin": "None", "Salicylic Acid": "Extremely Dry Skin (D)"},
        functional_groups={"Solvent", "Humectant", "Beta Hydroxy Acid (BHA)", "Vitamin B3", "Retinoid"},
        source_urls=set(),
        concerns={("Salicylic Acid", "Extremely Dry Skin (D)")},
        rule_pairs={frozenset(("Salicylic Acid", "Glycerin"))},
        category_pairs={frozenset(("Retinoid", "Beta Hydroxy Acid (BHA)"))},
        has_product_sources=True,
    )
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


VALID = {
    "sources": [
        {"key": "S101", "title": "CosIng: Niacinamide", "publisher": "European Commission",
         "url": "https://ec.europa.eu/growth/tools-databases/cosing/details/1", "source_type": "regulatory_register",
         "accessed_on": "2026-10-02", "notes": None},
        {"key": "S102", "title": "Brand ingredient list", "publisher": "Brand",
         "url": "https://brand.example/product", "source_type": "product_database",
         "accessed_on": "2026-10-02", "notes": None},
    ],
    "ingredients": [
        {"name": "Zinc Oxide Test", "functional_group": "Vitamin B3", "awareness_tier": "low",
         "benefits": "Regulates sebum.", "good_for": "Oily Skin, Acne-Prone Skin",
         "bad_for": "Highly Sensitive Skin (S)", "sources": {"function": ["S101"], "good_for": ["S101"]}},
    ],
    "products": [
        {"brand": "Brand", "name": "Clarifying Serum", "category": "Treatments",
         "description": "A serum for oily skin.", "price_thb": 590, "price_usd": 17.5,
         "source_url": "https://brand.example/product",
         "image_source_url": "https://images.openbeautyfacts.org/images/products/1/front.jpg",
         "ingredients": ["Water", "Glycerin", "Zinc Oxide Test", "Salicylic Acid"],
         "sources": {"listing": ["S102"], "price": ["S102"]}},
    ],
    "concerns": [
        {"ingredient": "Zinc Oxide Test", "concern_title": "Flushing", "concern_description": "Can flush reactive skin.",
         "target_profile": "Highly Sensitive Skin (S)", "severity": "Moderate", "sources": ["S101"]},
    ],
    "conflict_rules": [
        {"ingredient_a": "Zinc Oxide Test", "ingredient_b": "Salicylic Acid", "severity": "low",
         "warning_message": "Use at different times of day.", "sources": []},
    ],
    "category_rules": [
        {"group_a": "Vitamin B3", "group_b": "Retinoid", "severity": "low",
         "warning_message": "Introduce one at a time.", "sources": []},
    ],
}


def broken(path, value):
    """VALID with one field replaced; path like ("products", 0, "category")."""
    data = copy.deepcopy(VALID)
    target = data
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return data


def errors_for(data, **snap):
    return validate(data, snapshot(**snap)).errors


def test_a_file_that_follows_the_spec_has_no_errors():
    """Returns no errors for a file whose every row follows the spec and fits the
    live catalogue."""
    report = validate(VALID, snapshot())
    assert report.errors == []


@pytest.mark.parametrize("path, value, expected", [
    (("products", 0, "category"), "Serums", "category 'Serums' is not one of"),
    (("products", 0, "image_source_url"), "https://brand.example/photo.jpg", "must be an Open Beauty Facts image"),
    (("products", 0, "price_thb"), -5, "price_thb must be a positive number"),
    (("products", 0, "ingredients"), ["Water", "Water"], "more than once"),
    (("products", 0, "ingredients"), ["Nonexistent Oil"], "'Nonexistent Oil' does not exist"),
    (("products", 0, "ingredients"), ["Sodium Hydroxide"], "exists as 2 separate rows"),
    (("products", 0, "sources"), {"listing": ["S999"]}, "'S999' is not in this file's sources list"),
    (("products", 0, "sources"), {"ingredients": ["S102"]}, "claim 'ingredients' is not one of"),
    (("ingredients", 0, "functional_group"), "Vitamin b3", "is not an existing group"),
    (("ingredients", 0, "good_for"), "Oily Skin, Combination Skin", "'Combination Skin' is not in the mapped or neutral list"),
    (("ingredients", 0, "bad_for"), "", "bad_for is required"),
    (("ingredients", 0, "name"), "Glycerin", "already exists"),
    (("concerns", 0, "severity"), "Medium", "concerns use Moderate"),
    (("concerns", 0, "target_profile"), "Extremely Dry Skin (D)", "marks (D) but the ingredient's bad_for"),
    (("conflict_rules", 0, "severity"), "Low", "rules use lower case"),
    (("conflict_rules", 0, "ingredient_b"), "Zinc Oxide Test", "two different ingredients"),
    (("category_rules", 0, "group_b"), "Peptides", "are not existing functional groups"),
    (("sources", 0, "key"), "S05", "must be S101 or above"),
    (("sources", 0, "accessed_on"), "last week", "accessed_on must be the YYYY-MM-DD date"),
    (("sources", 0, "source_type"), "website", "source_type 'website' is not one of"),
    (("sources", 0, "url"), "ftp://x", "must start with http:// or https://"),
])
def test_each_rule_of_the_spec_is_enforced(path, value, expected):
    """Returns an error naming the rule, and where it was broken, for each rule of
    the spec broken on its own: product category, image origin, price, repeated or
    unknown or ambiguous ingredients, source references and claims, ingredient
    group and good_for vocabulary, concern and rule severity scales, a concern
    marker its ingredient does not carry, rule pairs, groups, and source keys,
    dates, types and urls."""
    errors = errors_for(broken(path, value))
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("change, expected", [
    ({"products": {("Brand", "Clarifying Serum")}}, "already exists; this file only adds new products"),
    ({"slugs": {"brand-clarifying-serum"}}, "is already taken by another product"),
    ({"rule_pairs": {frozenset(("Salicylic Acid", "Zinc Oxide Test"))}}, "already exists (in either order)"),
    ({"category_pairs": {frozenset(("Retinoid", "Vitamin B3"))}}, "already exists (in either order)"),
])
def test_rows_that_already_exist_are_rejected_not_overwritten(change, expected):
    """Returns an error, never an update, for a product, slug, rule pair (in
    either order) or group pair that already exists in the live catalogue: the
    loader only adds."""
    assert any(expected in e for e in errors_for(VALID, **change))


def test_a_concern_duplicating_a_live_one_is_rejected():
    """Returns an error for a concern whose ingredient and target profile already
    have a concern in the live catalogue."""
    data = copy.deepcopy(VALID)
    data["concerns"].append({"ingredient": "Salicylic Acid", "concern_title": "t", "concern_description": "d",
                             "target_profile": "Extremely Dry Skin (D)", "severity": "High", "sources": []})
    assert any("already exists for this ingredient" in e for e in errors_for(data))


def test_soft_problems_are_warnings_not_errors():
    """Returns warnings, and no errors, for an unused source, a price with no
    price source, and an ingredient defined but used by nothing."""
    data = copy.deepcopy(VALID)
    data["sources"].append({"key": "S150", "title": "Unused", "url": "https://unused.example",
                            "source_type": "peer_reviewed", "accessed_on": "2026-10-02"})
    data["products"][0]["sources"] = {"listing": ["S102"]}
    data["ingredients"].append({"name": "Lonely Extract", "functional_group": "Humectant", "awareness_tier": "low",
                                "benefits": "b", "good_for": "Dry Skin", "bad_for": "None"})
    report = validate(data, snapshot())
    assert report.errors == []
    joined = "\n".join(report.warnings)
    assert "S150 is not referenced" in joined
    assert "has a price but no price source" in joined
    assert "'Lonely Extract' is defined but" in joined


def with_unlisted_citral(concern, rule):
    """VALID plus a new ingredient, Citral, that no product lists, optionally
    named by a concern and/or a conflict rule."""
    data = copy.deepcopy(VALID)
    data["ingredients"].append({"name": "Citral", "functional_group": "Solvent", "awareness_tier": "low",
                                "benefits": "Fragrance.", "good_for": "", "bad_for": "Highly Sensitive Skin (S)"})
    if concern:
        data["concerns"].append({"ingredient": "Citral", "concern_title": "Fragrance allergen",
                                 "concern_description": "Can sensitise reactive skin.",
                                 "target_profile": "Highly Sensitive Skin (S)", "severity": "Moderate", "sources": []})
    if rule:
        data["conflict_rules"].append({"ingredient_a": "Citral", "ingredient_b": "Water", "severity": "low",
                                       "warning_message": "Test rule.", "sources": []})
    return data


@pytest.mark.parametrize("concern, rule, expected", [
    (True, False, "ingredients: 'Citral' is in no product; only its concern(s) use it, so users will never see it"),
    (False, True, "ingredients: 'Citral' is in no product; only its rule(s) use it, so users will never see it"),
    (True, True, "ingredients: 'Citral' is in no product; only its concern(s) and rule(s) use it, so users will never see it"),
    (False, False, "ingredients: 'Citral' is defined but no product, concern or rule uses it"),
])
def test_a_new_ingredient_in_no_product_is_warned_even_if_a_concern_or_rule_names_it(concern, rule, expected):
    """Returns exactly one warning, and no error, for a new ingredient that no
    product lists: when a concern and/or a rule names it, the warning says it is
    in no product, names the concern(s) and/or rule(s) that use it, and says users
    will never see it; when nothing names it, the warning is the unchanged
    "is defined but no product, concern or rule uses it"."""
    report = validate(with_unlisted_citral(concern, rule), snapshot())
    assert report.errors == []
    assert [w for w in report.warnings if "'Citral'" in w] == [expected]


def test_a_new_ingredient_a_product_lists_gets_no_use_warning():
    """Returns no warning about use for a new ingredient that a product lists,
    even when a concern and a rule also name it."""
    report = validate(VALID, snapshot())
    assert not [w for w in report.warnings if "Zinc Oxide Test" in w], report.warnings


def test_concerns_and_rules_on_live_ingredients_get_no_use_warning():
    """Returns no warning for a live ingredient that no product in the file lists
    but a new concern and a new rule name, since adding a concern or a rule to a
    live ingredient is normal."""
    data = copy.deepcopy(VALID)
    data["concerns"].append({"ingredient": "Live Extract", "concern_title": "t", "concern_description": "d",
                             "target_profile": "Highly Sensitive Skin (S)", "severity": "Low", "sources": []})
    data["conflict_rules"].append({"ingredient_a": "Live Extract", "ingredient_b": "Water", "severity": "low",
                                   "warning_message": "Test rule.", "sources": []})
    snap = snapshot()
    snap.ingredients["Live Extract"] = 1
    snap.ingredient_bad_for["Live Extract"] = "Highly Sensitive Skin (S)"
    report = validate(data, snap)
    assert report.errors == []
    assert not [w for w in report.warnings if "Live Extract" in w], report.warnings


def test_sql_is_still_generated_when_the_only_problems_are_warnings(tmp_path, monkeypatch):
    """Returns exit code 0 from the sql command, prints the in-no-product warning,
    and writes the SQL file, including the new ingredient, when the file's only
    problems are warnings."""
    import json
    from app.db import catalog_expansion

    monkeypatch.setattr(catalog_expansion, "read_snapshot", lambda: snapshot())
    source = tmp_path / "expansion.json"
    source.write_text(json.dumps(with_unlisted_citral(concern=True, rule=False)), encoding="utf-8")
    out = tmp_path / "out.sql"
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    assert catalog_expansion.main(["sql", str(source), "--out", str(out)]) == 0
    assert any("WARNING  ingredients: 'Citral' is in no product" in line for line in printed)
    assert "select 'Citral'" in out.read_text(encoding="utf-8")


# --- SQL -------------------------------------------------------------------------

def test_every_insert_is_guarded_so_the_sql_can_run_twice():
    """Returns SQL in one transaction whose every insert is guarded - by a not
    exists check on the row's natural key, or on conflict do nothing on a
    junction - and which contains no update or delete."""
    sql = generate_sql(VALID, snapshot())
    statements = [s for s in sql.split(";") if "insert into" in s]
    assert statements
    assert all("where not exists" in s or "on conflict do nothing" in s or "and not exists" in s for s in statements)
    lowered = sql.lower()
    assert "begin;" in lowered and "commit;" in lowered
    assert "update public." not in lowered and "delete from" not in lowered


def test_quotes_in_the_data_are_escaped():
    """Returns SQL literals with single quotes doubled, so text like "Paula's
    Choice" cannot end a string early."""
    assert q("Paula's Choice") == "'Paula''s Choice'"
    assert q(None) == "null" and q(17.5) == "17.5"
    data = broken(("products", 0, "brand"), "Paula's Choice")
    assert "'Paula''s Choice'" in generate_sql(data, snapshot())


def test_product_sources_wait_for_migration_0010():
    """Returns product-source inserts as live SQL when migration 0010 has run,
    and only as comments, with an instruction to regenerate, when it has not."""
    with_table = generate_sql(VALID, snapshot())
    without = generate_sql(VALID, snapshot(has_product_sources=False))
    assert "\ninsert into public.product_sources" in with_table
    assert "\ninsert into public.product_sources" not in without
    assert "-- insert into public.product_sources" in without
    assert "Run 0010, then regenerate" in without


def test_images_are_listed_for_the_separate_images_step():
    """Returns each product's Open Beauty Facts image as a comment naming the
    images command, since SQL cannot fetch an image."""
    sql = generate_sql(VALID, snapshot())
    assert "python -m app.db.catalog_expansion images" in sql
    assert "https://images.openbeautyfacts.org/images/products/1/front.jpg" in sql


def test_a_pair_listed_twice_in_opposite_orders_is_rejected():
    """Returns an error for the second of two rules in one file that name the
    same ingredient pair in opposite orders, since a rule's pair has no order."""
    data = copy.deepcopy(VALID)
    data["conflict_rules"].append({"ingredient_a": "Salicylic Acid", "ingredient_b": "Zinc Oxide Test",
                                   "severity": "low", "warning_message": "Again.", "sources": []})
    assert any("conflict_rules[1]" in e and "already exists (in either order)" in e for e in errors_for(data))
