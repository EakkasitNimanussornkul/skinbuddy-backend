"""Migration 0013 against a local Postgres replica: approve_submission(),
admin_update_product(), the unique brand+name index, the updated_at trigger,
the grants, and the SQL copies of the ingredient-name key and create_slug().

Skipped unless local PostgreSQL binaries and the (local-only) migration file are
present; see tests/postgres/pg_harness.py.
"""

import json

import pytest

from app.core.services.ingredient_names import INCI_ALIAS_MAP, ingredient_key
from app.core.utils import create_slug
from pg_harness import MIGRATION_0013, lit

ADMIN = "00000000-0000-0000-0000-0000000000a1"
USER = "00000000-0000-0000-0000-0000000000b1"

WATER = "11111111-0000-0000-0000-000000000001"
GLYCERIN = "11111111-0000-0000-0000-000000000002"
SODIUM_PCA = "11111111-0000-0000-0000-000000000003"
NIACINAMIDE = "11111111-0000-0000-0000-000000000004"
BETA_GLUCAN = "11111111-0000-0000-0000-000000000005"

SNAIL = "22222222-0000-0000-0000-000000000001"     # COSRX Advanced Snail 96 Mucin Power Essence
CLEANSER = "22222222-0000-0000-0000-000000000002"  # CeraVe Hydrating Facial Cleanser

CHECKED_SOURCE = "33333333-0000-0000-0000-000000000001"
CHECKED_URL = "https://checked.example/page"

TABLES = ["users", "products", "ingredients", "product_ingredients", "sources",
          "ingredient_sources", "product_sources", "product_submissions"]

SEED = f"""
truncate public.product_sources, public.ingredient_sources, public.product_ingredients,
         public.product_submissions, public.sources, public.products, public.ingredients,
         public.users cascade;
insert into public.users (id, line_id, display_name, role) values
  ('{ADMIN}', 'U-admin', 'Admin', 'admin'),
  ('{USER}',  'U-user',  'User',  'user');
insert into public.ingredients (id, name, benefits, good_for, bad_for, functional_group, awareness_tier, source) values
  ('{WATER}',       'Water',       'The base of the formula.',   'All Skin Types', 'None', 'Solvent',                 'low',    'curated'),
  ('{GLYCERIN}',    'Glycerin',    'A humectant.',               'Dry Skin (D)',   'None', 'Humectant',               'low',    'curated'),
  ('{SODIUM_PCA}',  'Sodium PCA',  'A natural moisturising factor.', 'Dehydrated Skin', 'None', 'Humectant',        'medium', 'curated'),
  ('{NIACINAMIDE}', 'Niacinamide', 'Vitamin B3.',                'Oily Skin (O)',  'None', 'Vitamin B3',              'high',   'curated'),
  ('{BETA_GLUCAN}', 'Beta-Glucan', 'A film-forming polysaccharide.', 'Dry Skin (D)', 'None', 'Skin-Conditioning Agent', 'medium', 'curated');
insert into public.products (id, brand, name, category, slug, price_thb, price_usd, image_url, description) values
  ('{SNAIL}',    'COSRX',  'Advanced Snail 96 Mucin Power Essence', 'Treatments', 'cosrx-advanced-snail-96-mucin-power-essence', 690, 25, 'https://img.example/snail.png', 'An essence.'),
  ('{CLEANSER}', 'CeraVe', 'Hydrating Facial Cleanser',             'Cleansers',  'cerave-hydrating-facial-cleanser',            450, 14, null, null);
insert into public.product_ingredients (product_id, ingredient_id) values
  ('{SNAIL}', '{WATER}'), ('{SNAIL}', '{GLYCERIN}'), ('{CLEANSER}', '{WATER}');
insert into public.sources (id, title, publisher, url, source_type, accessed_on, notes) values
  ('{CHECKED_SOURCE}', 'Checked source', 'A publisher', '{CHECKED_URL}', 'safety_review', '2026-09-30', 'opened and checked');
insert into public.product_sources (product_id, source_id, claim) values ('{SNAIL}', '{CHECKED_SOURCE}', 'listing');
"""


# --- helpers --------------------------------------------------------------------

@pytest.fixture
def db(pg):
    """The replica, reset to the seed rows before each test."""
    pg.check(SEED)
    return pg


def snapshot(db, tables=TABLES):
    parts = ", ".join(
        f"'{t}', (select coalesce(jsonb_agg(to_jsonb(x) order by to_jsonb(x)::text), '[]') from public.{t} x)"
        for t in tables)
    return db.check(f"select jsonb_build_object({parts});").json()


def scalar(db, sql):
    return db.check(sql).json()


def payload(**overrides):
    base = {
        "name": "Snow Mushroom Gel", "brand": "Glow Lab", "category": "Moisturizers",
        "image_path": "submissions/abc.png",
        "ingredients": [{"ingredient_id": WATER}],
        "price_thb": 590, "price_usd": None, "pao_months": 12,
        "benefits": [], "good_for": [], "sources": [], "note": None,
    }
    base.update(overrides)
    return base


def submit(db, body, *, edited=None, status="pending"):
    row = db.check(
        "insert into public.product_submissions (submitted_by, status, payload, edited_payload) "
        f"values ('{USER}', {lit(status)}, {lit(json.dumps(body))}::jsonb, "
        f"{lit(json.dumps(edited)) if edited is not None else 'NULL'}::jsonb) returning to_jsonb(id);")
    return row.json()


def approve(db, submission_id, decisions=None, *, image_url=None, admin=ADMIN, role="service_role"):
    decisions = {} if decisions is None else decisions
    return db.run(
        f"select public.approve_submission({lit(submission_id)}::uuid, {lit(admin)}::uuid, "
        f"{lit(json.dumps(decisions))}::jsonb, {lit(image_url)});", role=role)


def updated_at_of(db, product_id):
    """products.updated_at as PostgREST would return it: a JSON string."""
    return scalar(db, f"select to_jsonb(updated_at) from public.products where id = '{product_id}';")


def update(db, product_id, expected_updated_at, patch):
    return db.run(
        f"select public.admin_update_product({lit(product_id)}::uuid, {lit(expected_updated_at)}::timestamptz, "
        f"{lit(json.dumps(patch))}::jsonb);", role="service_role")


def rows(db, sql):
    return db.check(f"select coalesce(jsonb_agg(to_jsonb(x)), '[]') from ({sql}) x;").json()


def links_in_order(db, product_id):
    """The product's ingredient ids in the order they were inserted."""
    return scalar(db, "select coalesce(jsonb_agg(ingredient_id order by ctid), '[]') "
                      f"from public.product_ingredients where product_id = '{product_id}';")


def today(db):
    return scalar(db, "select to_jsonb(to_char(current_date, 'YYYY-MM-DD'));")


# --- migration ------------------------------------------------------------------

def test_migration_runs_twice_without_error(pg):
    """0013 applies twice in a row on the replica with no error, and the second run leaves
    each new column, the pao_months check, the unique index and both triggers exactly once."""
    assert [run.returncode for run in pg.migration_runs] == [0, 0], [r.stderr for r in pg.migration_runs]
    columns = rows(pg, "select table_name, column_name from information_schema.columns "
                       "where table_schema = 'public' and ((table_name = 'products' and column_name in "
                       "('benefits', 'good_for', 'pao_months', 'updated_at')) or (table_name = 'product_submissions' "
                       "and column_name in ('product_id', 'edited_payload', 'updated_at')))")
    assert len(columns) == 7
    assert scalar(pg, "select count(*) from pg_indexes where indexname = 'products_brand_name_lower_key';") == 1
    assert scalar(pg, "select count(*) from pg_constraint where conname = 'products_pao_months_check';") == 1
    assert scalar(pg, "select count(*) from pg_trigger where tgname like '%bump_updated_at';") == 2
    assert scalar(pg, "select count(*) from pg_proc where proname = 'approve_submission';") == 1


def test_migration_rerun_changes_no_data(db):
    """Running 0013 again on a database that already has products, ingredients, sources and
    submissions leaves every row exactly as it was."""
    submit(db, payload())
    before = snapshot(db)
    assert db.run_file(MIGRATION_0013).ok
    assert snapshot(db) == before


def test_migration_stops_on_existing_brand_name_duplicates(pg):
    """With two products differing only in letter case, 0013 stops with SBIDX, names the
    duplicate pair in its notice, and changes nothing (products gains no new column)."""
    pg.create_database("skinbuddy_guard")
    pg.check("insert into public.products (brand, name, category, slug) values "
             "('COSRX', 'Snail Essence', 'Treatments', 'cosrx-snail-essence'), "
             "('cosrx', 'SNAIL ESSENCE', 'Treatments', 'cosrx-snail-essence-2');", db="skinbuddy_guard")
    result = pg.run_file(MIGRATION_0013, db="skinbuddy_guard")
    assert result.sqlstate == "SBIDX"
    assert "cosrx / snail essence (2 rows" in result.stderr
    guard_columns = pg.check("select count(*) from information_schema.columns where table_schema = 'public' "
                             "and table_name = 'products' and column_name in ('benefits', 'updated_at');",
                             db="skinbuddy_guard").json()
    assert guard_columns == 0
    pg.check("drop database skinbuddy_guard;", db="postgres")


def test_unique_index_rejects_a_case_variant_duplicate(db):
    """Inserting a product whose brand and name differ from an existing one only in letter
    case fails with unique_violation (23505), even with a different slug."""
    result = db.run("insert into public.products (brand, name, category, slug) values "
                    "('cosrx', 'ADVANCED SNAIL 96 MUCIN POWER ESSENCE', 'Treatments', 'another-slug');")
    assert result.sqlstate == "23505"
    assert "products_brand_name_lower_key" in result.stderr


def test_updated_at_trigger_bumps_on_update(db):
    """An UPDATE moves products.updated_at forward to the time of the update, even when the
    statement itself tries to set an old value; product_submissions.updated_at is bumped too."""
    db.check(f"update public.products set updated_at = '2000-01-01' where id = '{SNAIL}';")
    first = scalar(db, f"select to_jsonb(updated_at) from public.products where id = '{SNAIL}';")
    assert not first.startswith("2000")
    db.check(f"update public.products set price_thb = 700 where id = '{SNAIL}';")
    assert scalar(db, f"select to_jsonb(updated_at > {lit(first)}::timestamptz) from public.products where id = '{SNAIL}';") is True

    sid = submit(db, payload())
    assert scalar(db, f"select to_jsonb(updated_at is null) from public.product_submissions where id = '{sid}';") is True
    db.check(f"update public.product_submissions set review_notes = 'x' where id = '{sid}';")
    assert scalar(db, f"select to_jsonb(updated_at is not null) from public.product_submissions where id = '{sid}';") is True


def test_pao_months_only_allows_6_12_or_24(db):
    """products.pao_months accepts 6, 12, 24 and NULL, and rejects 7 with check_violation (23514)."""
    for value in ("6", "12", "24", "null"):
        assert db.run(f"update public.products set pao_months = {value} where id = '{SNAIL}';").ok
    assert db.run(f"update public.products set pao_months = 7 where id = '{SNAIL}';").sqlstate == "23514"


def test_ingredient_benefits_may_be_null_after_migration(db):
    """ingredients.benefits accepts NULL after 0013 (the live column was NOT NULL)."""
    assert db.run("insert into public.ingredients (name) values ('Only A Name');").ok


FUNCTIONS = [
    "public.approve_submission(uuid, uuid, jsonb, text)",
    "public.admin_update_product(uuid, timestamptz, jsonb)",
    "public.ingredient_name_key(text)",
    "public.product_slug(text, text)",
    "public.sb_source_id_for_url(text, text, text, text, text)",
    "public.sb_bump_updated_at()",
]


@pytest.mark.parametrize("function", FUNCTIONS)
def test_functions_are_executable_by_service_role_only(pg, function):
    """Each new function is executable by service_role and not by anon, authenticated or
    PUBLIC, despite Supabase's default privileges granting EXECUTE to all three roles."""
    privileges = scalar(pg, "select jsonb_build_array("
                            f"has_function_privilege('anon', {lit(function)}, 'execute'), "
                            f"has_function_privilege('authenticated', {lit(function)}, 'execute'), "
                            f"has_function_privilege('service_role', {lit(function)}, 'execute'));")
    assert privileges == [False, False, True]
    public_grant = scalar(pg, f"select to_jsonb(exists (select 1 from aclexplode((select proacl from pg_proc "
                              f"where oid = {lit(function)}::regprocedure)) a where a.grantee = 0));")
    assert public_grant is False


def test_authenticated_role_cannot_call_approve(db):
    """Calling approve_submission as the authenticated role fails with insufficient_privilege
    (42501) and creates nothing."""
    sid = submit(db, payload())
    before = snapshot(db)
    result = approve(db, sid, role="authenticated")
    assert result.sqlstate == "42501"
    assert snapshot(db) == before


# --- the SQL copies of the Python helpers ---------------------------------------------

NAME_CASES = [
    "en:aqua", "Aqua", "Water (Aqua)", "(Aqua)", "Aqua *", "Sodium PCA", "sodium pca",
    "Beta-Glucan", "  Glycerol  ", "\tGlycerol\n", "VITAMIN C", "Alcool Cétylique", "Cholestérol",
    "CHOLESTÉROL", "en:sodium-hyaluronate", "Camellia Oleifera (Green Tea) Leaf Extract",
    "d-panthenol", "1,2-Hexanediol", "Tocopherol*", "EN:Aqua", "", "Centella Asiatica Extract",
]


def test_ingredient_name_key_matches_the_python_normaliser(pg):
    """public.ingredient_name_key() returns the same key as the shared Python ingredient_key()
    for every INCI alias and canonical name in the alias map, and for each edge case listed."""
    cases = sorted(set(NAME_CASES) | set(INCI_ALIAS_MAP) | set(INCI_ALIAS_MAP.values()))
    values = ", ".join(f"({i}, {lit(c)})" for i, c in enumerate(cases))
    sql_keys = scalar(pg, f"select jsonb_agg(public.ingredient_name_key(c) order by i) from (values {values}) v(i, c);")
    assert sql_keys == [ingredient_key(c) for c in cases]


SLUG_CASES = [
    ("COSRX", "Advanced Snail 96 Mucin Power Essence"), ("Bioré", "UV Aqua Rich Watery Essence SPF50+"),
    ("Paula's Choice", "Skin Perfecting 2% BHA Liquid Exfoliant"), ("The Ordinary", '"Buffet"'),
    ("  La Roche-Posay ", " Effaclar Duo(+) "), ("Some By Mi", "AHA-BHA-PHA 30 Days Miracle Toner"),
    ("", "Name Only"), ("ÉLÉMENT", "Crème"),
]


def test_product_slug_matches_create_slug(pg):
    """public.product_slug(brand, name) returns the same slug as create_slug(brand, name) for
    each case listed, including accented letters, quotes, symbols and outer spaces."""
    values = ", ".join(f"({i}, {lit(b)}, {lit(n)})" for i, (b, n) in enumerate(SLUG_CASES))
    sql_slugs = scalar(pg, f"select jsonb_agg(public.product_slug(b, n) order by i) from (values {values}) v(i, b, n);")
    assert sql_slugs == [create_slug(b, n) for b, n in SLUG_CASES]


# --- approve_submission -----------------------------------------------------------------

def test_approve_creates_the_product_and_links_ingredients_in_pack_order(db):
    """Approving creates one new product with the submitted brand, name, category, image URL,
    prices and pao_months and the create_slug slug; links known and kept new ingredients in
    pack order, skipping the dropped one; marks the submission approved with product_id,
    reviewed_by and reviewed_at; and returns the product_id and slug."""
    body = payload(ingredients=[
        {"ingredient_id": WATER},
        {"new_name": "Tremella Fuciformis Extract", "details": {"known_for": "hydrating"}},
        {"ingredient_id": NIACINAMIDE},
        {"new_name": "Mystery Peptide X"},
        {"new_name": "Dropped Thing"},
    ], price_usd=19.5)
    sid = submit(db, body)
    result = approve(db, sid, {"new_ingredients": [
        {"position": 1, "decision": "with_details", "functional_group": "Humectant", "benefits": "Holds water."},
        {"position": 3, "decision": "name_only"},
        {"position": 4, "decision": "drop"},
    ]}, image_url="https://img.example/submissions/abc.png")
    assert result.ok, result.stderr
    out = result.json()
    assert out["slug"] == "glow-lab-snow-mushroom-gel"

    product = rows(db, f"select brand, name, category, slug, image_url, price_thb, price_usd, pao_months, "
                       f"description, benefits, good_for from public.products where id = '{out['product_id']}'")
    assert product == [{"brand": "Glow Lab", "name": "Snow Mushroom Gel", "category": "Moisturizers",
                        "slug": "glow-lab-snow-mushroom-gel", "image_url": "https://img.example/submissions/abc.png",
                        "price_thb": 590, "price_usd": 19.5, "pao_months": 12, "description": None,
                        "benefits": None, "good_for": None}]

    tremella = scalar(db, "select to_jsonb(id) from public.ingredients where name = 'Tremella Fuciformis Extract';")
    peptide = scalar(db, "select to_jsonb(id) from public.ingredients where name = 'Mystery Peptide X';")
    assert links_in_order(db, out["product_id"]) == [WATER, tremella, NIACINAMIDE, peptide]
    assert scalar(db, "select count(*) from public.ingredients where name = 'Dropped Thing';") == 0

    submission = rows(db, f"select status, product_id, reviewed_by, reviewed_at is not null as reviewed, "
                          f"updated_at is not null as touched from public.product_submissions where id = '{sid}'")
    assert submission == [{"status": "approved", "product_id": out["product_id"], "reviewed_by": ADMIN,
                           "reviewed": True, "touched": True}]


def test_approve_never_updates_an_existing_ingredient_or_product(db):
    """Approving a submission whose new names normalise to existing ingredients, with
    with_details decisions carrying a different functional_group and benefits, and a ticked
    source whose url is an existing checked source, leaves every pre-existing products,
    ingredients and sources row byte-for-byte unchanged."""
    before = snapshot(db, ["products", "ingredients", "sources"])
    body = payload(ingredients=[
        {"new_name": "aqua", "details": {"known_for": "solvent", "source_url": CHECKED_URL}},
        {"new_name": "SODIUM PCA", "details": {"known_for": "moisture"}},
        {"ingredient_id": GLYCERIN},
    ], benefits=["Hydrating"], sources=[{"url": CHECKED_URL, "title": "A new title", "claims": ["listing"]}])
    sid = submit(db, body)
    result = approve(db, sid, {
        "publish_benefits": ["Hydrating"], "publish_source_urls": [CHECKED_URL],
        "new_ingredients": [
            {"position": 0, "decision": "with_details", "functional_group": "Humectant", "benefits": "Overwritten?"},
            {"position": 1, "decision": "with_details", "functional_group": "Emollient", "benefits": "Overwritten?"},
        ]})
    assert result.ok, result.stderr
    after = snapshot(db, ["products", "ingredients", "sources"])
    for table in ("products", "ingredients", "sources"):
        for row in before[table]:
            assert row in after[table], f"{table} row changed: {row}"
    assert len(after["ingredients"]) == len(before["ingredients"])
    assert len(after["sources"]) == len(before["sources"])
    assert len(after["products"]) == len(before["products"]) + 1


@pytest.mark.parametrize("brand, name", [
    ("cosrx", "advanced snail 96 mucin power essence"),
    ("COSRX ", " ADVANCED SNAIL 96 MUCIN POWER ESSENCE"),
])
def test_approve_answers_duplicate_on_a_case_insensitive_brand_and_name_match(db, brand, name):
    """Approving a submission whose brand and name match an existing product case-insensitively
    fails with SBDUP, message 'duplicate', a DETAIL listing that product's id, slug, brand and
    name, and changes nothing."""
    sid = submit(db, payload(brand=brand, name=name))
    before = snapshot(db)
    result = approve(db, sid)
    assert result.sqlstate == "SBDUP"
    assert result.message == "duplicate"
    assert json.loads(result.detail) == [{"id": SNAIL, "slug": "cosrx-advanced-snail-96-mucin-power-essence",
                                          "brand": "COSRX", "name": "Advanced Snail 96 Mucin Power Essence"}]
    assert snapshot(db) == before


def test_approve_answers_duplicate_when_only_the_slug_matches(db):
    """A brand and name that differ from an existing product's but give the same slug
    ("Power-Essence" vs "Power Essence") fail with SBDUP naming that product."""
    sid = submit(db, payload(brand="COSRX", name="Advanced Snail 96 Mucin Power-Essence"))
    result = approve(db, sid)
    assert result.sqlstate == "SBDUP"
    assert [c["id"] for c in json.loads(result.detail)] == [SNAIL]


BIORE_SQL = ("insert into public.products (brand, name, category, slug) values "
             "('Bioré', 'UV Aqua Rich Watery Essence SPF50+', 'Sun Care', 'biore-uv-aqua-rich-watery-essence-spf50') "
             "returning to_jsonb(id);")


def test_approve_answers_duplicate_when_the_existing_slug_is_hand_written(db):
    """A case variant of a product whose stored slug was written by hand (live: Bioré's
    'biore-...', where create_slug gives 'bior-...') still fails with SBDUP naming that
    product: the brand+name check does not rely on the slugs matching."""
    biore = db.check(BIORE_SQL).json()
    sid = submit(db, payload(brand="bioré", name="uv aqua rich watery essence spf50+"))
    result = approve(db, sid)
    assert result.sqlstate == "SBDUP"
    assert [c["id"] for c in json.loads(result.detail)] == [biore]


def test_new_name_matching_an_existing_ingredient_after_normalisation_links_it(db):
    """New names "en:Aqua", "sodium pca", "Beta Glucan" and "Glycerol" match Water, Sodium PCA,
    Beta-Glucan and Glycerin after normalisation, so approve links those four rows and
    inserts no ingredient."""
    count_before = scalar(db, "select count(*) from public.ingredients;")
    names = ["en:Aqua", "sodium pca", "Beta Glucan", "Glycerol"]
    sid = submit(db, payload(ingredients=[{"new_name": n} for n in names]))
    result = approve(db, sid, {"new_ingredients": [
        {"position": i, "decision": "with_details", "functional_group": "Humectant"} for i in range(4)]})
    assert result.ok, result.stderr
    assert scalar(db, "select count(*) from public.ingredients;") == count_before
    assert links_in_order(db, result.json()["product_id"]) == [WATER, SODIUM_PCA, BETA_GLUCAN, GLYCERIN]


def test_new_name_matching_two_ingredients_is_refused(db):
    """When a new name matches two ingredients rows after normalisation, approve fails with
    SBAMB, a DETAIL listing both rows, and changes nothing."""
    db.check("insert into public.ingredients (name, benefits) values ('Sodium Hydroxide', 'a'), ('sodium hydroxide', 'b');")
    sid = submit(db, payload(ingredients=[{"ingredient_id": WATER}, {"new_name": "Sodium-Hydroxide"}]))
    before = snapshot(db)
    result = approve(db, sid, {"new_ingredients": [{"position": 1, "decision": "name_only"}]})
    assert result.sqlstate == "SBAMB"
    assert sorted(m["name"] for m in json.loads(result.detail)) == ["Sodium Hydroxide", "sodium hydroxide"]
    assert snapshot(db) == before


def test_with_details_inserts_profile_fields_only_and_links_a_ticked_source(db):
    """with_details inserts a row with the typed name, the admin's functional_group and
    benefits, NULL good_for and bad_for, source "user submission <id>, approved by admin
    <date>", and links the user's ticked source_url in ingredient_sources with claim 'benefits'."""
    url = "https://brand.example/tremella"
    sid = submit(db, payload(ingredients=[
        {"new_name": "  Tremella Fuciformis Extract ", "details": {"known_for": "user text", "source_url": url}}]))
    result = approve(db, sid, {"publish_source_urls": [url], "new_ingredients": [
        {"position": 0, "decision": "with_details", "functional_group": "Humectant", "benefits": "Admin text."}]})
    assert result.ok, result.stderr
    row = rows(db, "select name, functional_group, benefits, good_for, bad_for, source, awareness_tier "
                   "from public.ingredients where name like 'Tremella%'")
    assert row == [{"name": "Tremella Fuciformis Extract", "functional_group": "Humectant",
                    "benefits": "Admin text.", "good_for": None, "bad_for": None,
                    "source": f"user submission {sid}, approved by admin {today(db)}", "awareness_tier": "medium"}]
    link = rows(db, "select s.url, s.title, s.source_type, l.claim from public.ingredient_sources l "
                    "join public.sources s on s.id = l.source_id join public.ingredients i on i.id = l.ingredient_id "
                    "where i.name = 'Tremella Fuciformis Extract'")
    assert link == [{"url": url, "title": "User-submitted source for Tremella Fuciformis Extract",
                     "source_type": "product_database", "claim": "benefits"}]


def test_with_details_does_not_link_an_unticked_source(db):
    """A with_details ingredient whose source_url the admin did not tick gets no sources row
    and no ingredient_sources link."""
    url = "https://brand.example/unticked"
    sid = submit(db, payload(ingredients=[{"new_name": "Tremella", "details": {"source_url": url}}]))
    assert approve(db, sid, {"new_ingredients": [{"position": 0, "decision": "with_details"}]}).ok
    assert scalar(db, f"select count(*) from public.sources where url = {lit(url)};") == 0
    assert scalar(db, "select count(*) from public.ingredient_sources;") == 0


def test_name_only_inserts_the_name_and_nothing_else(db):
    """name_only inserts a row holding only the name: benefits, functional_group, good_for,
    bad_for and source are all NULL, and no ingredient source is linked."""
    url = "https://brand.example/x"
    sid = submit(db, payload(ingredients=[{"new_name": "Mystery Peptide X",
                                           "details": {"known_for": "firming", "source_url": url}}]))
    result = approve(db, sid, {"publish_source_urls": [url], "new_ingredients": [
        {"position": 0, "decision": "name_only", "functional_group": "Peptide", "benefits": "ignored"}]})
    assert result.ok, result.stderr
    row = rows(db, "select benefits, functional_group, good_for, bad_for, source from public.ingredients "
                   "where name = 'Mystery Peptide X'")
    assert row == [{"benefits": None, "functional_group": None, "good_for": None, "bad_for": None, "source": None}]
    assert scalar(db, "select count(*) from public.ingredient_sources;") == 0


def test_drop_inserts_and_links_nothing_for_that_ingredient(db):
    """drop skips the ingredient: no ingredients row is inserted and the product links only
    the other ingredients."""
    count_before = scalar(db, "select count(*) from public.ingredients;")
    sid = submit(db, payload(ingredients=[{"ingredient_id": WATER}, {"new_name": "Dropped Thing"}]))
    result = approve(db, sid, {"new_ingredients": [{"position": 1, "decision": "drop"}]})
    assert result.ok, result.stderr
    assert scalar(db, "select count(*) from public.ingredients;") == count_before
    assert links_in_order(db, result.json()["product_id"]) == [WATER]


def test_only_ticked_benefits_tags_and_sources_are_published(db):
    """Only the ticked benefits and good_for tags are stored on the product, in the
    submission's order, and only the ticked source is inserted and linked with its claims;
    the unticked source gets no sources row."""
    body = payload(
        benefits=["Hydrating", "Soothing", "Brightening"], good_for=["Dry skin", "Oily", "Redness"],
        sources=[{"url": "https://shop.example/a", "title": "Shop A", "claims": ["listing", "price"]},
                 {"url": "https://shop.example/b", "title": "Shop B", "claims": ["image"]}])
    sid = submit(db, body)
    result = approve(db, sid, {"publish_benefits": ["Brightening", "Hydrating"], "publish_good_for": ["Redness"],
                               "publish_source_urls": ["https://shop.example/a"]})
    assert result.ok, result.stderr
    pid = result.json()["product_id"]
    assert rows(db, f"select benefits, good_for from public.products where id = '{pid}'") == [
        {"benefits": ["Hydrating", "Brightening"], "good_for": ["Redness"]}]
    links = rows(db, f"select s.url, s.title, s.source_type, l.claim from public.product_sources l "
                     f"join public.sources s on s.id = l.source_id where l.product_id = '{pid}' order by l.claim")
    assert links == [{"url": "https://shop.example/a", "title": "Shop A", "source_type": "product_database", "claim": "listing"},
                     {"url": "https://shop.example/a", "title": "Shop A", "source_type": "product_database", "claim": "price"}]
    assert scalar(db, "select count(*) from public.sources where url = 'https://shop.example/b';") == 0


def test_nothing_ticked_stores_null_lists_and_no_sources(db):
    """With nothing ticked, the product's benefits and good_for are NULL and it has no
    product_sources rows."""
    sid = submit(db, payload(benefits=["Hydrating"], good_for=["Oily"],
                             sources=[{"url": "https://shop.example/a", "title": "A", "claims": ["listing"]}]))
    result = approve(db, sid, {})
    assert result.ok, result.stderr
    pid = result.json()["product_id"]
    assert rows(db, f"select benefits, good_for from public.products where id = '{pid}'") == [
        {"benefits": None, "good_for": None}]
    assert scalar(db, f"select count(*) from public.product_sources where product_id = '{pid}';") == 0


def test_an_existing_source_url_is_reused_without_being_changed(db):
    """A ticked source whose url already exists links the existing sources row, which keeps
    its own title, type and notes; no second sources row is made."""
    sid = submit(db, payload(sources=[{"url": CHECKED_URL, "title": "User's title", "claims": ["price"]}]))
    result = approve(db, sid, {"publish_source_urls": [CHECKED_URL]})
    assert result.ok, result.stderr
    assert rows(db, "select id, title, source_type, notes from public.sources") == [
        {"id": CHECKED_SOURCE, "title": "Checked source", "source_type": "safety_review", "notes": "opened and checked"}]
    assert rows(db, f"select source_id, claim from public.product_sources where product_id = "
                    f"'{result.json()['product_id']}'") == [{"source_id": CHECKED_SOURCE, "claim": "price"}]


@pytest.mark.parametrize("decisions", [
    {"publish_benefits": ["Invented"]},
    {"publish_good_for": ["Oily"]},
    {"publish_source_urls": ["https://not-in-the-submission.example"]},
])
def test_a_ticked_item_the_submission_does_not_contain_is_refused(db, decisions):
    """Ticking a benefit, tag or url the submission does not contain fails with SBVAL and
    changes nothing."""
    sid = submit(db, payload(benefits=["Hydrating"]))
    before = snapshot(db)
    result = approve(db, sid, decisions)
    assert result.sqlstate == "SBVAL"
    assert snapshot(db) == before


@pytest.mark.parametrize("decisions", [
    {},
    {"new_ingredients": [{"position": 1, "decision": "keep"}]},
    {"new_ingredients": [{"position": 1}]},
    {"new_ingredients": [{"position": 1, "decision": "drop"}, {"position": 1, "decision": "name_only"}]},
    {"new_ingredients": [{"position": 0, "decision": "drop"}, {"position": 1, "decision": "name_only"}]},
    {"new_ingredients": [{"position": 9, "decision": "drop"}, {"position": 1, "decision": "name_only"}]},
])
def test_a_missing_or_invalid_decision_is_refused(db, decisions):
    """A new ingredient with no decision, an unknown decision, two decisions for one
    position, or a decision for a position that is not a new name fails with SBDEC and
    changes nothing."""
    sid = submit(db, payload(ingredients=[{"ingredient_id": WATER}, {"new_name": "Tremella"}]))
    before = snapshot(db)
    result = approve(db, sid, decisions)
    assert result.sqlstate == "SBDEC", result.stderr
    assert snapshot(db) == before


def test_every_ingredient_dropped_is_refused(db):
    """When every ingredient is a new name and every decision is drop, approve fails with
    SBNON and changes nothing."""
    sid = submit(db, payload(ingredients=[{"new_name": "A Thing"}, {"new_name": "B Thing"}]))
    before = snapshot(db)
    result = approve(db, sid, {"new_ingredients": [{"position": 0, "decision": "drop"},
                                                   {"position": 1, "decision": "drop"}]})
    assert result.sqlstate == "SBNON"
    assert snapshot(db) == before


def test_a_legacy_payload_is_refused_until_edited(db):
    """A submission in the old shape (ingredients as a list of names) fails with SBLEG; once an
    admin's edited_payload gives new-shape ingredients, the same submission approves."""
    legacy = {"name": "Old Gel", "brand": "Old Brand", "category": "Moisturizers",
              "ingredients": ["Water", "Glycerin"], "description": "old"}
    sid = submit(db, legacy)
    result = approve(db, sid)
    assert result.sqlstate == "SBLEG"
    assert "old format" in result.message

    db.check(f"update public.product_submissions set edited_payload = "
             f"{lit(json.dumps({'ingredients': [{'ingredient_id': WATER}, {'new_name': 'Glycerin'}]}))}::jsonb "
             f"where id = '{sid}';")
    result = approve(db, sid, {"new_ingredients": [{"position": 1, "decision": "name_only"}]})
    assert result.ok, result.stderr
    assert links_in_order(db, result.json()["product_id"]) == [WATER, GLYCERIN]


def test_edited_payload_fields_replace_the_users_fields(db):
    """A field in edited_payload replaces the same field from the user's payload: the product
    takes the admin's corrected name and price, and the user's other fields."""
    sid = submit(db, payload(name="Snow Mushrom Gell"), edited={"name": "Snow Mushroom Gel", "price_thb": 650})
    result = approve(db, sid)
    assert result.ok, result.stderr
    assert rows(db, f"select name, brand, price_thb from public.products where id = '{result.json()['product_id']}'") == [
        {"name": "Snow Mushroom Gel", "brand": "Glow Lab", "price_thb": 650}]


def test_approve_rolls_back_everything_when_a_later_ingredient_fails(db):
    """When the third ingredient's functional_group does not exist, after the product, its
    ticked source and an earlier new ingredient were already inserted, approve fails with
    SBFGR and every table is exactly as before; the submission stays pending."""
    sid = submit(db, payload(
        ingredients=[{"ingredient_id": WATER}, {"new_name": "Tremella"}, {"new_name": "Second New"}],
        benefits=["Hydrating"],
        sources=[{"url": "https://shop.example/a", "title": "A", "claims": ["listing"]}]))
    before = snapshot(db)
    result = approve(db, sid, {"publish_benefits": ["Hydrating"], "publish_source_urls": ["https://shop.example/a"],
                               "new_ingredients": [
                                   {"position": 1, "decision": "with_details", "functional_group": "Humectant"},
                                   {"position": 2, "decision": "with_details", "functional_group": "Not A Group"}]})
    assert result.sqlstate == "SBFGR"
    assert snapshot(db) == before


def test_approve_rolls_back_everything_on_a_constraint_failure(db):
    """When a ticked source carries a claim the product_sources check does not allow, the
    insert fails with check_violation (23514) after the product and source rows were written,
    and every table is exactly as before."""
    sid = submit(db, payload(sources=[{"url": "https://shop.example/a", "title": "A", "claims": ["listing", "warranty"]}]))
    before = snapshot(db)
    result = approve(db, sid, {"publish_source_urls": ["https://shop.example/a"]})
    assert result.sqlstate == "23514"
    assert snapshot(db) == before


def test_approving_a_submission_that_is_not_pending_is_refused(db):
    """Approving an already-approved or a rejected submission fails with SBNPD and changes
    nothing."""
    sid = submit(db, payload())
    assert approve(db, sid).ok
    before = snapshot(db)
    assert approve(db, sid).sqlstate == "SBNPD"
    rejected = submit(db, payload(name="Other"), status="rejected")
    assert approve(db, rejected).sqlstate == "SBNPD"
    after = snapshot(db)
    assert after["products"] == before["products"]


def test_approve_by_a_non_admin_is_refused(db):
    """approve_submission with a reviewer whose users.role is 'user' fails with SBADM."""
    sid = submit(db, payload())
    assert approve(db, sid, admin=USER).sqlstate == "SBADM"


def test_approve_with_an_unknown_ingredient_id_is_refused(db):
    """An ingredient_id that is not in ingredients fails with SBUNK, its DETAIL naming the id."""
    missing = "11111111-0000-0000-0000-00000000dead"
    sid = submit(db, payload(ingredients=[{"ingredient_id": missing}]))
    result = approve(db, sid)
    assert result.sqlstate == "SBUNK"
    assert json.loads(result.detail) == [missing]


def test_approve_of_a_missing_submission_is_refused(db):
    """Approving a submission id that does not exist fails with SBNFD."""
    assert approve(db, "44444444-0000-0000-0000-000000000000").sqlstate == "SBNFD"


def test_approve_without_prices_stores_null_not_the_column_defaults(db):
    """A submission without prices creates a product with NULL price_thb and price_usd, not
    the columns' legacy defaults of 450 and 14.0."""
    sid = submit(db, payload(price_thb=None, price_usd=None, pao_months=None))
    result = approve(db, sid)
    assert result.ok, result.stderr
    assert rows(db, f"select price_thb, price_usd, pao_months from public.products "
                    f"where id = '{result.json()['product_id']}'") == [
        {"price_thb": None, "price_usd": None, "pao_months": None}]


def test_approve_with_an_invalid_pao_is_refused(db):
    """A submission with pao_months 7 fails with SBVAL."""
    sid = submit(db, payload(pao_months=7))
    assert approve(db, sid).sqlstate == "SBVAL"


# --- admin_update_product ---------------------------------------------------------------

def test_update_with_a_stale_updated_at_is_refused(db):
    """A PATCH carrying an updated_at other than the product's fails with SBSTL, message
    'stale', the current updated_at in DETAIL, and changes nothing."""
    current = updated_at_of(db, SNAIL)
    before = snapshot(db)
    result = update(db, SNAIL, "2000-01-01T00:00:00+00:00", {"name": "Renamed"})
    assert result.sqlstate == "SBSTL"
    assert result.message == "stale"
    assert json.loads(result.detail) == current
    assert snapshot(db) == before


def test_update_returns_the_new_updated_at_and_the_old_value_becomes_stale(db):
    """A successful PATCH returns the product's new updated_at, later than the one sent;
    sending the old value again then fails with SBSTL."""
    old = updated_at_of(db, SNAIL)
    result = update(db, SNAIL, old, {"price_thb": 720})
    assert result.ok, result.stderr
    out = result.json()
    assert out["product_id"] == SNAIL
    assert scalar(db, f"select to_jsonb({lit(out['updated_at'])}::timestamptz > {lit(old)}::timestamptz);") is True
    assert scalar(db, f"select to_jsonb(updated_at = {lit(out['updated_at'])}::timestamptz) "
                      f"from public.products where id = '{SNAIL}';") is True
    assert update(db, SNAIL, old, {"price_thb": 730}).sqlstate == "SBSTL"


def test_update_replaces_ingredients_and_sources_and_never_deletes_sources(db):
    """A PATCH with ingredients and sources replaces the product's product_ingredients and
    product_sources rows exactly; a new name matching an existing ingredient links it, an
    unknown new name is inserted name-only; the unlinked sources row still exists."""
    sources_before = scalar(db, "select count(*) from public.sources;")
    result = update(db, SNAIL, updated_at_of(db, SNAIL), {
        "ingredients": [{"ingredient_id": NIACINAMIDE}, {"new_name": "Glycerol"}, {"new_name": "Brand New Thing"}],
        "sources": [{"url": "https://shop.example/new", "title": "New shop", "publisher": "Shop",
                     "claims": ["price", "image"]}]})
    assert result.ok, result.stderr
    new_id = scalar(db, "select to_jsonb(id) from public.ingredients where name = 'Brand New Thing';")
    assert links_in_order(db, SNAIL) == [NIACINAMIDE, GLYCERIN, new_id]
    assert rows(db, "select benefits, functional_group, good_for, bad_for, source from public.ingredients "
                    "where name = 'Brand New Thing'") == [
        {"benefits": None, "functional_group": None, "good_for": None, "bad_for": None, "source": None}]
    assert rows(db, f"select s.url, s.publisher, l.claim from public.product_sources l join public.sources s "
                    f"on s.id = l.source_id where l.product_id = '{SNAIL}' order by l.claim") == [
        {"url": "https://shop.example/new", "publisher": "Shop", "claim": "image"},
        {"url": "https://shop.example/new", "publisher": "Shop", "claim": "price"}]
    assert scalar(db, f"select count(*) from public.sources where id = '{CHECKED_SOURCE}';") == 1
    assert scalar(db, "select count(*) from public.sources;") == sources_before + 1
    # The other product's links are untouched.
    assert links_in_order(db, CLEANSER) == [WATER]


def test_update_with_an_empty_sources_list_unlinks_every_source(db):
    """sources: [] removes all of the product's product_sources rows and deletes no sources row."""
    result = update(db, SNAIL, updated_at_of(db, SNAIL), {"sources": []})
    assert result.ok, result.stderr
    assert scalar(db, f"select count(*) from public.product_sources where product_id = '{SNAIL}';") == 0
    assert scalar(db, "select count(*) from public.sources;") == 1


def test_update_regenerates_the_slug_when_the_name_changes(db):
    """Renaming a product sets its slug to create_slug(brand, new name) and returns it; the
    old slug no longer exists."""
    result = update(db, SNAIL, updated_at_of(db, SNAIL), {"name": "Snail 92 All In One Cream"})
    assert result.ok, result.stderr
    assert result.json()["slug"] == create_slug("COSRX", "Snail 92 All In One Cream")
    assert scalar(db, f"select to_jsonb(slug) from public.products where id = '{SNAIL}';") == "cosrx-snail-92-all-in-one-cream"
    assert scalar(db, "select count(*) from public.products where slug = 'cosrx-advanced-snail-96-mucin-power-essence';") == 0


def test_update_keeps_a_hand_written_slug_when_brand_and_name_do_not_change(db):
    """A PATCH that changes neither brand nor name keeps the stored slug, even one that
    create_slug would not produce."""
    db.check(f"update public.products set slug = 'hand-written-slug' where id = '{CLEANSER}';")
    result = update(db, CLEANSER, updated_at_of(db, CLEANSER), {"price_thb": 500})
    assert result.ok, result.stderr
    assert result.json()["slug"] == "hand-written-slug"


def test_update_leaves_absent_fields_and_lists_unchanged(db):
    """A PATCH with only price_thb changes price_thb and updated_at and nothing else: the other
    columns, ingredient links and source links are as before."""
    before = snapshot(db)
    assert update(db, SNAIL, updated_at_of(db, SNAIL), {"price_thb": 700}).ok
    after = snapshot(db)
    for table in TABLES:
        if table != "products":
            assert after[table] == before[table], table
    old = next(p for p in before["products"] if p["id"] == SNAIL)
    new = next(p for p in after["products"] if p["id"] == SNAIL)
    assert {k for k in old if old[k] != new[k]} == {"price_thb", "updated_at"}


def test_update_sets_benefits_good_for_and_pao(db):
    """A PATCH stores benefits, good_for and pao_months as given, and an empty list as NULL."""
    result = update(db, SNAIL, updated_at_of(db, SNAIL),
                    {"benefits": ["Repairing"], "good_for": [], "pao_months": 6, "description": None})
    assert result.ok, result.stderr
    assert rows(db, f"select benefits, good_for, pao_months, description from public.products where id = '{SNAIL}'") == [
        {"benefits": ["Repairing"], "good_for": None, "pao_months": 6, "description": None}]


def test_update_colliding_with_another_product_is_refused(db):
    """Renaming a product to another product's brand and name, in different letter case, fails
    with SBDUP naming the other product, and changes nothing."""
    before = snapshot(db)
    result = update(db, CLEANSER, updated_at_of(db, CLEANSER),
                    {"brand": "cosrx", "name": "advanced snail 96 mucin power essence"})
    assert result.sqlstate == "SBDUP"
    assert [c["id"] for c in json.loads(result.detail)] == [SNAIL]
    assert snapshot(db) == before


def test_update_colliding_by_slug_or_with_a_hand_written_slug_is_refused(db):
    """Renaming a product so its slug equals another product's, or to a case variant of a
    product whose slug was written by hand, fails with SBDUP naming that product."""
    result = update(db, CLEANSER, updated_at_of(db, CLEANSER),
                    {"brand": "COSRX", "name": "Advanced Snail 96 Mucin Power-Essence"})
    assert result.sqlstate == "SBDUP"
    assert [c["id"] for c in json.loads(result.detail)] == [SNAIL]
    biore = db.check(BIORE_SQL).json()
    result = update(db, CLEANSER, updated_at_of(db, CLEANSER),
                    {"brand": "bioré", "name": "uv aqua rich watery essence spf50+"})
    assert result.sqlstate == "SBDUP"
    assert [c["id"] for c in json.loads(result.detail)] == [biore]


def test_update_rolls_back_everything_when_a_later_step_fails(db):
    """When the second source has a source_type the sources check does not allow, after the
    product row, its ingredient links and the first source were already changed, the PATCH
    fails with check_violation (23514) and every table is exactly as before."""
    before = snapshot(db)
    result = update(db, SNAIL, updated_at_of(db, SNAIL), {
        "name": "Renamed", "ingredients": [{"new_name": "Brand New Thing"}],
        "sources": [{"url": "https://shop.example/ok", "title": "OK", "claims": ["listing"]},
                    {"url": "https://shop.example/bad", "title": "Bad", "source_type": "website", "claims": ["listing"]}]})
    assert result.sqlstate == "23514"
    assert snapshot(db) == before


def test_update_with_an_ambiguous_new_name_is_refused(db):
    """A PATCH whose new ingredient name matches two ingredients rows fails with SBAMB and
    changes nothing."""
    db.check("insert into public.ingredients (name, benefits) values ('Sodium Hydroxide', 'a'), ('Sodium hydroxide', 'b');")
    before = snapshot(db)
    result = update(db, SNAIL, updated_at_of(db, SNAIL), {"ingredients": [{"new_name": "sodium hydroxide"}]})
    assert result.sqlstate == "SBAMB"
    assert snapshot(db) == before


@pytest.mark.parametrize("patch, code", [
    ({"ingredients": []}, "SBNON"),
    ({"ingredients": [{"ingredient_id": "11111111-0000-0000-0000-00000000dead"}]}, "SBUNK"),
    ({"pao_months": 7}, "SBVAL"),
    ({"price_thb": -1}, "SBVAL"),
    ({"price_usd": "cheap"}, "SBVAL"),
    ({"name": "  "}, "SBVAL"),
])
def test_update_with_invalid_input_is_refused(db, patch, code):
    """An empty ingredients list (SBNON), an unknown ingredient_id (SBUNK), a pao_months of 7,
    a negative or non-numeric price, or a blank name (SBVAL) is refused and changes nothing."""
    before = snapshot(db)
    assert update(db, SNAIL, updated_at_of(db, SNAIL), patch).sqlstate == code
    assert snapshot(db) == before


def test_update_of_a_missing_product_is_refused(db):
    """A PATCH for a product id that does not exist fails with SBNFD."""
    assert update(db, "22222222-0000-0000-0000-00000000dead", "2026-01-01T00:00:00+00:00", {}).sqlstate == "SBNFD"



# --- product_ingredients.position (pack order) -----------------------------------------
# Appended at the end so earlier Test Record case numbers do not shift.

def positions(db, product_id):
    """The product's links as [ingredient_id, position] pairs, in position order."""
    return scalar(db, "select coalesce(jsonb_agg(jsonb_build_array(ingredient_id, position) "
                      "order by position nulls last, ctid), '[]') "
                      f"from public.product_ingredients where product_id = '{product_id}';")


def test_migration_adds_position_once_with_its_check_and_unique_index(pg):
    """After 0013 has run twice, product_ingredients has one nullable integer position
    column, one position >= 0 check and one unique index on (product_id, position)."""
    column = rows(pg, "select data_type, is_nullable from information_schema.columns where table_schema = 'public' "
                      "and table_name = 'product_ingredients' and column_name = 'position'")
    assert column == [{"data_type": "integer", "is_nullable": "YES"}]
    assert scalar(pg, "select count(*) from pg_constraint where conname = 'product_ingredients_position_check';") == 1
    assert scalar(pg, "select count(*) from pg_indexes where indexname = 'product_ingredients_product_position_key';") == 1


def test_links_that_exist_before_the_migration_keep_a_null_position(pg):
    """Ingredient links that exist when 0013 runs keep position NULL after it runs, and
    after it runs a second time: there is no backfill."""
    pg.create_database("skinbuddy_positions")
    pg.check("insert into public.products (id, brand, name, category, slug) values "
             f"('{SNAIL}', 'COSRX', 'Snail', 'Treatments', 'cosrx-snail');"
             f"insert into public.ingredients (id, name, benefits) values ('{WATER}', 'Water', 'b'), ('{GLYCERIN}', 'Glycerin', 'b');"
             f"insert into public.product_ingredients (product_id, ingredient_id) values ('{SNAIL}', '{WATER}'), ('{SNAIL}', '{GLYCERIN}');",
             db="skinbuddy_positions")
    for _ in range(2):
        assert pg.run_file(MIGRATION_0013, db="skinbuddy_positions").ok
        assert pg.check("select count(*) from public.product_ingredients where position is null;",
                        db="skinbuddy_positions").json() == 2
    pg.check("drop database skinbuddy_positions;", db="postgres")


def test_approve_numbers_positions_in_pack_order_with_no_gaps(db):
    """approve writes position 0, 1, 2, 3 to the linked ingredients in pack order: a
    dropped ingredient and a repeat of one already linked (a known id listed twice, or
    a new name that resolves to a linked row) take no number, so there are no gaps."""
    sid = submit(db, payload(ingredients=[
        {"ingredient_id": WATER},
        {"new_name": "Dropped Thing"},
        {"ingredient_id": NIACINAMIDE},
        {"ingredient_id": WATER},
        {"new_name": "aqua"},
        {"new_name": "Glycerol"},
        {"new_name": "Tremella"},
    ]))
    result = approve(db, sid, {"new_ingredients": [
        {"position": 1, "decision": "drop"},
        {"position": 4, "decision": "name_only"},
        {"position": 5, "decision": "name_only"},
        {"position": 6, "decision": "with_details", "functional_group": "Humectant"},
    ]})
    assert result.ok, result.stderr
    tremella = scalar(db, "select to_jsonb(id) from public.ingredients where name = 'Tremella';")
    assert positions(db, result.json()["product_id"]) == [
        [WATER, 0], [NIACINAMIDE, 1], [GLYCERIN, 2], [tremella, 3]]


def test_update_rewrites_positions_in_the_new_order(db):
    """A PATCH that replaces the ingredient list numbers the new links 0, 1, 2 in the
    order given, skipping a repeat; a second PATCH with the order reversed renumbers
    them in the reversed order."""
    result = update(db, SNAIL, updated_at_of(db, SNAIL), {"ingredients": [
        {"ingredient_id": GLYCERIN}, {"new_name": "aqua"}, {"ingredient_id": NIACINAMIDE}, {"ingredient_id": GLYCERIN}]})
    assert result.ok, result.stderr
    assert positions(db, SNAIL) == [[GLYCERIN, 0], [WATER, 1], [NIACINAMIDE, 2]]
    result = update(db, SNAIL, result.json()["updated_at"], {"ingredients": [
        {"ingredient_id": NIACINAMIDE}, {"ingredient_id": WATER}, {"ingredient_id": GLYCERIN}]})
    assert result.ok, result.stderr
    assert positions(db, SNAIL) == [[NIACINAMIDE, 0], [WATER, 1], [GLYCERIN, 2]]


def test_update_without_an_ingredient_list_leaves_positions_alone(db):
    """A PATCH without "ingredients" leaves every link and its position (NULL for the
    seeded rows) as it was."""
    before = positions(db, SNAIL)
    assert update(db, SNAIL, updated_at_of(db, SNAIL), {"price_thb": 700}).ok
    assert positions(db, SNAIL) == before == [[WATER, None], [GLYCERIN, None]]


def test_two_links_cannot_share_a_position_and_a_position_cannot_be_negative(db):
    """A second link at a position the product already uses fails with unique_violation
    (23505), and a negative position fails with check_violation (23514)."""
    db.check(f"update public.product_ingredients set position = 0 where product_id = '{SNAIL}' and ingredient_id = '{WATER}';")
    clash = db.run(f"update public.product_ingredients set position = 0 where product_id = '{SNAIL}' and ingredient_id = '{GLYCERIN}';")
    assert clash.sqlstate == "23505"
    negative = db.run(f"update public.product_ingredients set position = -1 where product_id = '{SNAIL}' and ingredient_id = '{GLYCERIN}';")
    assert negative.sqlstate == "23514"


def test_catalogue_expansion_sql_stores_label_positions_and_reruns_cleanly(db):
    """SQL generated by the catalogue-expansion loader, once 0013 has run, links the new
    product's ingredients at positions 0..3 in label order (including a new ingredient
    defined in the same file); running the same SQL a second time changes nothing."""
    from app.db.catalog_expansion import Snapshot, generate_sql

    data = {
        "sources": [], "concerns": [], "conflict_rules": [], "category_rules": [],
        "ingredients": [{"name": "Zinc Test", "functional_group": "Vitamin B3", "awareness_tier": "low",
                         "benefits": "Regulates sebum.", "good_for": "Oily Skin", "bad_for": "None"}],
        "products": [{"brand": "Loader Brand", "name": "Label Order Serum", "category": "Treatments",
                      "description": "A serum.", "price_thb": 590, "price_usd": 17.5, "source_url": None,
                      "ingredients": ["Glycerin", "Zinc Test", "Water", "Niacinamide"]}],
    }
    sql = generate_sql(data, Snapshot(has_product_sources=True, has_ingredient_positions=True))
    assert db.run(sql).ok
    product = scalar(db, "select to_jsonb(id) from public.products where name = 'Label Order Serum';")
    zinc = scalar(db, "select to_jsonb(id) from public.ingredients where name = 'Zinc Test';")
    assert positions(db, product) == [[GLYCERIN, 0], [zinc, 1], [WATER, 2], [NIACINAMIDE, 3]]
    before = snapshot(db)
    assert db.run(sql).ok
    assert snapshot(db) == before
