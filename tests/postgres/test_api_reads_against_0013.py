"""The stage-2 API's reads and review numbering, against the local 0013 replica.

The fake Supabase client ignores select strings, and 0013 is not yet applied to
the live database, so these tests check the replica instead: every column and
foreign key the new selects and embeds name exists once 0013 has run, the
ordering the product reads ask PostgREST for gives pack order, and the review
screen's merge and 0-based numbering are the ones approve_submission() uses.

Skipped unless local PostgreSQL binaries and the (local-only) migration file are
present; see tests/postgres/pg_harness.py.
"""

import json

import pytest

from app.core.services.submission_service import merged_payload, review_ingredients
from pg_harness import lit

ADMIN = "00000000-0000-0000-0000-0000000000a1"
USER = "00000000-0000-0000-0000-0000000000b1"
WATER = "11111111-0000-0000-0000-000000000001"
GLYCERIN = "11111111-0000-0000-0000-000000000002"

SEED = f"""
truncate public.product_sources, public.ingredient_sources, public.product_ingredients,
         public.product_submissions, public.sources, public.products, public.ingredients,
         public.users cascade;
insert into public.users (id, line_id, display_name, role) values
  ('{ADMIN}', 'U-admin', 'Admin', 'admin'), ('{USER}', 'U-user', 'User', 'user');
insert into public.ingredients (id, name, benefits, functional_group) values
  ('{WATER}', 'Water', 'The base.', 'Solvent'), ('{GLYCERIN}', 'Glycerin', 'A humectant.', 'Humectant');
"""


@pytest.fixture
def db(pg):
    pg.check(SEED)
    return pg


def rows(db, sql):
    return db.check(f"select coalesce(jsonb_agg(to_jsonb(x)), '[]') from ({sql}) x;").json()


def submit(db, payload, edited=None):
    return db.check(
        "insert into public.product_submissions (submitted_by, payload, edited_payload) "
        f"values ('{USER}', {lit(json.dumps(payload))}::jsonb, "
        f"{lit(json.dumps(edited)) if edited is not None else 'NULL'}::jsonb) returning to_jsonb(id);").json()


def approve(db, submission_id, decisions):
    result = db.run(f"select public.approve_submission({lit(submission_id)}::uuid, '{ADMIN}'::uuid, "
                    f"{lit(json.dumps(decisions))}::jsonb, null);", role="service_role")
    assert result.ok, result.stderr
    return result.json()


# Every column the stage-2 code names in a select, filter, embed or order.
COLUMNS_THE_API_NAMES = {
    "products": ["id", "slug", "brand", "name", "category", "benefits", "good_for", "pao_months", "updated_at"],
    "product_ingredients": ["product_id", "ingredient_id", "position"],
    "ingredients": ["id", "name", "functional_group"],
    "product_submissions": ["id", "submitted_by", "status", "payload", "edited_payload", "created_at",
                            "updated_at", "reviewed_by", "reviewed_at", "review_notes", "product_id"],
    "users": ["id", "display_name", "role", "skin_type"],
}


def test_every_column_the_new_reads_name_exists_after_0013(db):
    """Finds every column the stage-2 selects, filters, embeds and orders name in the
    replica once 0013 has run."""
    found = rows(db, "select table_name, column_name from information_schema.columns where table_schema = 'public'")
    have = {(r["table_name"], r["column_name"]) for r in found}
    missing = [(t, c) for t, cols in COLUMNS_THE_API_NAMES.items() for c in cols if (t, c) not in have]
    assert missing == []


def test_the_embeds_the_new_reads_use_resolve_to_one_foreign_key_each(db):
    """product_submissions has exactly one foreign key to products (product_id), so the
    embed products(slug) is unambiguous, and its submitted_by column has its own foreign
    key to users, which the embed users!submitted_by(display_name) names."""
    fks = rows(db, """
        select c.conname, a.attname as column_name, c.confrelid::regclass::text as target
          from pg_constraint c
          join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
         where c.contype = 'f' and c.conrelid = 'public.product_submissions'::regclass""")
    to_products = [fk for fk in fks if fk["target"] == "products"]
    assert [fk["column_name"] for fk in to_products] == ["product_id"]
    assert ("submitted_by", "users") in {(fk["column_name"], fk["target"]) for fk in fks}


def test_pack_order_reading_lists_positions_in_order_then_nulls(db):
    """Ordering a product's product_ingredients the way the reads ask PostgREST to
    (position ascending, NULLs last) lists an approved product's ingredients in pack
    order, and puts a link without a position (as every pre-0013 link has) last."""
    sid = submit(db, {"name": "Gel", "brand": "Glow Lab", "category": "Moisturizers",
                      "ingredients": [{"ingredient_id": GLYCERIN}, {"new_name": "Tremella Extract"},
                                      {"ingredient_id": WATER}]})
    product_id = approve(db, sid, {"new_ingredients": [{"position": 1, "decision": "name_only"}]})["product_id"]
    db.check(f"insert into public.ingredients (id, name, benefits) values "
             f"('11111111-0000-0000-0000-00000000000f', 'Old Link', 'x');"
             f"insert into public.product_ingredients (product_id, ingredient_id) values "
             f"('{product_id}', '11111111-0000-0000-0000-00000000000f');")
    ordered = rows(db, f"""
        select i.name from public.product_ingredients pi join public.ingredients i on i.id = pi.ingredient_id
         where pi.product_id = '{product_id}' order by pi.position asc nulls last""")
    assert [r["name"] for r in ordered] == ["Glycerin", "Tremella Extract", "Water", "Old Link"]


MERGE_CASES = [
    ({"name": "A", "ingredients": [{"ingredient_id": WATER}], "note": "x"}, None),
    ({"name": "A", "ingredients": [{"ingredient_id": WATER}], "note": "x"},
     {"name": "B", "note": None, "ingredients": [{"new_name": "Y"}]}),
    ({"name": "A", "sources": [{"url": "https://a.example", "title": "A", "claims": ["listing"]}]},
     {"sources": []}),
    ({"name": "A", "ingredients": ["Water", "Mystery"]}, {"brand": "Z"}),
]


@pytest.mark.parametrize("payload, edited", MERGE_CASES, ids=["no edits", "replace and null", "emptied list", "legacy"])
def test_review_merge_equals_the_sql_merge(db, payload, edited):
    """merged_payload gives the same object as payload || edited_payload in Postgres,
    which approve_submission() reads: a top-level merge, a key in edited_payload
    replacing the user's whole value, null included."""
    sid = submit(db, payload, edited)
    sql = db.check(f"select payload || coalesce(edited_payload, '{{}}'::jsonb) from public.product_submissions "
                   f"where id = '{sid}';").json()
    assert merged_payload({"payload": payload, "edited_payload": edited}) == sql


def test_review_numbering_is_the_position_approve_expects(db):
    """The positions GET /submissions/admin/{id} gives the new ingredients of an edited
    submission are the ones approve_submission() reads: deciding name_only for the one
    numbered "Keep Me" and drop for "Drop Me" inserts Keep Me, not Drop Me, and links
    the product's ingredients in the edited list's order."""
    payload = {"name": "Gel", "brand": "Glow Lab", "category": "Moisturizers",
               "ingredients": [{"new_name": "User Name A"}, {"ingredient_id": WATER}]}
    edited = {"ingredients": [{"ingredient_id": GLYCERIN}, {"new_name": "Drop Me"},
                              {"ingredient_id": WATER}, {"new_name": "Keep Me"}]}
    sid = submit(db, payload, edited)
    review = review_ingredients(merged_payload({"payload": payload, "edited_payload": edited}),
                                [{"id": WATER, "name": "Water", "_key": "water"},
                                 {"id": GLYCERIN, "name": "Glycerin", "_key": "glycerin"}])
    position = {item["name"]: item["position"] for item in review if item["status"] == "new"}
    product_id = approve(db, sid, {"new_ingredients": [
        {"position": position["Keep Me"], "decision": "name_only"},
        {"position": position["Drop Me"], "decision": "drop"}]})["product_id"]
    linked = rows(db, f"""
        select i.name from public.product_ingredients pi join public.ingredients i on i.id = pi.ingredient_id
         where pi.product_id = '{product_id}' order by pi.position""")
    assert [r["name"] for r in linked] == ["Glycerin", "Water", "Keep Me"]
    assert rows(db, "select name from public.ingredients where name in ('Drop Me', 'User Name A')") == []
