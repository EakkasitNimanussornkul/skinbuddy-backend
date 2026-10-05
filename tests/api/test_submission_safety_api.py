"""Submission hardening through the routes: link refusals as field-level 422s,
invisible characters cleaned from stored text, the cap on pending submissions,
and the approve route's check of the stored image_path. The database is the
in-memory fake (tests/conftest.py).
"""

import copy

import pytest

WATER = "11111111-0000-0000-0000-000000000001"
SUB_1 = "5ab00000-0000-0000-0000-000000000001"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
IMAGE = "submissions/0f8fad5b-d9cb-469f-a165-70867728950e.png"
LOADED_AT = "2026-10-04T08:15:30.123456+00:00"
LOCAL_MSG = "Value error, must not point at a local or internal host"
PENDING_MSG = ("You already have 10 submissions waiting for review. "
               "You can send another once an admin has reviewed one.")

MODULES = ("app.api.submissions", "app.api.products", "app.core.services.submission_service",
           "app.core.services.ingredient_lookup", "app.core.services.image_upload",
           "app.core.services.token")


def body(**overrides):
    base = {
        "name": "Snow Mushroom Gel", "brand": "Glow Lab", "category": "Moisturizers",
        "ingredients": [{"ingredient_id": WATER},
                        {"new_name": "Tremella Extract", "details": {"source_url": "https://example.com/tremella"}}],
        "sources": [{"url": "https://brand.example/gel", "title": "Brand page", "claims": ["listing"]}],
    }
    base.update(overrides)
    return base


def row(sid, status="pending", submitted_by="user-1", payload=None):
    return {"id": sid, "submitted_by": submitted_by, "status": status, "payload": payload or body(),
            "edited_payload": None, "created_at": "2026-10-01T00:00:00+00:00", "updated_at": None,
            "reviewed_at": None, "review_notes": None, "reviewed_by": None, "product_id": None,
            "submitter": {"display_name": "Kla"}}


@pytest.fixture
def backend(patch_backend):
    def _make(submissions=()):
        fake = patch_backend({
            "users": [{"id": "admin-1", "role": "admin", "skin_type": None}, {"id": "user-1", "role": "user"}],
            "ingredients": [{"id": WATER, "name": "Water", "functional_group": "Solvent"}],
            "products": [{"id": PROD_ID, "slug": "cosrx-snail", "brand": "COSRX", "name": "Snail",
                          "updated_at": LOADED_AT, "product_ingredients": [], "product_sources": []}],
            "product_submissions": copy.deepcopy(list(submissions)),
        }, *MODULES)
        fake.rpc_results["approve_submission"] = {"product_id": "p", "slug": "s"}
        fake.rpc_results["admin_update_product"] = {"product_id": PROD_ID, "updated_at": LOADED_AT, "slug": "s"}
        return fake
    return _make


def only_error(resp):
    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    return error["loc"], error["msg"]


# --- Links: a field-level 422 with a loc, on every route that takes one -------------

def test_post_submission_refuses_an_internal_link_at_its_field(client, backend, as_user):
    """POST /submissions answers HTTP 422 with one pydantic error whose loc names the
    field: ["body", "sources", 0, "url"] for a source and ["body", "ingredients", 1,
    "details", "source_url"] for an ingredient's link, and msg "Value error, must not
    point at a local or internal host"; nothing is stored."""
    fake = backend()
    as_user("user-1")
    bad = [{"url": "http://printer.local/x", "title": "T", "claims": ["listing"]}]
    assert only_error(client.post("/submissions", json=body(sources=bad))) == (
        ["body", "sources", 0, "url"], LOCAL_MSG)
    ings = [{"ingredient_id": WATER}, {"new_name": "X", "details": {"source_url": "http://localhost/"}}]
    assert only_error(client.post("/submissions", json=body(ingredients=ings))) == (
        ["body", "ingredients", 1, "details", "source_url"], LOCAL_MSG)
    assert fake.store["product_submissions"] == []


def test_admin_edit_and_product_patch_refuse_an_internal_link_at_its_field(client, backend, as_user):
    """PATCH /submissions/admin/{id} and PATCH /products/{id} answer HTTP 422 with loc
    ["body", "sources", 0, "url"] for a source at 127.0.0.1, and change nothing."""
    fake = backend([row(SUB_1)])
    as_user("admin-1")
    bad = [{"url": "http://127.0.0.1/admin", "title": "T", "claims": ["listing"]}]
    private = "Value error, must not point at a private, loopback, link-local or reserved IP address"
    assert only_error(client.patch(f"/submissions/admin/{SUB_1}", json={"sources": bad})) == (
        ["body", "sources", 0, "url"], private)
    assert only_error(client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "sources": bad})) == (
        ["body", "sources", 0, "url"], private)
    assert fake.store["product_submissions"][0]["edited_payload"] is None
    assert fake.rpc_calls == []


def test_approve_refuses_an_internal_publish_source_url_at_its_index(client, backend, as_user):
    """POST /submissions/admin/{id}/approve answers HTTP 422 with loc ["body",
    "publish_source_urls", 1] for the second ticked URL when it is http://intranet/,
    and does not call approve_submission; a public URL is passed on unchanged."""
    fake = backend([row(SUB_1)])
    as_user("admin-1")
    resp = client.post(f"/submissions/admin/{SUB_1}/approve",
                       json={"publish_source_urls": ["https://brand.example/gel", "http://intranet/"]})
    assert only_error(resp) == (["body", "publish_source_urls", 1],
                                "Value error, must use a full public host name, like example.com")
    assert fake.rpc_calls == []
    client.post(f"/submissions/admin/{SUB_1}/approve", json={"publish_source_urls": ["https://brand.example/gel"]})
    assert fake.rpc_calls[0][1]["p_decisions"] == {"publish_source_urls": ["https://brand.example/gel"]}


# --- Text cleaning -----------------------------------------------------------------

def test_submitted_text_is_stored_without_invisible_or_control_characters(client, backend, as_user):
    """POST /submissions stores name, brand, benefits, ingredient names, known_for,
    source titles and the note with zero-width and bidi-override characters removed,
    a line break in a name turned into a space, and the note's line breaks kept."""
    fake = backend()
    as_user("user-1")
    sent = body(name="Snow\u202E Mushroom\u200B Gel", brand="Glow\nLab\uFEFF", benefits=["Hydr\u200Dating"],
                ingredients=[{"new_name": "Trem\u2066ella", "details": {"known_for": "holds\u200C water"}}],
                sources=[{"url": "https://brand.example/gel", "title": "Brand\u2069 page", "claims": ["listing"]}],
                note="Bought in\r\nBangkok\u202E")
    assert client.post("/submissions", json=sent).status_code == 201
    payload = fake.store["product_submissions"][0]["payload"]
    assert payload["name"] == "Snow Mushroom Gel" and payload["brand"] == "Glow Lab"
    assert payload["benefits"] == ["Hydrating"]
    assert payload["ingredients"] == [{"new_name": "Tremella", "details": {"roles": [], "known_for": "holds water"}}]
    assert payload["sources"][0]["title"] == "Brand page"
    assert payload["note"] == "Bought in\nBangkok"


def test_a_name_of_only_invisible_characters_is_refused_as_empty(client, backend, as_user):
    """POST /submissions answers HTTP 422 at loc ["body", "name"] (string_too_short)
    for a name made only of zero-width spaces and a right-to-left override."""
    backend()
    as_user("user-1")
    resp = client.post("/submissions", json=body(name="\u200B\u200B\u202E"))
    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert error["loc"] == ["body", "name"] and error["type"] == "string_too_short"


def test_reject_notes_and_product_description_are_cleaned(client, backend, as_user):
    """POST /submissions/admin/{id}/reject stores review_notes, and PATCH /products/{id}
    sends description, with the bidi override removed and line breaks kept."""
    fake = backend([row(SUB_1)])
    as_user("admin-1")
    client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": "Dup\u202Elicate\nof X"})
    assert fake.store["product_submissions"][0]["review_notes"] == "Duplicate\nof X"
    client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "description": "Line 1\r\nLine\u200B 2"})
    assert fake.rpc_calls[-1][1]["p_patch"] == {"description": "Line 1\nLine 2"}


# --- Pending cap ----------------------------------------------------------------------

def test_an_eleventh_pending_submission_is_refused_with_429(client, backend, as_user):
    """POST /submissions answers HTTP 429 {"detail": "You already have 10 submissions
    waiting for review. You can send another once an admin has reviewed one."} for a
    user with 10 pending submissions, and stores nothing."""
    fake = backend([row(f"5ab00000-0000-0000-0000-0000000000{i:02d}") for i in range(10)])
    as_user("user-1")
    resp = client.post("/submissions", json=body())
    assert resp.status_code == 429
    assert resp.json() == {"detail": PENDING_MSG}
    assert len(fake.store["product_submissions"]) == 10


def test_the_pending_cap_counts_only_the_callers_pending_submissions(client, backend, as_user):
    """POST /submissions answers HTTP 201 for a user with 9 pending submissions, however
    many approved or rejected ones they have and however many pending ones other users
    have."""
    rows = ([row(f"5ab00000-0000-0000-0000-0000000000{i:02d}") for i in range(9)]
            + [row(f"5ab00000-0000-0000-0000-0000000001{i:02d}", status=s) for i, s in enumerate(["approved", "rejected"] * 3)]
            + [row(f"5ab00000-0000-0000-0000-0000000002{i:02d}", submitted_by="user-2") for i in range(10)])
    backend(rows)
    as_user("user-1")
    assert client.post("/submissions", json=body()).status_code == 201


# --- Approve: the stored image_path -------------------------------------------------

@pytest.mark.parametrize("stored", ["https://evil.example/x.png", "submissions/../products/x.png",
                                    IMAGE + "\n", "products/0f8fad5b-d9cb-469f-a165-70867728950e.png", 42])
def test_approve_refuses_a_stored_image_path_that_is_not_a_submission_upload(client, backend, as_user, stored):
    """POST /submissions/admin/{id}/approve answers HTTP 422 {"detail": "The
    submission's image_path is not an uploaded image. Save a new photo with PATCH
    /submissions/admin/{id} first."} when the stored payload's image_path is a URL, a
    traversal, has a trailing newline, is under products/, or is not a string; it does
    not call approve_submission."""
    fake = backend([row(SUB_1, payload=body(image_path=stored))])
    as_user("admin-1")
    resp = client.post(f"/submissions/admin/{SUB_1}/approve", json={})
    assert resp.status_code == 422
    assert resp.json() == {"detail": "The submission's image_path is not an uploaded image. "
                                     "Save a new photo with PATCH /submissions/admin/{id} first."}
    assert fake.rpc_calls == []
