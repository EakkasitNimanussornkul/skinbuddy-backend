"""The product-submission routes: POST /submissions, /mine, and the admin review.

The database is the in-memory fake (tests/conftest.py). approve_submission()
itself is tested against a local Postgres in tests/postgres; here the approve
route is tested for what it sends the function and how it answers each error.
"""

import copy

import pytest
from postgrest.exceptions import APIError

WATER = "11111111-0000-0000-0000-000000000001"
GLYCERIN = "11111111-0000-0000-0000-000000000002"
SODIUM_PCA = "11111111-0000-0000-0000-000000000003"
UNKNOWN = "99999999-0000-0000-0000-000000000009"
SUB_1 = "5ab00000-0000-0000-0000-000000000001"
SUB_2 = "5ab00000-0000-0000-0000-000000000002"
SUB_3 = "5ab00000-0000-0000-0000-000000000003"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
IMAGE = "submissions/0f8fad5b-d9cb-469f-a165-70867728950e.png"

INGREDIENTS = [
    {"id": WATER, "name": "Water", "functional_group": "Solvent"},
    {"id": GLYCERIN, "name": "Glycerin", "functional_group": "Humectant"},
    {"id": SODIUM_PCA, "name": "Sodium PCA", "functional_group": "Humectant"},
]
PRODUCTS = [{"id": PROD_ID, "slug": "cosrx-snail-mucin-essence", "brand": "COSRX", "name": "Snail Mucin Essence"}]
USERS = [{"id": "admin-1", "role": "admin", "display_name": "Admin"},
         {"id": "user-1", "role": "user", "display_name": "Kla"},
         {"id": "user-2", "role": "user", "display_name": "Other"}]

MODULES = ("app.api.submissions", "app.core.services.submission_service",
           "app.core.services.ingredient_lookup", "app.core.services.image_upload",
           "app.core.services.token")


def body(**overrides):
    base = {
        "name": "Snow Mushroom Gel", "brand": "Glow Lab", "category": "Moisturizers",
        "image_path": IMAGE,
        "ingredients": [{"ingredient_id": WATER},
                        {"new_name": "Tremella Extract",
                         "details": {"roles": ["Moisturising"], "known_for": "Holds water",
                                     "source_url": "https://example.com/tremella"}}],
        "price_thb": 590, "price_usd": None, "pao_months": 12,
        "benefits": ["Hydrating"], "good_for": ["Dry skin"],
        "sources": [{"url": "https://brand.example/gel", "title": "Brand page", "claims": ["listing", "price"]}],
        "note": "Bought in Bangkok",
    }
    base.update(overrides)
    return base


def submission(sid, status="pending", payload=None, edited=None, created_at="2026-10-01T00:00:00+00:00",
               submitted_by="user-1", **extra):
    return {"id": sid, "submitted_by": submitted_by, "status": status,
            "payload": payload if payload is not None else body(), "edited_payload": edited,
            "created_at": created_at, "updated_at": None, "reviewed_at": None, "review_notes": None,
            "reviewed_by": None, "product_id": None,
            "submitter": {"display_name": {"user-1": "Kla", "user-2": "Other"}.get(submitted_by)}, **extra}


@pytest.fixture
def backend(patch_backend):
    def _make(submissions=(), ingredients=INGREDIENTS, products=PRODUCTS):
        return patch_backend({"users": copy.deepcopy(USERS), "ingredients": copy.deepcopy(list(ingredients)),
                              "products": copy.deepcopy(list(products)),
                              "product_submissions": copy.deepcopy(list(submissions))}, *MODULES)
    return _make


# --- POST /submissions --------------------------------------------------------

def test_create_submission_stores_the_body_as_a_pending_payload(client, backend, as_user):
    """Returns HTTP 201 {"id", "status": "pending", "created_at"} and stores one row
    submitted_by the caller, status pending, whose payload is the body as sent: an
    ingredient picked by id keeps only ingredient_id, a typed one keeps new_name and
    details, in the order sent."""
    fake = backend()
    as_user("user-1")
    resp = client.post("/submissions", json=body())
    assert resp.status_code == 201
    [row] = fake.store["product_submissions"]
    assert resp.json() == {"id": row["id"], "status": "pending", "created_at": row["created_at"]}
    assert row["submitted_by"] == "user-1" and row["status"] == "pending"
    assert row["payload"] == body()


def test_create_submission_accepts_every_limit_at_its_maximum(client, backend, as_user):
    """Returns HTTP 201 for 100 ingredients, 8 benefits of 80 characters, all 9 concern
    tags, 5 sources, a 1000-character note and 200-character name and brand."""
    from app.schemas import CONCERN_TAGS
    fake = backend()
    as_user("user-1")
    resp = client.post("/submissions", json=body(
        name="N" * 200, brand="B" * 200,
        ingredients=[{"new_name": f"Ingredient {i}"} for i in range(100)],
        benefits=["b" * 80] * 8, good_for=list(CONCERN_TAGS),
        sources=[{"url": f"http://s{i}.example", "title": "t" * 120, "claims": ["listing"]} for i in range(5)],
        note="n" * 1000))
    assert resp.status_code == 201
    assert len(fake.store["product_submissions"][0]["payload"]["ingredients"]) == 100


INVALID_BODIES = {
    "no ingredients": {"ingredients": []},
    "101 ingredients": {"ingredients": [{"new_name": f"I{i}"} for i in range(101)]},
    "both id and new_name": {"ingredients": [{"ingredient_id": WATER, "new_name": "Water"}]},
    "neither id nor new_name": {"ingredients": [{"details": {"roles": ["Soothing"]}}]},
    "blank new_name": {"ingredients": [{"new_name": "   "}]},
    "new_name over 120": {"ingredients": [{"new_name": "x" * 121}]},
    "details with an id": {"ingredients": [{"ingredient_id": WATER, "details": {"roles": ["Soothing"]}}]},
    "malformed id": {"ingredients": [{"ingredient_id": "not-a-uuid"}]},
    "role not in the list": {"ingredients": [{"new_name": "X", "details": {"roles": ["Magic"]}}]},
    "known_for over 200": {"ingredients": [{"new_name": "X", "details": {"known_for": "k" * 201}}]},
    "ftp source_url": {"ingredients": [{"new_name": "X", "details": {"source_url": "ftp://x.example/a"}}]},
    "javascript source_url": {"ingredients": [{"new_name": "X", "details": {"source_url": "javascript:alert(1)"}}]},
    "claim not in the list": {"sources": [{"url": "https://a.example", "title": "A", "claims": ["benefits"]}]},
    "source with no claims": {"sources": [{"url": "https://a.example", "title": "A", "claims": []}]},
    "source url not http": {"sources": [{"url": "mailto:a@b.c", "title": "A", "claims": ["listing"]}]},
    "source title over 120": {"sources": [{"url": "https://a.example", "title": "t" * 121, "claims": ["listing"]}]},
    "6 sources": {"sources": [{"url": f"https://{i}.example", "title": "A", "claims": ["listing"]} for i in range(6)]},
    "9 benefits": {"benefits": ["b"] * 9},
    "benefit over 80": {"benefits": ["b" * 81]},
    "blank benefit": {"benefits": [" "]},
    "tag not in the list": {"good_for": ["Wrinkles"]},
    "note over 1000": {"note": "n" * 1001},
    "category not in the list": {"category": "Perfume"},
    "pao 3": {"pao_months": 3},
    "negative price": {"price_thb": -1},
    "name over 200": {"name": "n" * 201},
    "blank brand": {"brand": "  "},
    "image_path not an upload": {"image_path": "products/../../etc/passwd"},
    "unknown field": {"ingredient_list": "Water, Glycerin"},
}


@pytest.mark.parametrize("override", list(INVALID_BODIES.values()), ids=list(INVALID_BODIES))
def test_create_submission_refuses_an_invalid_body_with_422(client, backend, as_user, override):
    """Returns HTTP 422 with field errors for each invalid body listed, and stores nothing."""
    fake = backend()
    as_user("user-1")
    resp = client.post("/submissions", json=body(**override))
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)
    assert fake.store["product_submissions"] == []


def test_create_submission_refuses_an_unknown_ingredient_id(client, backend, as_user):
    """Returns HTTP 422 {"detail": "unknown ingredient_id", "code": "SBUNK", "details":
    [the unknown id]} when an ingredient_id is not in the ingredients table, and stores
    nothing."""
    fake = backend()
    as_user("user-1")
    resp = client.post("/submissions", json=body(ingredients=[{"ingredient_id": WATER}, {"ingredient_id": UNKNOWN}]))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "unknown ingredient_id", "code": "SBUNK", "details": [UNKNOWN]}
    assert fake.store["product_submissions"] == []


# --- GET /submissions/mine ----------------------------------------------------

def test_my_submissions_are_newest_first_with_a_summary_and_product_slug(client, backend, as_user):
    """Returns only the caller's submissions, newest first, each with status, review
    fields, product_id, the approved product's slug, and a summary of name, brand,
    category and ingredient count read from the payload merged with the admin's edits."""
    backend([
        submission(SUB_1, created_at="2026-10-01T00:00:00+00:00"),
        submission(SUB_2, status="approved", created_at="2026-10-03T00:00:00+00:00",
                   edited={"name": "Snow Mushroom Gel Cream"}, product_id=PROD_ID,
                   product={"slug": "glow-lab-snow-mushroom-gel-cream"}),
        submission(SUB_3, created_at="2026-10-02T00:00:00+00:00", submitted_by="user-2"),
    ])
    as_user("user-1")
    resp = client.get("/submissions/mine")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [SUB_2, SUB_1]
    newest = resp.json()[0]
    assert newest == {"id": SUB_2, "status": "approved", "created_at": "2026-10-03T00:00:00+00:00",
                      "reviewed_at": None, "review_notes": None, "product_id": PROD_ID,
                      "product_slug": "glow-lab-snow-mushroom-gel-cream",
                      "summary": {"name": "Snow Mushroom Gel Cream", "brand": "Glow Lab",
                                  "category": "Moisturizers", "ingredient_count": 2}}
    assert resp.json()[1]["product_slug"] is None


# --- GET /submissions/admin ---------------------------------------------------

def test_admin_list_is_oldest_first_with_counts_and_flags(client, backend, as_user):
    """Returns {"counts": {"pending": 2, "approved": 1, "rejected": 0}, "submissions": [...]}
    with only the pending rows, oldest first, each with submitter_name, summary and flags:
    new_ingredient_count, has_source and has_photo read from the merged payload."""
    backend([
        submission(SUB_1, created_at="2026-10-02T00:00:00+00:00"),
        submission(SUB_2, created_at="2026-10-01T00:00:00+00:00",
                   payload=body(image_path=None, sources=[], ingredients=[{"ingredient_id": WATER}])),
        submission(SUB_3, status="approved"),
    ])
    as_user("admin-1")
    resp = client.get("/submissions/admin")
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"] == {"pending": 2, "approved": 1, "rejected": 0}
    assert [s["id"] for s in data["submissions"]] == [SUB_2, SUB_1]
    oldest, newer = data["submissions"]
    assert oldest["submitter_name"] == "Kla"
    assert oldest["summary"] == {"name": "Snow Mushroom Gel", "brand": "Glow Lab",
                                 "category": "Moisturizers", "ingredient_count": 1}
    assert oldest["flags"] == {"possible_duplicate": False, "new_ingredient_count": 0,
                               "has_source": False, "has_photo": False}
    assert newer["flags"] == {"possible_duplicate": False, "new_ingredient_count": 1,
                              "has_source": True, "has_photo": True}


def test_admin_list_filters_by_status(client, backend, as_user):
    """Returns only the approved rows for ?status=approved, and HTTP 422 for a status
    outside pending / approved / rejected."""
    backend([submission(SUB_1), submission(SUB_3, status="approved")])
    as_user("admin-1")
    assert [s["id"] for s in client.get("/submissions/admin?status=approved").json()["submissions"]] == [SUB_3]
    assert client.get("/submissions/admin?status=deleted").status_code == 422


def test_admin_list_flags_a_possible_duplicate(client, backend, as_user):
    """Sets possible_duplicate true for a submission whose brand and name match an
    existing product case-insensitively, and for one whose name is close to an existing
    product's under the same brand."""
    backend([submission(SUB_1, payload=body(brand="cosrx", name="SNAIL MUCIN ESSENCE")),
             submission(SUB_2, payload=body(brand="COSRX", name="Snail Mucin Essences"),
                        created_at="2026-10-02T00:00:00+00:00")])
    as_user("admin-1")
    flags = [s["flags"]["possible_duplicate"] for s in client.get("/submissions/admin").json()["submissions"]]
    assert flags == [True, True]


# --- GET /submissions/admin/{id} ----------------------------------------------

def test_admin_detail_merges_edited_payload_over_payload_at_the_top_level(client, backend, as_user):
    """Returns submission = payload with each key in edited_payload replacing the same key
    whole (a top-level merge, as approve_submission() does): an edited "ingredients" list
    replaces the user's list, an edited null clears a value, unedited keys keep the
    user's values. has_edits is true."""
    edited = {"name": "Snow Mushroom Gel Cream", "ingredients": [{"ingredient_id": GLYCERIN}], "note": None}
    backend([submission(SUB_1, edited=edited)])
    as_user("admin-1")
    data = client.get(f"/submissions/admin/{SUB_1}").json()
    assert data["submission"] == {**body(), **edited}
    assert data["has_edits"] is True
    assert data["submitter_name"] == "Kla" and data["status"] == "pending"


def test_admin_detail_numbers_ingredients_from_zero_in_list_order(client, backend, as_user):
    """Returns ingredients numbered 0, 1, 2, ... in the merged list's order: a picked one
    as status "known" with its stored name, a typed one as status "new" with its details,
    and existing_matches naming the row a new name normalises to ("aqua" -> Water) or []
    when approving would insert it."""
    backend([submission(SUB_1, payload=body(ingredients=[
        {"new_name": "Tremella Extract", "details": {"roles": ["Moisturising"]}},
        {"ingredient_id": GLYCERIN},
        {"new_name": "Aqua"},
    ]))])
    as_user("admin-1")
    assert client.get(f"/submissions/admin/{SUB_1}").json()["ingredients"] == [
        {"position": 0, "ingredient_id": None, "name": "Tremella Extract", "status": "new",
         "details": {"roles": ["Moisturising"]}, "existing_matches": []},
        {"position": 1, "ingredient_id": GLYCERIN, "name": "Glycerin", "status": "known",
         "details": None, "existing_matches": []},
        {"position": 2, "ingredient_id": None, "name": "Aqua", "status": "new",
         "details": None, "existing_matches": [{"id": WATER, "name": "Water"}]},
    ]


def test_admin_detail_shows_a_legacy_submissions_names_as_new_ingredients(client, backend, as_user):
    """Returns HTTP 200 for a submission in the old shape (ingredients as plain names):
    each name is listed as status "new", numbered from 0, with no details."""
    legacy = {"name": "Old Toner", "brand": "Old Brand", "category": "Toners",
              "image_url": None, "ingredients": ["Water", "Mystery Extract"]}
    backend([submission(SUB_1, status="rejected", payload=legacy)])
    as_user("admin-1")
    resp = client.get(f"/submissions/admin/{SUB_1}")
    assert resp.status_code == 200
    assert [(i["position"], i["name"], i["status"], i["details"]) for i in resp.json()["ingredients"]] == [
        (0, "Water", "new", None), (1, "Mystery Extract", "new", None)]
    assert resp.json()["ingredients"][0]["existing_matches"] == [{"id": WATER, "name": "Water"}]


def test_admin_detail_lists_exact_and_close_duplicate_candidates(client, backend, as_user):
    """Returns duplicate_candidates [{"id", "slug", "brand", "name", "exact"}]: exact true for
    a case-insensitive brand+name match (approve will answer 409), false for a close name
    under the same brand; a different brand is not listed."""
    products = PRODUCTS + [
        {"id": "p-2", "slug": "cosrx-snail-mucin-essences", "brand": "COSRX", "name": "Snail Mucin Essences"},
        {"id": "p-3", "slug": "other-snail-mucin-essence", "brand": "Other", "name": "Snail Mucin Essence"},
    ]
    backend([submission(SUB_1, payload=body(brand="Cosrx", name="snail mucin essence"))], products=products)
    as_user("admin-1")
    assert client.get(f"/submissions/admin/{SUB_1}").json()["duplicate_candidates"] == [
        {"id": PROD_ID, "slug": "cosrx-snail-mucin-essence", "brand": "COSRX", "name": "Snail Mucin Essence", "exact": True},
        {"id": "p-2", "slug": "cosrx-snail-mucin-essences", "brand": "COSRX", "name": "Snail Mucin Essences", "exact": False},
    ]


def test_admin_detail_counts_a_case_variant_as_exact_even_with_a_hand_written_slug(client, backend, as_user):
    """Lists a product whose brand and name differ only in letter case as an exact
    duplicate, even when its stored slug is hand-written and so does not match."""
    products = [{"id": "p-8", "slug": "cosrx-essence-handmade", "brand": "COSRX", "name": "Snail Mucin Essence"}]
    backend([submission(SUB_1, payload=body(brand="cosrx", name="SNAIL MUCIN ESSENCE"))], products=products)
    as_user("admin-1")
    [candidate] = client.get(f"/submissions/admin/{SUB_1}").json()["duplicate_candidates"]
    assert candidate["id"] == "p-8" and candidate["exact"] is True


def test_admin_detail_counts_a_same_slug_product_as_an_exact_duplicate(client, backend, as_user):
    """Lists a product with the same slug as an exact duplicate even when its stored brand
    and name differ in punctuation, as approve_submission() refuses it."""
    products = [{"id": "p-9", "slug": "glow-lab-snow-mushroom-gel", "brand": "Glow-Lab", "name": "Snow Mushroom Gel!"}]
    backend([submission(SUB_1)], products=products)
    as_user("admin-1")
    [candidate] = client.get(f"/submissions/admin/{SUB_1}").json()["duplicate_candidates"]
    assert candidate["id"] == "p-9" and candidate["exact"] is True


@pytest.mark.parametrize("method, suffix", [("get", ""), ("patch", ""), ("post", "/approve"), ("post", "/reject")])
def test_admin_routes_answer_404_for_a_malformed_id_without_querying(client, backend, as_user, monkeypatch,
                                                                      method, suffix):
    """Returns HTTP 404, not 500, for a submission id that is not a UUID: the id is refused
    before any query, since Postgres would answer such an id with a type error (22P02)."""
    fake = backend([submission(SUB_1)])
    as_user("admin-1")
    table = fake.table

    def refuse_malformed(name):
        if name == "product_submissions":
            raise APIError({"code": "22P02", "message": 'invalid input syntax for type uuid: "not-a-uuid"',
                            "details": None, "hint": None})
        return table(name)
    monkeypatch.setattr(fake, "table", refuse_malformed)
    kwargs = {"json": {}} if method != "get" else {}
    assert getattr(client, method)(f"/submissions/admin/not-a-uuid{suffix}", **kwargs).status_code == 404


def test_admin_detail_answers_404_for_an_unknown_or_malformed_id(client, backend, as_user):
    """Returns HTTP 404 for an id no submission has, and for one that is not a UUID."""
    backend([submission(SUB_1)])
    as_user("admin-1")
    assert client.get(f"/submissions/admin/{SUB_2}").status_code == 404
    assert client.get("/submissions/admin/not-a-uuid").status_code == 404


# --- PATCH /submissions/admin/{id} --------------------------------------------

def test_admin_edit_saves_only_the_fields_sent_on_top_of_earlier_edits(client, backend, as_user):
    """Stores edited_payload = the earlier edits with the sent fields added or replaced,
    leaves payload untouched, and returns the review detail of the merged result."""
    fake = backend([submission(SUB_1, edited={"name": "First Edit"})])
    as_user("admin-1")
    resp = client.patch(f"/submissions/admin/{SUB_1}",
                        json={"brand": "Glow Lab Co", "ingredients": [{"ingredient_id": GLYCERIN}, {"new_name": "Aqua"}],
                              "price_usd": None})
    assert resp.status_code == 200
    row = fake.store["product_submissions"][0]
    assert row["edited_payload"] == {"name": "First Edit", "brand": "Glow Lab Co", "price_usd": None,
                                     "ingredients": [{"ingredient_id": GLYCERIN}, {"new_name": "Aqua"}]}
    assert row["payload"] == body()
    assert resp.json()["submission"]["name"] == "First Edit"
    assert [i["status"] for i in resp.json()["ingredients"]] == ["known", "new"]


def test_admin_edit_turns_a_legacy_submission_into_the_new_shape(client, backend, as_user):
    """Saving an ingredients list of objects over a legacy submission makes the merged
    payload's ingredients the new shape, which approve accepts."""
    legacy = {"name": "Old Toner", "brand": "Old Brand", "category": "Toners", "ingredients": ["Water", "Mystery"]}
    backend([submission(SUB_1, payload=legacy)])
    as_user("admin-1")
    resp = client.patch(f"/submissions/admin/{SUB_1}",
                        json={"ingredients": [{"ingredient_id": WATER}, {"new_name": "Mystery"}]})
    assert resp.json()["submission"]["ingredients"] == [{"ingredient_id": WATER}, {"new_name": "Mystery"}]


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_admin_edit_answers_409_when_the_submission_is_not_pending(client, backend, as_user, status):
    """Returns HTTP 409 {"detail": "the submission is <status>, not pending", "code":
    "SBNPD"} and changes nothing."""
    fake = backend([submission(SUB_1, status=status)])
    as_user("admin-1")
    resp = client.patch(f"/submissions/admin/{SUB_1}", json={"name": "New"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": f"the submission is {status}, not pending", "code": "SBNPD"}
    assert fake.store["product_submissions"][0]["edited_payload"] is None


@pytest.mark.parametrize("edit", [
    {"name": None}, {"ingredients": None}, {"ingredients": []}, {"category": "Perfume"},
    {"good_for": ["Wrinkles"]}, {"ingredients": [{"ingredient_id": WATER, "new_name": "Water"}]},
    {"image_path": "elsewhere/x.png"}, {"surprise": 1},
], ids=["null name", "null ingredients", "no ingredients", "bad category", "bad tag",
        "both id and name", "foreign image_path", "unknown field"])
def test_admin_edit_refuses_an_invalid_edit_with_422(client, backend, as_user, edit):
    """Returns HTTP 422 for an edit the POST body's rules refuse, or that nulls a field
    that needs a value, and changes nothing."""
    fake = backend([submission(SUB_1)])
    as_user("admin-1")
    assert client.patch(f"/submissions/admin/{SUB_1}", json=edit).status_code == 422
    assert fake.store["product_submissions"][0]["edited_payload"] is None


def test_admin_edit_refuses_an_unknown_ingredient_id(client, backend, as_user):
    """Returns HTTP 422 with code SBUNK for an ingredient_id not in the ingredients table,
    and changes nothing."""
    fake = backend([submission(SUB_1)])
    as_user("admin-1")
    resp = client.patch(f"/submissions/admin/{SUB_1}", json={"ingredients": [{"ingredient_id": UNKNOWN}]})
    assert resp.status_code == 422 and resp.json()["code"] == "SBUNK"
    assert fake.store["product_submissions"][0]["edited_payload"] is None


# --- POST /submissions/admin/{id}/reject --------------------------------------

def test_reject_records_the_decision(client, backend, as_user):
    """Returns HTTP 200 {"id", "status": "rejected", "reviewed_at", "review_notes"} and stores
    status rejected, the notes, reviewed_by = the admin and a reviewed_at time."""
    fake = backend([submission(SUB_1)])
    as_user("admin-1")
    resp = client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": "Already listed"})
    assert resp.status_code == 200
    row = fake.store["product_submissions"][0]
    assert row["status"] == "rejected" and row["review_notes"] == "Already listed"
    assert row["reviewed_by"] == "admin-1" and row["reviewed_at"]
    assert resp.json() == {"id": SUB_1, "status": "rejected", "reviewed_at": row["reviewed_at"],
                           "review_notes": "Already listed"}


def test_reject_answers_409_when_not_pending(client, backend, as_user):
    """Returns HTTP 409 {"detail": "the submission is approved, not pending", "code": "SBNPD"}
    for a submission already approved, and changes nothing."""
    fake = backend([submission(SUB_1, status="approved")])
    as_user("admin-1")
    resp = client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": None})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "the submission is approved, not pending", "code": "SBNPD"}
    assert fake.store["product_submissions"][0]["status"] == "approved"


def test_reject_refuses_notes_over_1000_characters(client, backend, as_user):
    """Returns HTTP 422 for review_notes longer than 1000 characters."""
    backend([submission(SUB_1)])
    as_user("admin-1")
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": "n" * 1001}).status_code == 422


# --- POST /submissions/admin/{id}/approve -------------------------------------

APPROVE_BODY = {
    "publish_benefits": ["Hydrating"], "publish_good_for": ["Dry skin"],
    "publish_source_urls": ["https://brand.example/gel"],
    "new_ingredients": [{"position": 1, "decision": "with_details", "functional_group": "Humectant",
                         "benefits": "Holds water"}],
}


def test_approve_passes_the_body_to_the_function_as_sent(client, backend, as_user):
    """Calls approve_submission once with the submission id, the admin's id, the body
    exactly as sent, and the public URL of the merged payload's image_path; returns the
    function's {"product_id", "slug"} with HTTP 200."""
    fake = backend([submission(SUB_1, edited={"image_path": IMAGE})])
    fake.rpc_results["approve_submission"] = {"product_id": "new-product", "slug": "glow-lab-snow-mushroom-gel"}
    as_user("admin-1")
    resp = client.post(f"/submissions/admin/{SUB_1}/approve", json=APPROVE_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"product_id": "new-product", "slug": "glow-lab-snow-mushroom-gel"}
    assert fake.rpc_calls == [("approve_submission", {
        "p_submission_id": SUB_1, "p_admin_id": "admin-1", "p_decisions": APPROVE_BODY,
        "p_image_url": f"https://test-project.supabase.co/storage/v1/object/public/product-images/{IMAGE}",
    })]


def test_approve_sends_only_the_keys_the_admin_sent(client, backend, as_user):
    """Passes {"new_ingredients": [{"position": 0, "decision": "drop"}]} through unchanged,
    adding no empty lists and no null fields, and passes p_image_url null when the
    submission has no image."""
    fake = backend([submission(SUB_1, payload=body(image_path=None))])
    fake.rpc_results["approve_submission"] = {"product_id": "p", "slug": "s"}
    as_user("admin-1")
    sent = {"new_ingredients": [{"position": 0, "decision": "drop"}]}
    client.post(f"/submissions/admin/{SUB_1}/approve", json=sent)
    [(_, params)] = fake.rpc_calls
    assert params["p_decisions"] == sent
    assert params["p_image_url"] is None


def rpc_error(code, message="message from the database", details=None):
    return APIError({"code": code, "message": message, "details": details, "hint": None})


CANDIDATES = [{"id": PROD_ID, "slug": "cosrx-snail-mucin-essence", "brand": "COSRX", "name": "Snail Mucin Essence"}]

APPROVE_ERRORS = [
    ("SBDUP", "duplicate", '[{"id": "%s", "slug": "cosrx-snail-mucin-essence", "brand": "COSRX", "name": "Snail Mucin Essence"}]' % PROD_ID,
     409, {"detail": "duplicate", "candidates": CANDIDATES}),
    ("23505", "duplicate key value violates unique constraint", "Key exists.",
     409, {"detail": "duplicate", "candidates": []}),
    ("SBDEC", "the new ingredient at position 1 (\"X\") has no decision", None,
     422, {"detail": "the new ingredient at position 1 (\"X\") has no decision", "code": "SBDEC"}),
    ("SBNON", "every ingredient was dropped", None,
     422, {"detail": "every ingredient was dropped", "code": "SBNON"}),
    ("SBLEG", "this submission uses the old format", None,
     422, {"detail": "this submission uses the old format", "code": "SBLEG"}),
    ("SBAMB", "\"Aqua\" (position 0) matches more than one ingredient", '[{"id": "a", "name": "Water"}, {"id": "b", "name": "water"}]',
     422, {"detail": "\"Aqua\" (position 0) matches more than one ingredient", "code": "SBAMB",
           "details": [{"id": "a", "name": "Water"}, {"id": "b", "name": "water"}]}),
    ("SBFGR", "\"Magic\" is not an existing functional_group", None,
     422, {"detail": "\"Magic\" is not an existing functional_group", "code": "SBFGR"}),
    ("SBUNK", "unknown ingredient_id", '["%s"]' % UNKNOWN,
     422, {"detail": "unknown ingredient_id", "code": "SBUNK", "details": [UNKNOWN]}),
    ("SBVAL", "publish_benefits names a benefit the submission does not list", None,
     422, {"detail": "publish_benefits names a benefit the submission does not list", "code": "SBVAL"}),
    ("23514", "new row violates check constraint", "Failing row contains (...).",
     422, {"detail": "new row violates check constraint", "code": "23514"}),
    ("SBNFD", "submission not found", None, 404, {"detail": "submission not found", "code": "SBNFD"}),
    ("SBNPD", "the submission is approved, not pending", None,
     409, {"detail": "the submission is approved, not pending", "code": "SBNPD"}),
    ("SBADM", "the reviewer is not an admin", None, 403, {"detail": "the reviewer is not an admin", "code": "SBADM"}),
    ("XX000", "internal error", None, 500, {"detail": "The database refused the change."}),
]


@pytest.mark.parametrize("code, message, details, status, expected", APPROVE_ERRORS,
                         ids=[e[0] for e in APPROVE_ERRORS])
def test_approve_maps_each_database_error_to_its_http_answer(client, backend, as_user,
                                                             code, message, details, status, expected):
    """Returns the HTTP status and body 0013's error table gives each code: 409 with the
    candidates for SBDUP, 409 duplicate for a unique-index race (23505), 422 with the
    message (and the DETAIL when it is JSON) for invalid input, 404 SBNFD, 409 SBNPD,
    403 SBADM, and 500 for anything else."""
    fake = backend([submission(SUB_1)])
    fake.rpc_results["approve_submission"] = rpc_error(code, message, details)
    as_user("admin-1")
    resp = client.post(f"/submissions/admin/{SUB_1}/approve", json=APPROVE_BODY)
    assert resp.status_code == status
    assert resp.json() == expected


def test_approve_answers_404_for_an_unknown_submission_without_calling_the_function(client, backend, as_user):
    """Returns HTTP 404 for an id no submission has, and never calls approve_submission."""
    fake = backend([])
    as_user("admin-1")
    assert client.post(f"/submissions/admin/{SUB_1}/approve", json=APPROVE_BODY).status_code == 404
    assert fake.rpc_calls == []
