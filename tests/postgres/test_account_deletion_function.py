"""Migration 0014 against a local Postgres replica: the six consent columns on
users and delete_user_account(), the function behind POST /auth/me/delete.

Skipped unless local PostgreSQL binaries and both (local-only) migration files
are present: SKINBUDDY_PG_BIN, SKINBUDDY_MIGRATION_0013 and
SKINBUDDY_MIGRATION_0014 (see tests/postgres/pg_harness.py).

The replica is a second database in the same throwaway cluster: the 0013 replica
tables, the tables 0014 deletes from (replica_schema_0014.sql), then 0013 and 0014.
"""

import os
from pathlib import Path

import pytest

import pg_harness
from pg_harness import lit

MIGRATION_0014 = Path(os.environ.get(
    "SKINBUDDY_MIGRATION_0014",
    pg_harness.REPO_ROOT / "app" / "db" / "migrations" / "0014_consent_and_account_deletion.sql"))
REPLICA_0014 = Path(__file__).with_name("replica_schema_0014.sql")
DB14 = "skinbuddy_test_0014"

ADMIN = "00000000-0000-0000-0000-0000000000a1"
VICTIM = "00000000-0000-0000-0000-0000000000c1"
OTHER = "00000000-0000-0000-0000-0000000000d1"
GHOST = "00000000-0000-0000-0000-0000000000ff"

PRODUCT = "22222222-0000-0000-0000-000000000001"      # a catalogue product made from the victim's approved submission
OTHER_PRODUCT = "22222222-0000-0000-0000-000000000002"

PHOTO_PENDING = "submissions/11111111-1111-4111-8111-111111111111.jpg"
PHOTO_EDITED_OLD = "submissions/22222222-2222-4222-8222-222222222222.jpg"
PHOTO_EDITED_NEW = "submissions/33333333-3333-4333-8333-333333333333.jpg"
PHOTO_APPROVED = "submissions/44444444-4444-4444-8444-444444444444.jpg"
PHOTO_REJECTED = "submissions/55555555-5555-4555-8555-555555555555.jpg"
PHOTO_CLEARED = "submissions/66666666-6666-4666-8666-666666666666.jpg"
PHOTO_OTHER = "submissions/77777777-7777-4777-8777-777777777777.jpg"

DELETED_TABLES = ["routine_step_completions", "routine_steps", "routines", "shelf_items",
                  "skin_analysis_reports", "skin_logs", "quiz_results", "product_submissions"]
CATALOGUE = ["products", "ingredients", "product_ingredients", "sources", "product_sources"]
ALL_TABLES = DELETED_TABLES + ["users"] + CATALOGUE

OTHER_ROUTINE = "b0000000-0000-0000-0000-0000000000f1"

SEED = f"""
truncate public.routine_step_completions, public.routine_steps, public.routines,
         public.shelf_items, public.skin_analysis_reports, public.skin_logs,
         public.quiz_results, public.product_submissions, public.product_sources,
         public.product_ingredients, public.products, public.sources,
         public.ingredients, public.users cascade;
insert into public.users (id, line_id, display_name, role) values
  ('{ADMIN}',  'U-admin',  'Admin',  'admin'),
  ('{VICTIM}', 'U-victim', 'Victim', 'user'),
  ('{OTHER}',  'U-other',  'Other',  'user');
insert into public.products (id, brand, name, category, slug, image_url) values
  ('{PRODUCT}', 'Acme', 'Approved Serum', 'Treatments', 'acme-approved-serum', 'https://x/{PHOTO_APPROVED}'),
  ('{OTHER_PRODUCT}', 'Acme', 'Other Serum', 'Treatments', 'acme-other-serum', null);
insert into public.ingredients (id, name, benefits) values ('44444444-0000-0000-0000-000000000001', 'Water', 'The base.');
insert into public.product_ingredients (product_id, ingredient_id) values
  ('{PRODUCT}', '44444444-0000-0000-0000-000000000001');
insert into public.sources (id, title, url, source_type) values
  ('33333333-0000-0000-0000-000000000001', 'A source', 'https://s.example/a', 'safety_review');
insert into public.product_sources (product_id, source_id, claim) values
  ('{PRODUCT}', '33333333-0000-0000-0000-000000000001', 'listing');

-- the victim's data
insert into public.shelf_items (id, user_id, product_id) values
  ('a0000000-0000-0000-0000-000000000001', '{VICTIM}', '{PRODUCT}'),
  ('a0000000-0000-0000-0000-000000000002', '{VICTIM}', '{OTHER_PRODUCT}');
insert into public.routines (id, user_id) values ('b0000000-0000-0000-0000-000000000001', '{VICTIM}');
insert into public.routine_steps (id, routine_id, shelf_item_id, product_id) values
  ('c0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', '{PRODUCT}'),
  ('c0000000-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000002', '{OTHER_PRODUCT}');
insert into public.routine_step_completions (user_id, step_id, product_id, period_key) values
  ('{VICTIM}', 'c0000000-0000-0000-0000-000000000001', '{PRODUCT}', '2026-10-01'),
  ('{VICTIM}', null, '{PRODUCT}', '2026-09-30');
insert into public.skin_logs (id, user_id, week_start) values
  ('d0000000-0000-0000-0000-000000000001', '{VICTIM}', '2026-09-28');
insert into public.skin_analysis_reports (log_id, user_id) values
  ('d0000000-0000-0000-0000-000000000001', '{VICTIM}');
insert into public.quiz_results (user_id) values ('{VICTIM}'), ('{VICTIM}');
insert into public.product_submissions (id, submitted_by, status, payload, edited_payload, reviewed_by, product_id) values
  ('e0000000-0000-0000-0000-000000000001', '{VICTIM}', 'pending',  '{{"image_path": "{PHOTO_PENDING}"}}', null, null, null),
  ('e0000000-0000-0000-0000-000000000002', '{VICTIM}', 'pending',  '{{"image_path": "{PHOTO_EDITED_OLD}"}}', '{{"image_path": "{PHOTO_EDITED_NEW}"}}', null, null),
  ('e0000000-0000-0000-0000-000000000003', '{VICTIM}', 'approved', '{{"image_path": "{PHOTO_APPROVED}"}}', null, '{ADMIN}', '{PRODUCT}'),
  ('e0000000-0000-0000-0000-000000000004', '{VICTIM}', 'rejected', '{{"image_path": "{PHOTO_REJECTED}"}}', null, '{ADMIN}', null),
  ('e0000000-0000-0000-0000-000000000005', '{VICTIM}', 'pending',  '{{"image_path": "{PHOTO_CLEARED}"}}', '{{"image_path": null}}', null, null),
  ('e0000000-0000-0000-0000-000000000006', '{VICTIM}', 'pending',  '{{"name": "no photo"}}', null, null, null),
  ('e0000000-0000-0000-0000-000000000007', '{VICTIM}', 'pending',  '{{"image_path": "{PHOTO_PENDING}"}}', null, null, null);

-- another user's data, which must survive; one of their submissions was reviewed by the victim
insert into public.shelf_items (id, user_id, product_id) values
  ('a0000000-0000-0000-0000-0000000000f1', '{OTHER}', '{PRODUCT}');
insert into public.routines (id, user_id) values ('{OTHER_ROUTINE}', '{OTHER}');
insert into public.routine_steps (id, routine_id, shelf_item_id, product_id) values
  ('c0000000-0000-0000-0000-0000000000f1', '{OTHER_ROUTINE}', 'a0000000-0000-0000-0000-0000000000f1', '{PRODUCT}');
insert into public.routine_step_completions (user_id, step_id, product_id, period_key) values
  ('{OTHER}', 'c0000000-0000-0000-0000-0000000000f1', '{PRODUCT}', '2026-10-01');
insert into public.skin_logs (id, user_id, week_start) values
  ('d0000000-0000-0000-0000-0000000000f1', '{OTHER}', '2026-09-28');
insert into public.skin_analysis_reports (log_id, user_id) values
  ('d0000000-0000-0000-0000-0000000000f1', '{OTHER}');
insert into public.quiz_results (user_id) values ('{OTHER}');
insert into public.product_submissions (id, submitted_by, status, payload, reviewed_by) values
  ('e0000000-0000-0000-0000-0000000000f1', '{OTHER}', 'approved', '{{"image_path": "{PHOTO_OTHER}"}}', '{VICTIM}'),
  ('e0000000-0000-0000-0000-0000000000f2', '{OTHER}', 'pending',  '{{"image_path": "{PHOTO_PENDING}"}}', null),
  ('e0000000-0000-0000-0000-0000000000f3', '{OTHER}', 'rejected', '{{"name": "x"}}', '{ADMIN}');
"""


@pytest.fixture(scope="module")
def pg14(pg):
    if not MIGRATION_0014.exists():
        pytest.skip(f"migration not found: {MIGRATION_0014} (set SKINBUDDY_MIGRATION_0014)")
    pg.create_database(DB14)
    pg.check(REPLICA_0014.read_text(encoding="utf-8"), db=DB14)
    # The replica gives service_role EXECUTE on every new function by default, as Supabase
    # does. Take that away for this database so that the migration's own GRANT is what
    # the tests prove (a missing GRANT must not be hidden by the default).
    pg.check("alter default privileges in schema public revoke all on functions from service_role;", db=DB14)
    pg.check(pg_harness.MIGRATION_0013.read_text(encoding="utf-8"), db=DB14)
    # Twice: the second run proves 0014 is safe to re-run.
    pg.migration_runs_0014 = [pg.run_file(MIGRATION_0014, db=DB14) for _ in range(2)]
    return pg


@pytest.fixture
def db(pg14):
    pg14.check(SEED, db=DB14)
    return pg14


def q(db, sql, **kwargs):
    return db.check(sql, db=DB14, **kwargs)


def scalar(db, sql):
    return q(db, sql).json()


def snapshot(db, tables=ALL_TABLES):
    parts = ", ".join(
        f"'{t}', (select coalesce(jsonb_agg(to_jsonb(x) order by to_jsonb(x)::text), '[]') from public.{t} x)"
        for t in tables)
    return scalar(db, f"select jsonb_build_object({parts});")


def delete(db, user_id, **kwargs):
    return db.run(f"select public.delete_user_account({lit(user_id)}::uuid);", db=DB14, **kwargs)


def rows_of(db, user_id):
    """How many rows each of the deleted tables still holds for the user."""
    counts = {}
    for table, column in [("routine_step_completions", "user_id"), ("routines", "user_id"),
                          ("shelf_items", "user_id"), ("skin_analysis_reports", "user_id"),
                          ("skin_logs", "user_id"), ("quiz_results", "user_id"),
                          ("product_submissions", "submitted_by"), ("users", "id")]:
        counts[table] = scalar(db, f"select count(*) from public.{table} where {column} = '{user_id}';")
    counts["routine_steps"] = scalar(
        db, f"select count(*) from public.routine_steps where routine_id in "
            f"(select id from public.routines where user_id = '{user_id}');")
    return counts


# --- the columns -------------------------------------------------------------------

def test_0014_adds_six_nullable_consent_columns_to_users(db):
    """Adds terms_accepted_at, terms_version, age_confirmed_at, health_consent_at,
    health_consent_version and health_consent_withdrawn_at to users, each
    nullable, with the timestamps typed timestamptz and the versions text."""
    found = q(db, """
        select coalesce(jsonb_object_agg(column_name, jsonb_build_array(data_type, is_nullable)), '{}')
          from information_schema.columns
         where table_schema = 'public' and table_name = 'users'
           and column_name in ('terms_accepted_at', 'terms_version', 'age_confirmed_at',
                               'health_consent_at', 'health_consent_version',
                               'health_consent_withdrawn_at');""").json()
    assert found == {
        "terms_accepted_at": ["timestamp with time zone", "YES"],
        "terms_version": ["text", "YES"],
        "age_confirmed_at": ["timestamp with time zone", "YES"],
        "health_consent_at": ["timestamp with time zone", "YES"],
        "health_consent_version": ["text", "YES"],
        "health_consent_withdrawn_at": ["timestamp with time zone", "YES"],
    }


def test_0014_gives_existing_users_no_consent(db):
    """Leaves every consent column NULL for a user row that has not recorded
    consent, because nothing is backfilled: such a user has given no consent."""
    row = scalar(db, f"select to_jsonb(u) from public.users u where id = '{VICTIM}';")
    for column in ("terms_accepted_at", "terms_version", "age_confirmed_at", "health_consent_at",
                   "health_consent_version", "health_consent_withdrawn_at"):
        assert row[column] is None


def test_0014_can_be_run_again_without_changing_anything(db):
    """Succeeds when run a third time on a database that already has it, leaving
    every table's rows and the stored consent values exactly as they were."""
    q(db, f"update public.users set terms_version = 'v1', terms_accepted_at = now() where id = '{VICTIM}';")
    before = snapshot(db)
    for run in db.migration_runs_0014:
        assert run.ok, run.stderr
    again = db.run_file(MIGRATION_0014, db=DB14)
    assert again.ok, again.stderr
    assert snapshot(db) == before


def test_0014_function_is_run_by_the_caller_with_a_pinned_search_path(db):
    """Declares delete_user_account as SECURITY INVOKER with its search_path
    pinned to public, pg_temp, so it runs with the caller's rights and cannot be
    redirected by a schema the caller controls."""
    row = scalar(db, """
        select jsonb_build_object('definer', prosecdef, 'config', proconfig)
          from pg_proc where oid = 'public.delete_user_account(uuid)'::regprocedure;""")
    assert row == {"definer": False, "config": ["search_path=public, pg_temp"]}


# --- what it deletes ----------------------------------------------------------------

def test_delete_removes_every_row_the_user_owns(db):
    """Deletes the user's routine step completions, routine steps, routines, shelf
    items, analysis reports, skin logs, quiz results, submissions and users row,
    and reports the row count removed from each table."""
    assert rows_of(db, VICTIM) == {
        "routine_step_completions": 2, "routines": 1, "shelf_items": 2, "skin_analysis_reports": 1,
        "skin_logs": 1, "quiz_results": 2, "product_submissions": 7, "users": 1, "routine_steps": 2}
    result = delete(db, VICTIM)
    assert result.ok, result.stderr
    assert result.json()["deleted"] == {
        "routine_step_completions": 2, "routine_steps": 2, "routines": 1, "shelf_items": 2,
        "skin_analysis_reports": 1, "skin_logs": 1, "quiz_results": 2, "product_submissions": 7, "users": 1}
    assert set(rows_of(db, VICTIM).values()) == {0}


def test_delete_leaves_every_other_users_rows_alone(db):
    """Changes no row that belongs to another user: their shelf, routine, steps,
    completions, log, report, quiz result and submissions are all unchanged,
    apart from the reviewed_by name of the deleted user."""
    before = snapshot(db, DELETED_TABLES + ["users"])
    assert delete(db, VICTIM).ok
    after = snapshot(db, DELETED_TABLES + ["users"])

    def others(snap):
        out = {}
        for table, rows in snap.items():
            kept = []
            for r in rows:
                if table == "routine_steps":
                    owner = OTHER if r["routine_id"] == OTHER_ROUTINE else VICTIM
                else:
                    owner = r.get("user_id") or r.get("submitted_by") or r.get("id")
                if owner != VICTIM:
                    r = {k: v for k, v in r.items() if k not in ("reviewed_by", "updated_at")}
                    kept.append(r)
            out[table] = kept
        return out

    assert others(after) == others(before)
    assert {r["id"] for r in after["users"]} == {ADMIN, OTHER}
    assert len(others(after)["routine_steps"]) == 1 and len(others(after)["product_submissions"]) == 3


def test_delete_keeps_the_catalogue_and_the_approved_product(db):
    """Leaves products, ingredients, product_ingredients, sources and
    product_sources exactly as they were, so a product approved from the user's
    submission stays in the catalogue."""
    before = snapshot(db, CATALOGUE)
    assert delete(db, VICTIM).ok
    assert snapshot(db, CATALOGUE) == before
    assert scalar(db, f"select count(*) from public.products where id = '{PRODUCT}';") == 1


def test_delete_clears_the_reviewer_name_on_submissions_the_user_reviewed(db):
    """Sets reviewed_by to NULL on another user's submission that the deleted user
    reviewed, keeps that submission, leaves the reviewer of a submission someone else
    reviewed, and reports one cleared row."""
    result = delete(db, VICTIM)
    assert result.ok, result.stderr
    assert result.json()["cleared"] == {"product_submissions.reviewed_by": 1}
    row = scalar(db, "select to_jsonb(s) from public.product_submissions s "
                     "where id = 'e0000000-0000-0000-0000-0000000000f1';")
    assert row["reviewed_by"] is None
    assert row["status"] == "approved" and row["submitted_by"] == OTHER
    # A submission another user reviewed keeps its reviewer.
    assert scalar(db, "select to_jsonb(reviewed_by) from public.product_submissions "
                      "where id = 'e0000000-0000-0000-0000-0000000000f3';") == ADMIN


def test_delete_returns_the_effective_photo_of_every_deleted_submission(db):
    """Returns image_paths holding each deleted submission's effective image_path,
    once each: an edited path replaces the submitted one, an edited null removes it,
    a rejected or approved submission's photo is included, and a submission with no
    photo adds nothing."""
    result = delete(db, VICTIM)
    assert result.ok, result.stderr
    assert sorted(result.json()["image_paths"]) == sorted(
        [PHOTO_PENDING, PHOTO_EDITED_NEW, PHOTO_APPROVED, PHOTO_REJECTED])


def test_delete_returns_an_empty_photo_list_for_a_user_without_submissions(db):
    """Returns image_paths as an empty list for a user who submitted nothing."""
    q(db, f"delete from public.product_submissions where submitted_by = '{OTHER}';")
    q(db, f"delete from public.product_submissions where reviewed_by = '{OTHER}';")
    result = delete(db, OTHER)
    assert result.ok, result.stderr
    assert result.json()["image_paths"] == []
    assert result.json()["deleted"]["users"] == 1


# --- what it refuses ----------------------------------------------------------------

def test_delete_refuses_an_admin_account_and_deletes_nothing(db):
    """Fails with SQLSTATE SBADM for a user whose role is admin, and leaves every
    table exactly as it was."""
    before = snapshot(db)
    result = delete(db, ADMIN)
    assert not result.ok
    assert result.sqlstate == "SBADM"
    assert snapshot(db) == before


def test_delete_accepts_an_account_once_it_has_been_demoted(db):
    """Deletes an account whose role was changed from admin to user by hand,
    which is the way the error message tells the owner to proceed, and clears its
    name from the three submissions it had reviewed."""
    q(db, f"update public.users set role = 'user' where id = '{ADMIN}';")
    result = delete(db, ADMIN)
    assert result.ok, result.stderr
    assert scalar(db, f"select count(*) from public.users where id = '{ADMIN}';") == 0
    assert result.json()["cleared"] == {"product_submissions.reviewed_by": 3}


def test_delete_fails_with_sbnfd_for_a_user_that_does_not_exist(db):
    """Fails with SQLSTATE SBNFD for an id with no users row, and changes nothing."""
    before = snapshot(db)
    result = delete(db, GHOST)
    assert not result.ok
    assert result.sqlstate == "SBNFD"
    assert snapshot(db) == before


def test_delete_cannot_be_repeated(db):
    """Fails with SQLSTATE SBNFD when the same user is deleted a second time, and
    the second call changes nothing."""
    assert delete(db, VICTIM).ok
    after_first = snapshot(db)
    second = delete(db, VICTIM)
    assert second.sqlstate == "SBNFD"
    assert snapshot(db) == after_first


def test_delete_rolls_everything_back_when_an_unknown_table_references_the_user(db):
    """Deletes nothing at all when a table the function does not know about still
    holds a row for the user: the final users delete fails with a foreign key
    error (23503) and every earlier delete in the call is rolled back."""
    q(db, "create table public.sb_unknown_ref (user_id uuid references public.users(id));")
    try:
        q(db, f"insert into public.sb_unknown_ref values ('{VICTIM}');")
        before = snapshot(db)
        result = delete(db, VICTIM)
        assert not result.ok
        assert result.sqlstate == "23503"
        assert snapshot(db) == before
        assert rows_of(db, VICTIM)["shelf_items"] == 2
    finally:
        q(db, "drop table public.sb_unknown_ref;")


def test_delete_rolls_back_when_a_step_fails_part_way(db):
    """Deletes nothing when a delete in the middle of the sequence fails: a routine
    step of another user's routine that points at the user's shelf item stops the
    shelf items delete, and the completions and steps already deleted come back."""
    q(db, f"insert into public.routine_steps (routine_id, shelf_item_id) "
          f"values ('{OTHER_ROUTINE}', 'a0000000-0000-0000-0000-000000000002');")
    before = snapshot(db)
    result = delete(db, VICTIM)
    assert not result.ok
    assert result.sqlstate == "23503"
    assert snapshot(db) == before


# --- who may call it ----------------------------------------------------------------

def test_delete_function_can_be_run_by_service_role_only(db):
    """Grants EXECUTE on delete_user_account to service_role and to none of
    public, anon or authenticated."""
    rows = q(db, """
        select jsonb_object_agg(r.rolname, has_function_privilege(r.rolname,
               'public.delete_user_account(uuid)', 'execute'))
          from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role');""").json()
    assert rows == {"anon": False, "authenticated": False, "service_role": True}
    assert not scalar(db, "select to_jsonb(exists (select 1 from pg_proc p, aclexplode(p.proacl) a "
                          "where p.oid = 'public.delete_user_account(uuid)'::regprocedure and a.grantee = 0));")


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_delete_function_is_refused_to_a_signed_in_caller(db, role):
    """Fails with SQLSTATE 42501 (permission denied) when anon or authenticated calls
    delete_user_account, and deletes nothing."""
    before = snapshot(db)
    result = delete(db, VICTIM, role=role)
    assert not result.ok
    assert result.sqlstate == "42501"
    assert snapshot(db) == before


def test_delete_function_runs_as_service_role(db):
    """Succeeds when service_role calls delete_user_account, deleting the user's rows."""
    result = delete(db, VICTIM, role="service_role")
    assert result.ok, result.stderr
    assert result.json()["deleted"]["users"] == 1
