"""Tests for the test double itself (tests/conftest.py's FakeSupabase).

Every handler test asserts on what a write left behind in `fake.store`. If the
fake's insert/update/delete logic is wrong, those tests are all wrong together
and silently — a green suite proving nothing. These tests are the floor that
stops that.

They cover the fake, not application code, so the Test Record generator does
not map them to a UTC group; they appear under "Not included in this chapter".
"""

import pytest

from tests.conftest import FAKE_TIMESTAMP


@pytest.fixture
def store():
    """Two users' shelf items, so every ownership-scoping case has something to
    accidentally match."""
    return {
        "shelf_items": [
            {"id": "item-a", "user_id": "user-A", "product_id": "p1", "usage_state": "unopened"},
            {"id": "item-b", "user_id": "user-B", "product_id": "p2", "usage_state": "active"},
        ]
    }


# --- insert ------------------------------------------------------------------

def test_insert_appends_the_payload_to_the_store(fake_supabase, store):
    """Adds exactly one row to the table and leaves the existing rows in place."""
    fake = fake_supabase(store)
    fake.table("shelf_items").insert({"user_id": "user-A", "product_id": "p9"}).execute()

    assert len(fake.store["shelf_items"]) == 3
    assert fake.store["shelf_items"][-1]["product_id"] == "p9"


def test_insert_returns_the_row_it_created(fake_supabase, store):
    """Returns the inserted row in .data, not whatever was already seeded — the
    handlers return response.data[0] straight to the caller."""
    fake = fake_supabase(store)
    response = fake.table("shelf_items").insert({"user_id": "user-A", "product_id": "p9"}).execute()

    assert len(response.data) == 1
    assert response.data[0]["product_id"] == "p9"


def test_insert_supplies_an_id_and_created_at(fake_supabase, store):
    """Fills in the columns Postgres would generate, so the caller receives an
    id it can later use to patch or delete the item."""
    fake = fake_supabase(store)
    row = fake.table("shelf_items").insert({"user_id": "user-A"}).execute().data[0]

    assert row["id"]
    assert row["created_at"] == FAKE_TIMESTAMP


def test_insert_keeps_an_explicitly_supplied_id(fake_supabase, store):
    """Does not overwrite an id the payload already carries."""
    fake = fake_supabase(store)
    row = fake.table("shelf_items").insert({"id": "chosen", "user_id": "user-A"}).execute().data[0]

    assert row["id"] == "chosen"


def test_insert_accepts_a_list_of_rows(fake_supabase):
    """Accepts the list form as well as a single dict, matching supabase-py."""
    fake = fake_supabase({})
    response = fake.table("t").insert([{"n": 1}, {"n": 2}]).execute()

    assert [r["n"] for r in response.data] == [1, 2]
    assert len(fake.store["t"]) == 2


def test_insert_creates_the_table_when_it_does_not_exist(fake_supabase):
    """Inserts into a table the test never seeded, rather than losing the row."""
    fake = fake_supabase({})
    fake.table("quiz_results").insert({"user_id": "user-A"}).execute()

    assert len(fake.store["quiz_results"]) == 1


# --- update ------------------------------------------------------------------

def test_update_merges_the_payload_into_matching_rows(fake_supabase, store):
    """Applies the update to the stored row, so the change is observable through
    fake.store rather than only in the response."""
    fake = fake_supabase(store)
    fake.table("shelf_items").update({"usage_state": "active"}).eq("id", "item-a").execute()

    assert fake.store["shelf_items"][0]["usage_state"] == "active"


def test_update_leaves_non_matching_rows_untouched(fake_supabase, store):
    """Changes only the filtered rows — the other user's row keeps its value."""
    fake = fake_supabase(store)
    fake.table("shelf_items").update({"usage_state": "archived"}).eq("id", "item-a").execute()

    assert fake.store["shelf_items"][1]["usage_state"] == "active"


def test_update_returns_the_merged_rows(fake_supabase, store):
    """Returns the updated rows in .data, carrying the new values — the handlers
    return response.data[0] to the caller."""
    fake = fake_supabase(store)
    response = fake.table("shelf_items").update({"pao": 12}).eq("id", "item-a").execute()

    assert response.data[0]["pao"] == 12


def test_update_matching_nothing_returns_an_empty_list(fake_supabase, store):
    """Returns [] when no row matches, which is what real supabase-py does and
    what makes the handlers' response.data[0] raise."""
    fake = fake_supabase(store)
    response = fake.table("shelf_items").update({"pao": 12}).eq("id", "nope").execute()

    assert response.data == []


# --- delete ------------------------------------------------------------------

def test_delete_removes_matching_rows_from_the_store(fake_supabase, store):
    """Removes the row, so a test can prove a delete actually happened."""
    fake = fake_supabase(store)
    fake.table("shelf_items").delete().eq("id", "item-a").execute()

    assert [r["id"] for r in fake.store["shelf_items"]] == ["item-b"]


def test_delete_matching_nothing_removes_nothing(fake_supabase, store):
    """Leaves the store intact when the filter matches no row — this is the
    behaviour ownership-scoping tests depend on."""
    fake = fake_supabase(store)
    fake.table("shelf_items").delete().eq("id", "item-a").eq("user_id", "user-B").execute()

    assert len(fake.store["shelf_items"]) == 2


def test_delete_returns_the_removed_rows(fake_supabase, store):
    """Returns the deleted rows in .data, so a handler could check whether
    anything was actually removed."""
    fake = fake_supabase(store)
    response = fake.table("shelf_items").delete().eq("id", "item-a").execute()

    assert response.data[0]["id"] == "item-a"


# --- filtering ---------------------------------------------------------------

def test_compound_filters_scope_to_the_intersection(fake_supabase, store):
    """Applies every .eq() together, which is what makes the handlers'
    .eq("id", ...).eq("user_id", ...) ownership check meaningful."""
    fake = fake_supabase(store)
    matched = fake.table("shelf_items").select("*").eq("id", "item-a").eq("user_id", "user-A").execute()

    assert len(matched.data) == 1


def test_select_still_filters_limits_and_singles(fake_supabase, store):
    """Preserves the read behaviour the existing 234 tests rely on."""
    fake = fake_supabase(store)
    assert len(fake.table("shelf_items").select("*").execute().data) == 2
    assert len(fake.table("shelf_items").select("*").limit(1).execute().data) == 1
    assert fake.table("shelf_items").select("*").eq("id", "item-b").single().execute().data["id"] == "item-b"


def test_single_raises_pgrst116_on_zero_rows_like_the_real_client(fake_supabase, store):
    """Raises APIError with code PGRST116 when the filters match nothing, which
    is what supabase-py's .single() does rather than returning empty data.

    This is the floor under every zero-row test in the suite. The fake used to
    return None here, and that leniency hid live defects behind green tests -
    GET /products/compare answered 400 for an unknown product id while its 404
    test passed. Nothing in app/ uses .single() any more, so no application
    test can catch a regression here; this one has to."""
    from postgrest.exceptions import APIError

    fake = fake_supabase(store)
    with pytest.raises(APIError) as excinfo:
        fake.table("shelf_items").select("*").eq("id", "does-not-exist").single().execute()

    assert excinfo.value.code == "PGRST116"


def test_unknown_builder_methods_still_no_op(fake_supabase, store):
    """Continues the chain for builder methods the fake does not model (order,
    range, gte, ...), so a handler using them still works."""
    fake = fake_supabase(store)
    response = fake.table("shelf_items").select("*").order("created_at").range(0, 10).execute()

    assert len(response.data) == 2


def test_private_attributes_are_not_answered_by_the_catch_all(fake_supabase, store):
    """Raises AttributeError for an unset private name instead of returning the
    chain function. Returning a truthy function here is what would silently
    misroute execute()'s insert/update/delete dispatch."""
    query = fake_supabase(store).table("shelf_items")

    with pytest.raises(AttributeError):
        query._not_a_real_attribute
