"""API-level tests for the Shelf router, driven through FastAPI's TestClient.

Covers what `test_auth_boundaries.py` cannot: what these endpoints actually do
once a *valid* request gets through. Auth rejection is already proven there and
is not repeated here.

Two patch targets are in play, and mixing them up silently breaks the tests:

  app.api.shelf                          for the handlers that query directly
  app.core.services.compatibility_service for /analyze, which delegates entirely

`analyze_product_compatibility` never touches `supabase` itself — it calls
compatibility_service, which holds its own module-level reference. Patching
`app.api.shelf` for that route would leave the real client in place.

The Supabase select strings here are PostgREST joins the fake cannot resolve
(`products(name, product_ingredients(ingredients(...)))`), so the seeded rows
carry the already-joined shape, exactly as the real client would return it.
"""

import pytest


# --- fixtures ----------------------------------------------------------------

def shelf_row(item_id, user_id, usage_state="active", product=None, **extra):
    row = {"id": item_id, "user_id": user_id, "usage_state": usage_state}
    if product is not None:
        row["products"] = product
    row.update(extra)
    return row


def joined_product(name, ing_id, ing_name, functional_group):
    """The shape a `products(name, product_ingredients(ingredients(...)))` join
    returns — seeded directly, since the fake cannot resolve joins."""
    return {
        "name": name,
        "product_ingredients": [
            {"ingredients": {"id": ing_id, "name": ing_name,
                             "functional_group": functional_group}}
        ],
    }


GLYCERIN_PRODUCT = joined_product("Hydrating Cleanser", "ing-gly", "Glycerin", "Humectant")
BHA_PRODUCT = joined_product("2% BHA Liquid", "ing-sa", "Salicylic Acid",
                             "Beta Hydroxy Acid (BHA)")

RETINOL_TARGET = {
    "id": "prod-retinol",
    "name": "Retinol 0.2% in Squalane",
    "product_ingredients": [
        {"ingredients": {"id": "ing-retinol", "name": "Retinol",
                         "functional_group": "Retinoid"}}
    ],
}

RETINOID_VS_BHA_RULE = {
    "group_a": "Retinoid",
    "group_b": "Beta Hydroxy Acid (BHA)",
    "severity": "high",
    "warning_message": "Layering these raises irritation risk.",
}


# ============================================================ Task 1
# GET /shelf/ — get_user_shelf()

def test_shelf_returns_only_the_calling_users_items(client, patch_supabase, as_user):
    """Returns HTTP 200 and only the authenticated user's shelf items, with
    another user's row absent from the response."""
    as_user("user-1")
    patch_supabase({"shelf_items": [
        shelf_row("item-1", "user-1"),
        shelf_row("item-2", "user-2"),
    ]}, "app.api.shelf")

    resp = client.get("/shelf/")
    assert resp.status_code == 200

    returned = resp.json()
    assert [row["id"] for row in returned] == ["item-1"]
    assert all(row["user_id"] == "user-1" for row in returned)


def test_shelf_returns_an_empty_list_when_the_user_has_no_items(client, patch_supabase, as_user):
    """Returns HTTP 200 and an empty list, not an error, for a user whose shelf
    is empty."""
    as_user("user-1")
    patch_supabase({"shelf_items": [shelf_row("item-2", "user-2")]}, "app.api.shelf")

    resp = client.get("/shelf/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_shelf_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to fetch shelf." and no
    trace of the internal exception text, when the query raises."""
    import app.api.shelf as shelf_module

    as_user("user-1")
    patch_supabase({"shelf_items": []}, "app.api.shelf")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(shelf_module.supabase, "table", explode)

    resp = client.get("/shelf/")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to fetch shelf."


# ============================================================ Task 2
# GET /shelf/analyze/{product_id} — analyze_product_compatibility()
#
# The conflict algorithm itself is covered by test_baumann_scoring.py and the
# products-compare tests. These prove only this route's wiring: which shelf
# items reach the comparison set.

def analysis_store(shelf_items):
    return {
        "shelf_items": shelf_items,
        "products": [RETINOL_TARGET],
        "users": [{"id": "user-1", "skin_type": "ORNT"}],
        "conflict_rules": [],
        "category_conflict_rules": [RETINOID_VS_BHA_RULE],
    }


def alert_types(response):
    return {w["alert_type"] for w in response.json()["warnings"]}


def test_analyze_surfaces_a_conflict_with_an_active_shelf_product(client, patch_supabase, as_user):
    """Returns is_safe=False and an Active Routine Clash warning when the target
    Retinoid conflicts with a BHA product active on the caller's shelf.

    Positive control: without this, the two scoping tests below could pass
    simply because no warning is ever produced."""
    as_user("user-1")
    patch_supabase(
        analysis_store([shelf_row("item-1", "user-1", "active", BHA_PRODUCT)]),
        "app.core.services.compatibility_service",
    )

    resp = client.get("/shelf/analyze/prod-retinol")
    assert resp.status_code == 200
    assert "Active Routine Clash" in alert_types(resp)
    assert resp.json()["is_safe"] is False


def test_analyze_ignores_shelf_items_that_are_not_active(client, patch_supabase, as_user):
    """Returns no Active Routine Clash when the conflicting BHA product is on the
    caller's shelf but not in the active state, so only active items form the
    comparison set."""
    as_user("user-1")
    patch_supabase(
        analysis_store([
            shelf_row("item-1", "user-1", "active", GLYCERIN_PRODUCT),
            shelf_row("item-2", "user-1", "wishlist", BHA_PRODUCT),
        ]),
        "app.core.services.compatibility_service",
    )

    resp = client.get("/shelf/analyze/prod-retinol")
    assert resp.status_code == 200
    assert "Active Routine Clash" not in alert_types(resp)


def test_analyze_ignores_another_users_active_shelf_items(client, patch_supabase, as_user):
    """Returns no Active Routine Clash when the conflicting BHA product is active
    on a different user's shelf, so the comparison set is scoped to the caller."""
    as_user("user-1")
    patch_supabase(
        analysis_store([
            shelf_row("item-1", "user-1", "active", GLYCERIN_PRODUCT),
            shelf_row("item-2", "user-2", "active", BHA_PRODUCT),
        ]),
        "app.core.services.compatibility_service",
    )

    resp = client.get("/shelf/analyze/prod-retinol")
    assert resp.status_code == 200
    assert "Active Routine Clash" not in alert_types(resp)


def test_analyze_reports_safe_when_the_shelf_is_empty(client, patch_supabase, as_user):
    """Returns is_safe=True and no warnings when the caller has no active shelf
    items to compare the target product against."""
    as_user("user-1")
    patch_supabase(analysis_store([]), "app.core.services.compatibility_service")

    resp = client.get("/shelf/analyze/prod-retinol")
    assert resp.status_code == 200
    assert resp.json() == {"is_safe": True, "warnings": []}


def test_analyze_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to analyze product
    compatibility." and no trace of the internal exception text, when the
    underlying query raises."""
    import app.core.services.compatibility_service as compat_module

    as_user("user-1")
    patch_supabase(analysis_store([]), "app.core.services.compatibility_service")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(compat_module.supabase, "table", explode)

    resp = client.get("/shelf/analyze/prod-retinol")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to analyze product compatibility."


# ============================================================ Task 4
# POST /shelf/add — add_to_shelf()

ADD_PAYLOAD = {
    "product_id": "prod-1",
    "usage_state": "unopened",
    "opened_date": None,
    "expiration_date": "2027-01-31",
    "pao": 12,
}

# The exact keys add_to_shelf maps from the request. Asserting on the set
# catches a field being dropped as well as one being added.
ADDED_FIELDS = {"user_id", "product_id", "usage_state",
                "opened_date", "expiration_date", "pao"}


def test_add_inserts_a_row_with_the_mapped_fields(client, patch_supabase, as_user):
    """Inserts one shelf_items row carrying exactly the six fields the handler
    maps from the request — user_id, product_id, usage_state, opened_date,
    expiration_date and pao — with the submitted values."""
    as_user("user-1")
    fake = patch_supabase({"shelf_items": []}, "app.api.shelf")

    resp = client.post("/shelf/add", json=ADD_PAYLOAD)
    assert resp.status_code == 200

    stored = fake.store["shelf_items"]
    assert len(stored) == 1
    # id and created_at are supplied by the database, not the handler.
    assert ADDED_FIELDS <= set(stored[0])
    assert stored[0]["product_id"] == "prod-1"
    assert stored[0]["usage_state"] == "unopened"
    assert stored[0]["expiration_date"] == "2027-01-31"
    assert stored[0]["pao"] == 12


def test_add_returns_the_row_it_created(client, patch_supabase, as_user):
    """Returns the newly created shelf item, including the database-generated id
    the client needs in order to open, archive or delete it later."""
    as_user("user-1")
    patch_supabase({"shelf_items": []}, "app.api.shelf")

    body = client.post("/shelf/add", json=ADD_PAYLOAD).json()

    assert body["product_id"] == "prod-1"
    assert body["id"]


def test_add_takes_the_user_id_from_the_session_not_the_body(client, patch_supabase, as_user):
    """Stores the authenticated caller's id on the new row even when the request
    body supplies a different one, so an item can never be added to another
    user's shelf."""
    as_user("user-1")
    fake = patch_supabase({"shelf_items": []}, "app.api.shelf")

    client.post("/shelf/add", json={**ADD_PAYLOAD, "user_id": "user-2"})

    assert fake.store["shelf_items"][0]["user_id"] == "user-1"


def test_add_leaves_existing_shelf_items_untouched(client, patch_supabase, as_user):
    """Appends the new item without altering or removing rows already on the
    shelf, including another user's."""
    as_user("user-1")
    fake = patch_supabase({"shelf_items": [
        shelf_row("item-1", "user-1"),
        shelf_row("item-2", "user-2"),
    ]}, "app.api.shelf")

    client.post("/shelf/add", json=ADD_PAYLOAD)

    stored = fake.store["shelf_items"]
    assert len(stored) == 3
    assert [row["id"] for row in stored[:2]] == ["item-1", "item-2"]


def test_add_rejects_a_request_missing_a_required_field(client, patch_supabase, as_user):
    """Returns HTTP 422 and writes nothing when product_id is absent, so an
    incomplete item never reaches the shelf."""
    as_user("user-1")
    fake = patch_supabase({"shelf_items": []}, "app.api.shelf")

    resp = client.post("/shelf/add", json={"usage_state": "unopened"})

    assert resp.status_code == 422
    assert fake.store["shelf_items"] == []


def test_add_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to add item to shelf." and
    no trace of the internal exception text, when the insert raises."""
    import app.api.shelf as shelf_module

    as_user("user-1")
    patch_supabase({"shelf_items": []}, "app.api.shelf")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(shelf_module.supabase, "table", explode)

    resp = client.post("/shelf/add", json=ADD_PAYLOAD)
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to add item to shelf."


# ============================================================ Task 5
# DELETE /shelf/{item_id} — delete_from_shelf()
#
# The response is the same fixed message whether or not anything was removed
# (BE-DEF-01), so every assertion below goes through fake.store. Asserting on
# the response body here would pass even with the ownership filter deleted.

def two_user_shelf():
    return {"shelf_items": [
        shelf_row("item-1", "user-1"),
        shelf_row("item-2", "user-1"),
        shelf_row("item-3", "user-2"),
    ]}


def remaining(fake):
    return [row["id"] for row in fake.store["shelf_items"]]


def test_delete_removes_the_callers_own_item(client, patch_supabase, as_user):
    """Removes the target row from shelf_items when it belongs to the caller, and
    returns HTTP 200."""
    as_user("user-1")
    fake = patch_supabase(two_user_shelf(), "app.api.shelf")

    resp = client.delete("/shelf/item-1")

    assert resp.status_code == 200
    assert "item-1" not in remaining(fake)


def test_delete_does_not_remove_another_users_item(client, patch_supabase, as_user):
    """Leaves the row in shelf_items when the item id belongs to a different
    user, so one user can never delete another user's shelf item.

    The endpoint answers "Item removed successfully" either way, so this is
    asserted against the store rather than the response body."""
    as_user("user-1")
    fake = patch_supabase(two_user_shelf(), "app.api.shelf")

    client.delete("/shelf/item-3")

    assert "item-3" in remaining(fake)


def test_delete_removes_only_the_targeted_item(client, patch_supabase, as_user):
    """Removes exactly one row, leaving the caller's other shelf items in
    place — the id filter is applied, not just the ownership filter."""
    as_user("user-1")
    fake = patch_supabase(two_user_shelf(), "app.api.shelf")

    client.delete("/shelf/item-1")

    assert remaining(fake) == ["item-2", "item-3"]


def test_delete_of_an_unknown_id_removes_nothing(client, patch_supabase, as_user):
    """Leaves every row in place when the item id matches nothing at all."""
    as_user("user-1")
    fake = patch_supabase(two_user_shelf(), "app.api.shelf")

    client.delete("/shelf/does-not-exist")

    assert remaining(fake) == ["item-1", "item-2", "item-3"]


def test_delete_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to remove item from shelf."
    and no trace of the internal exception text, when the delete raises."""
    import app.api.shelf as shelf_module

    as_user("user-1")
    patch_supabase(two_user_shelf(), "app.api.shelf")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(shelf_module.supabase, "table", explode)

    resp = client.delete("/shelf/item-1")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to remove item from shelf."


def test_delete_reports_failure_when_nothing_was_removed(client, patch_supabase, as_user):
    """Returns HTTP 404 when the item id belongs to another user, so the caller
    can tell a refused delete from a real one.

    Regression guard for BE-DEF-01: the handler previously discarded the delete
    result and always answered "Item removed successfully", leaving the frontend
    unable to detect that its shelf list was out of sync."""
    as_user("user-1")
    patch_supabase(two_user_shelf(), "app.api.shelf")

    resp = client.delete("/shelf/item-3")
    assert resp.status_code == 404


# ============================================================ Task 6
# PATCH /shelf/{item_id}/open — mark_item_opened()

OPEN_PAYLOAD = {"opened_date": "2026-08-01", "expiration_date": "2027-02-01"}


def opened_shelf(pao=None):
    """One item per user. The caller's carries a pre-existing pao so the
    omit-vs-overwrite branch has something to destroy."""
    mine = shelf_row("item-1", "user-1", "unopened")
    if pao is not None:
        mine["pao"] = pao
    return {"shelf_items": [mine, shelf_row("item-2", "user-2", "unopened", pao=6)]}


def stored_item(fake, item_id):
    return next(row for row in fake.store["shelf_items"] if row["id"] == item_id)


def test_open_activates_the_item_with_the_supplied_dates(client, patch_supabase, as_user):
    """Sets usage_state to "active" and writes the submitted opened_date and
    expiration_date onto the caller's own shelf item."""
    as_user("user-1")
    fake = patch_supabase(opened_shelf(), "app.api.shelf")

    resp = client.patch("/shelf/item-1/open", json=OPEN_PAYLOAD)
    assert resp.status_code == 200

    row = stored_item(fake, "item-1")
    assert row["usage_state"] == "active"
    assert row["opened_date"] == "2026-08-01"
    assert row["expiration_date"] == "2027-02-01"


def test_open_returns_the_updated_row(client, patch_supabase, as_user):
    """Returns the item as it now stands, carrying the active state rather than
    its pre-update values."""
    as_user("user-1")
    patch_supabase(opened_shelf(), "app.api.shelf")

    body = client.patch("/shelf/item-1/open", json=OPEN_PAYLOAD).json()

    assert body["id"] == "item-1"
    assert body["usage_state"] == "active"


def test_open_stores_pao_when_the_request_supplies_it(client, patch_supabase, as_user):
    """Writes the submitted period-after-opening value onto the item."""
    as_user("user-1")
    fake = patch_supabase(opened_shelf(), "app.api.shelf")

    client.patch("/shelf/item-1/open", json={**OPEN_PAYLOAD, "pao": 12})

    assert stored_item(fake, "item-1")["pao"] == 12


def test_open_preserves_an_existing_pao_when_the_request_omits_it(client, patch_supabase, as_user):
    """Leaves a previously stored pao untouched when the request does not supply
    one, rather than overwriting it with null.

    The handler only adds the key `if req.pao is not None`. Sending pao=None
    unconditionally would silently erase a value the caller never referred to."""
    as_user("user-1")
    fake = patch_supabase(opened_shelf(pao=6), "app.api.shelf")

    client.patch("/shelf/item-1/open", json=OPEN_PAYLOAD)

    assert stored_item(fake, "item-1")["pao"] == 6


def test_open_does_not_modify_another_users_item(client, patch_supabase, as_user):
    """Leaves a different user's item entirely unchanged — still unopened, with
    its original pao — when the caller targets that item's id."""
    as_user("user-1")
    fake = patch_supabase(opened_shelf(), "app.api.shelf")

    client.patch("/shelf/item-2/open", json={**OPEN_PAYLOAD, "pao": 99})

    row = stored_item(fake, "item-2")
    assert row["usage_state"] == "unopened"
    assert row["pao"] == 6
    assert "opened_date" not in row


def test_open_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to mark item as opened."
    and no trace of the internal exception text, when the update raises."""
    import app.api.shelf as shelf_module

    as_user("user-1")
    patch_supabase(opened_shelf(), "app.api.shelf")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(shelf_module.supabase, "table", explode)

    resp = client.patch("/shelf/item-1/open", json=OPEN_PAYLOAD)
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to mark item as opened."


def test_open_of_another_users_item_returns_404(client, patch_supabase, as_user):
    """Returns HTTP 404 when the item exists but belongs to another user, rather
    than reporting a server fault for a request that was correctly refused.

    Regression guard for BE-DEF-02: the ownership filter always worked, but
    `response.data[0]` on the empty result raised IndexError into the bare
    except, which answered 500."""
    as_user("user-1")
    patch_supabase(opened_shelf(), "app.api.shelf")

    resp = client.patch("/shelf/item-2/open", json=OPEN_PAYLOAD)
    assert resp.status_code == 404


# ============================================================ Task 7
# PATCH /shelf/{item_id}/status — update_shelf_status()
#
# The request field names differ from the stored column names by design:
#   outcome -> archive_outcome,  notes -> archive_notes,  archived_at as-is.
# That mapping is invisible from outside the handler, so it is asserted here.

ARCHIVE_FIELDS = ("archive_outcome", "archive_notes", "archived_at")

ARCHIVE_PAYLOAD = {
    "usage_state": "archived",
    "outcome": "finished",
    "notes": "Used it all.",
    "archived_at": "2026-08-10T00:00:00+00:00",
}


def status_shelf(**mine):
    return {"shelf_items": [
        shelf_row("item-1", "user-1", "active", **mine),
        shelf_row("item-2", "user-2", "active"),
    ]}


def test_status_archives_the_item_with_its_archive_metadata(client, patch_supabase, as_user):
    """Sets usage_state to "archived" and maps the request's outcome, notes and
    archived_at onto archive_outcome, archive_notes and archived_at."""
    as_user("user-1")
    fake = patch_supabase(status_shelf(), "app.api.shelf")

    resp = client.patch("/shelf/item-1/status", json=ARCHIVE_PAYLOAD)
    assert resp.status_code == 200

    row = stored_item(fake, "item-1")
    assert row["usage_state"] == "archived"
    assert row["archive_outcome"] == "finished"
    assert row["archive_notes"] == "Used it all."
    assert row["archived_at"] == "2026-08-10T00:00:00+00:00"


def test_status_change_other_than_archived_ignores_archive_values(client, patch_supabase, as_user):
    """Leaves archive_outcome, archive_notes and archived_at null for a
    non-archived transition, even when the request body supplies outcome, notes
    and archived_at, so those columns only ever describe a real archive."""
    as_user("user-1")
    fake = patch_supabase(status_shelf(), "app.api.shelf")

    client.patch("/shelf/item-1/status", json={
        "usage_state": "active",
        "outcome": "finished",
        "notes": "should not be stored",
        "archived_at": "2026-08-10T00:00:00+00:00",
    })

    row = stored_item(fake, "item-1")
    assert row["usage_state"] == "active"
    assert all(row.get(field) is None for field in ARCHIVE_FIELDS)


def test_status_archiving_without_optional_fields_stores_nulls(client, patch_supabase, as_user):
    """Stores null archive_outcome and archive_notes when the request archives an
    item without supplying them, rather than rejecting the request."""
    as_user("user-1")
    fake = patch_supabase(status_shelf(), "app.api.shelf")

    resp = client.patch("/shelf/item-1/status", json={"usage_state": "archived"})
    assert resp.status_code == 200

    row = stored_item(fake, "item-1")
    assert row["usage_state"] == "archived"
    assert row["archive_outcome"] is None
    assert row["archive_notes"] is None


def test_status_does_not_modify_another_users_item(client, patch_supabase, as_user):
    """Leaves a different user's item unchanged — still active, with no archive
    metadata — when the caller targets that item's id."""
    as_user("user-1")
    fake = patch_supabase(status_shelf(), "app.api.shelf")

    client.patch("/shelf/item-2/status", json=ARCHIVE_PAYLOAD)

    row = stored_item(fake, "item-2")
    assert row["usage_state"] == "active"
    assert not any(field in row for field in ARCHIVE_FIELDS)


def test_status_requires_a_usage_state(client, patch_supabase, as_user):
    """Returns HTTP 422 and writes nothing when usage_state is absent from the
    request body."""
    as_user("user-1")
    fake = patch_supabase(status_shelf(), "app.api.shelf")

    resp = client.patch("/shelf/item-1/status", json={"notes": "no state given"})

    assert resp.status_code == 422
    assert stored_item(fake, "item-1")["usage_state"] == "active"


def test_status_does_not_leak_internal_errors(client, patch_supabase, as_user, monkeypatch):
    """Returns HTTP 500 with the generic body "Failed to update item status." and
    no trace of the internal exception text, when the update raises."""
    import app.api.shelf as shelf_module

    as_user("user-1")
    patch_supabase(status_shelf(), "app.api.shelf")

    def explode(*_a, **_k):
        raise RuntimeError("connection string postgres://user:hunter2@db.internal")

    monkeypatch.setattr(shelf_module.supabase, "table", explode)

    resp = client.patch("/shelf/item-1/status", json=ARCHIVE_PAYLOAD)
    assert resp.status_code == 500
    assert "hunter2" not in resp.text
    assert resp.json()["detail"] == "Failed to update item status."


def test_status_of_another_users_item_returns_404(client, patch_supabase, as_user):
    """Returns HTTP 404 when the item exists but belongs to another user, rather
    than reporting a server fault for a request that was correctly refused."""
    as_user("user-1")
    patch_supabase(status_shelf(), "app.api.shelf")

    resp = client.patch("/shelf/item-2/status", json=ARCHIVE_PAYLOAD)
    assert resp.status_code == 404


def test_status_clears_archive_metadata_when_un_archiving(client, patch_supabase, as_user):
    """Clears archive_outcome, archive_notes and archived_at when an archived
    item is moved back to active, so the row does not read as an active item
    that also carries a completed archive record.

    Regression guard for BE-DEF-04: those three columns were previously only
    written inside the archived branch and never cleared on the way out."""
    as_user("user-1")
    fake = patch_supabase(status_shelf(
        archive_outcome="finished",
        archive_notes="Used it all.",
        archived_at="2026-08-10T00:00:00+00:00",
    ), "app.api.shelf")

    client.patch("/shelf/item-1/status", json={"usage_state": "active"})

    row = stored_item(fake, "item-1")
    assert all(row.get(field) is None for field in ARCHIVE_FIELDS)
