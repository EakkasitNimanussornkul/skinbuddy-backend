"""Deleting a photo once nothing uses it: on reject, and when an admin edit
replaces or clears it (POST /submissions/admin/{id}/reject, PATCH
/submissions/admin/{id}, PATCH /products/{id}).

The database and the bucket are the in-memory fakes in tests/conftest.py. A
photo is "in use" while a product's image_url or a pending or approved
submission's effective image_path points at it; a storage failure never fails
the request.
"""

import copy

import pytest
from postgrest.exceptions import APIError

from app.core.services import image_upload

BUCKET = "product-images"
PUBLIC = "https://test-project.supabase.co/storage/v1/object/public/product-images/"
SUB_1 = "5ab00000-0000-0000-0000-000000000001"
SUB_2 = "5ab00000-0000-0000-0000-000000000002"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROD_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WATER = "11111111-0000-0000-0000-000000000001"
LOADED_AT = "2026-10-04T08:15:30.123456+00:00"
SAVED_AT = "2026-10-04T09:00:00.654321+00:00"

PHOTO = "submissions/0f8fad5b-d9cb-469f-a165-70867728950e.png"
NEW_PHOTO = "submissions/7c9e6679-7425-40de-944b-e07fc1f90ae7.jpg"
PRODUCT_PHOTO = "products/16fd2706-8baf-433b-82eb-8c7fada847da.webp"
NEW_PRODUCT_PHOTO = "products/886313e1-3b8a-5372-9b90-0c9aee199e5d.png"

MODULES = ("app.api.submissions", "app.api.products", "app.core.services.submission_service",
           "app.core.services.ingredient_lookup", "app.core.services.image_upload",
           "app.core.services.token")


def payload(image_path=PHOTO, **overrides):
    base = {"name": "Snow Mushroom Gel", "brand": "Glow Lab", "category": "Moisturizers",
            "image_path": image_path, "ingredients": [{"ingredient_id": WATER}],
            "benefits": [], "good_for": [], "sources": []}
    base.update(overrides)
    return base


def submission(sid, status="pending", image_path=PHOTO, edited=None):
    return {"id": sid, "submitted_by": "user-1", "status": status, "payload": payload(image_path),
            "edited_payload": edited, "created_at": "2026-10-01T00:00:00+00:00", "updated_at": None,
            "reviewed_at": None, "review_notes": None, "reviewed_by": None, "product_id": None,
            "submitter": {"display_name": "Kla"}}


def product(pid=PROD_ID, image_url=None):
    return {"id": pid, "brand": "COSRX", "name": "Snail Mucin Essence", "category": "Treatments",
            "slug": f"cosrx-snail-mucin-essence-{pid[:4]}", "description": None, "price_thb": 690,
            "price_usd": 25, "image_url": image_url, "benefits": [], "good_for": [], "pao_months": None,
            "updated_at": LOADED_AT, "product_sources": [],
            "product_ingredients": [{"ingredients": {"id": WATER, "name": "Water", "good_for": "All Skin Types"}}]}


@pytest.fixture
def backend(patch_backend, as_user):
    """A fake database and bucket, with the caller logged in as an admin."""
    as_user("admin-1")

    def _make(submissions=(), products=(), stored=()):
        fake = patch_backend({"users": [{"id": "admin-1", "role": "admin", "skin_type": None}],
                              "ingredients": [{"id": WATER, "name": "Water", "functional_group": "Solvent"}],
                              "products": copy.deepcopy(list(products)),
                              "product_submissions": copy.deepcopy(list(submissions))}, *MODULES)
        for key in stored:
            fake.storage.put(key, hours_old=1)
        fake.rpc_results["admin_update_product"] = {"product_id": PROD_ID, "updated_at": SAVED_AT,
                                                    "slug": "cosrx-snail-mucin-essence"}
        _apply_saved_image(fake)
        return fake
    return _make


def _apply_saved_image(fake):
    """Makes the fake admin_update_product do what the real one does to the
    image: set the product's image_url, but only when the call succeeds. Without
    it the product would still show its old photo after the edit, and every
    "keeps the photo" test would pass for the wrong reason."""
    original = fake.rpc

    def rpc(name, params):
        call = original(name, params)
        execute = call.execute

        def execute_and_save():
            result = execute()                     # raises for an error result
            if name == "admin_update_product" and "image_url" in params["p_patch"]:
                for row in fake.store["products"]:
                    if row["id"] == params["p_product_id"]:
                        row["image_url"] = params["p_patch"]["image_url"]
            return result
        call.execute = execute_and_save
        return call
    fake.rpc = rpc


def row_of(fake, sid):
    return next(r for r in fake.store["product_submissions"] if r["id"] == sid)


def removed_keys(fake):
    return [key for _bucket, keys in fake.storage.removals for key in keys]


# --- POST /submissions/admin/{id}/reject ------------------------------------------

def test_reject_deletes_the_unused_photo_and_records_it_as_gone(client, backend):
    """Returns HTTP 200 with the same body as before ({"id", "status": "rejected",
    "reviewed_at", "review_notes"}), deletes the submission's photo from the
    product-images bucket, and records it as gone with edited_payload.image_path =
    null in the same row; the user's payload still holds the original path."""
    fake = backend([submission(SUB_1)], stored=[PHOTO])
    resp = client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": "Not a skincare product"})
    assert resp.status_code == 200
    row = row_of(fake, SUB_1)
    assert resp.json() == {"id": SUB_1, "status": "rejected", "reviewed_at": row["reviewed_at"],
                           "review_notes": "Not a skincare product"}
    assert fake.storage.removals == [(BUCKET, [PHOTO])]
    assert fake.storage.keys() == []
    assert row["status"] == "rejected"
    assert row["edited_payload"] == {"image_path": None}
    assert row["payload"]["image_path"] == PHOTO


def test_rejected_submission_detail_shows_no_photo(client, backend):
    """After a reject, GET /submissions/admin/{id} returns "submission" with
    "image_path": null and "has_edits": true, and the rejected list's
    flags.has_photo is false."""
    backend([submission(SUB_1)], stored=[PHOTO])
    client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": None})
    detail = client.get(f"/submissions/admin/{SUB_1}").json()
    assert detail["submission"]["image_path"] is None
    assert detail["has_edits"] is True
    listed = client.get("/submissions/admin?status=rejected").json()["submissions"]
    assert [item["flags"]["has_photo"] for item in listed] == [False]


def test_reject_deletes_the_admins_replacement_photo_and_keeps_other_edits(client, backend):
    """Deletes the effective photo, the one the admin's edit set (edited_payload
    wins over payload), not the user's original, and keeps the admin's other
    edits: edited_payload becomes {"name": ..., "image_path": null}."""
    fake = backend([submission(SUB_1, edited={"name": "Snow Mushroom Gel Cream", "image_path": NEW_PHOTO})],
                   stored=[PHOTO, NEW_PHOTO])
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 200
    assert removed_keys(fake) == [NEW_PHOTO]
    assert fake.storage.keys() == [PHOTO]
    assert row_of(fake, SUB_1)["edited_payload"] == {"name": "Snow Mushroom Gel Cream", "image_path": None}


@pytest.mark.parametrize("other_status", ["pending", "approved"])
def test_reject_keeps_a_photo_another_live_submission_uses(client, backend, other_status):
    """Deletes nothing when another pending or approved submission's effective
    image_path is the same photo; the reject still answers 200 and records
    image_path null on the rejected row."""
    fake = backend([submission(SUB_1), submission(SUB_2, status=other_status)], stored=[PHOTO])
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 200
    assert fake.storage.removals == []
    assert fake.storage.keys() == [PHOTO]
    assert row_of(fake, SUB_1)["edited_payload"] == {"image_path": None}


def test_reject_keeps_a_photo_another_submission_holds_through_its_edits(client, backend):
    """Deletes nothing when another pending submission uses the photo through
    its edited_payload rather than its payload."""
    fake = backend([submission(SUB_1), submission(SUB_2, image_path=None, edited={"image_path": PHOTO})],
                   stored=[PHOTO])
    client.post(f"/submissions/admin/{SUB_1}/reject", json={})
    assert fake.storage.keys() == [PHOTO]


def test_reject_deletes_a_photo_only_a_rejected_submission_also_names(client, backend):
    """Deletes the photo when the only other submission naming it is rejected:
    a rejected submission does not keep a photo."""
    fake = backend([submission(SUB_1), submission(SUB_2, status="rejected")], stored=[PHOTO])
    client.post(f"/submissions/admin/{SUB_1}/reject", json={})
    assert fake.storage.keys() == []


def test_reject_keeps_a_photo_a_product_uses(client, backend):
    """Deletes nothing when a product's image_url is the photo's public URL."""
    fake = backend([submission(SUB_1)], products=[product(image_url=PUBLIC + PHOTO)], stored=[PHOTO])
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 200
    assert fake.storage.removals == []
    assert fake.storage.keys() == [PHOTO]


def test_reject_still_succeeds_when_storage_fails(client, backend):
    """Returns HTTP 200 with status "rejected" when the bucket refuses the
    deletion: the row is rejected with image_path null, and the file stays for
    the cleanup route to delete later."""
    fake = backend([submission(SUB_1)], stored=[PHOTO])
    fake.storage.remove_error = RuntimeError("storage is down")
    resp = client.post(f"/submissions/admin/{SUB_1}/reject", json={"review_notes": "Duplicate"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    row = row_of(fake, SUB_1)
    assert row["status"] == "rejected" and row["edited_payload"] == {"image_path": None}
    assert fake.storage.removals == [(BUCKET, [PHOTO])]
    assert fake.storage.keys() == [PHOTO]


def test_reject_deletes_nothing_when_the_in_use_check_fails(client, backend, monkeypatch):
    """Returns HTTP 200 and deletes nothing when reading which photos are in use
    fails: a photo is never deleted without that check."""
    fake = backend([submission(SUB_1)], stored=[PHOTO])

    def broken():
        raise RuntimeError("database is down")
    monkeypatch.setattr(image_upload, "referenced_upload_paths", broken)
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 200
    assert fake.storage.removals == []
    assert row_of(fake, SUB_1)["status"] == "rejected"


@pytest.mark.parametrize("stored_path", ["Products/Biore UV Aqua Rich Watery Essence.png", PUBLIC + PHOTO,
                                         "submissions/not-a-uuid.png"],
                         ids=["seed image key", "public URL", "not a uuid"])
def test_reject_never_deletes_an_image_path_that_is_not_an_upload(client, backend, stored_path):
    """Deletes nothing when the stored image_path is not an upload path
    (<submissions|products>/<uuid>.<jpg|png|webp>), such as a seed image's key or
    a URL saved before uploads were checked; the reject still answers 200 and
    records image_path null."""
    fake = backend([submission(SUB_1, image_path=stored_path)],
                   stored=["Products/Biore UV Aqua Rich Watery Essence.png", PHOTO, "submissions/not-a-uuid.png"])
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 200
    assert fake.storage.removals == []
    assert len(fake.storage.keys()) == 3
    assert row_of(fake, SUB_1)["edited_payload"] == {"image_path": None}


def test_reject_without_a_photo_changes_no_edits(client, backend):
    """Leaves edited_payload as it was (null) and deletes nothing when the
    submission has no photo."""
    fake = backend([submission(SUB_1, image_path=None)])
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 200
    assert row_of(fake, SUB_1)["edited_payload"] is None
    assert fake.storage.removals == []


def test_reject_of_a_reviewed_submission_deletes_nothing(client, backend):
    """Returns HTTP 409 SBNPD for a submission already approved, and neither
    deletes its photo nor changes its row."""
    fake = backend([submission(SUB_1, status="approved")], stored=[PHOTO])
    assert client.post(f"/submissions/admin/{SUB_1}/reject", json={}).status_code == 409
    assert fake.storage.removals == []
    assert row_of(fake, SUB_1)["edited_payload"] is None


def test_reject_that_loses_a_race_deletes_nothing(client, backend, monkeypatch):
    """Returns HTTP 409 SBNPD and deletes nothing when another admin reviewed the
    submission between the read and the write (the status update matches no
    row): the photo is deleted only after this reject's own update succeeds."""
    from app.api import submissions as submissions_api
    fake = backend([submission(SUB_1, status="rejected")], stored=[PHOTO])
    pending_copy = submission(SUB_1)                 # what the read saw, before the other review
    monkeypatch.setattr(submissions_api, "_load_submission", lambda _sid: copy.deepcopy(pending_copy))
    resp = client.post(f"/submissions/admin/{SUB_1}/reject", json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "SBNPD"
    assert fake.storage.removals == []
    assert fake.storage.keys() == [PHOTO]


# --- PATCH /submissions/admin/{id} ------------------------------------------------

def test_submission_edit_replacing_the_photo_deletes_the_old_one(client, backend):
    """Returns HTTP 200 with the detail showing the new image_path, and deletes the
    photo it replaced; the new photo stays."""
    fake = backend([submission(SUB_1)], stored=[PHOTO, NEW_PHOTO])
    resp = client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": NEW_PHOTO})
    assert resp.status_code == 200
    assert resp.json()["submission"]["image_path"] == NEW_PHOTO
    assert fake.storage.removals == [(BUCKET, [PHOTO])]
    assert fake.storage.keys() == [NEW_PHOTO]


def test_submission_edit_clearing_the_photo_deletes_it(client, backend):
    """Returns HTTP 200 with "image_path": null and deletes the photo cleared."""
    fake = backend([submission(SUB_1)], stored=[PHOTO])
    resp = client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": None})
    assert resp.status_code == 200
    assert resp.json()["submission"]["image_path"] is None
    assert fake.storage.keys() == []


def test_submission_edit_replacing_an_earlier_replacement_deletes_that_one(client, backend):
    """Deletes the photo the admin's earlier edit set (the effective one), and
    not the user's original, when a second edit replaces it."""
    fake = backend([submission(SUB_1, edited={"image_path": NEW_PHOTO})], stored=[PHOTO, NEW_PHOTO])
    client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": PHOTO})
    assert removed_keys(fake) == [NEW_PHOTO]
    assert fake.storage.keys() == [PHOTO]


@pytest.mark.parametrize("other_status", ["pending", "approved"])
def test_submission_edit_keeps_an_old_photo_another_submission_uses(client, backend, other_status):
    """Deletes nothing when the replaced photo is still another pending or
    approved submission's image_path."""
    fake = backend([submission(SUB_1), submission(SUB_2, status=other_status)], stored=[PHOTO, NEW_PHOTO])
    assert client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": NEW_PHOTO}).status_code == 200
    assert fake.storage.removals == []
    assert fake.storage.keys() == [PHOTO, NEW_PHOTO]


def test_submission_edit_keeps_an_old_photo_a_product_uses(client, backend):
    """Deletes nothing when the cleared photo is a product's image_url."""
    fake = backend([submission(SUB_1)], products=[product(image_url=PUBLIC + PHOTO)], stored=[PHOTO])
    client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": None})
    assert fake.storage.keys() == [PHOTO]


@pytest.mark.parametrize("edit", [{"name": "Snow Mushroom Gel Cream"}, {"image_path": PHOTO}],
                         ids=["photo not sent", "same photo sent"])
def test_submission_edit_that_keeps_the_photo_deletes_nothing(client, backend, monkeypatch, edit):
    """Deletes nothing, and makes no attempt (no in-use check runs), when the edit
    leaves image_path out or sends the same path."""
    fake = backend([submission(SUB_1)], stored=[PHOTO])
    checks = []
    monkeypatch.setattr(image_upload, "referenced_upload_paths", lambda: checks.append(1) or set())
    assert client.patch(f"/submissions/admin/{SUB_1}", json=edit).status_code == 200
    assert checks == []
    assert fake.storage.removals == []


def test_submission_edit_still_saves_when_storage_fails(client, backend):
    """Returns HTTP 200 with the new image_path saved when the bucket refuses to
    delete the old photo."""
    fake = backend([submission(SUB_1)], stored=[PHOTO, NEW_PHOTO])
    fake.storage.remove_error = RuntimeError("storage is down")
    resp = client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": NEW_PHOTO})
    assert resp.status_code == 200
    assert row_of(fake, SUB_1)["edited_payload"] == {"image_path": NEW_PHOTO}


def test_submission_edit_refused_as_not_pending_deletes_nothing(client, backend):
    """Returns HTTP 409 SBNPD for a rejected submission and deletes nothing."""
    fake = backend([submission(SUB_1, status="rejected")], stored=[PHOTO, NEW_PHOTO])
    assert client.patch(f"/submissions/admin/{SUB_1}", json={"image_path": NEW_PHOTO}).status_code == 409
    assert fake.storage.removals == []


# --- PATCH /products/{id} ---------------------------------------------------------

def product_patch(client, **fields):
    return client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, **fields})


def test_product_edit_replacing_our_upload_deletes_it(client, backend):
    """Returns HTTP 200 and deletes the product's old photo, an upload under
    products/, once the new one is saved; the new photo stays."""
    fake = backend(products=[product(image_url=PUBLIC + PRODUCT_PHOTO)], stored=[PRODUCT_PHOTO, NEW_PRODUCT_PHOTO])
    resp = product_patch(client, image_path=NEW_PRODUCT_PHOTO)
    assert resp.status_code == 200
    assert fake.store["products"][0]["image_url"] == PUBLIC + NEW_PRODUCT_PHOTO
    assert fake.storage.removals == [(BUCKET, [PRODUCT_PHOTO])]
    assert fake.storage.keys() == [NEW_PRODUCT_PHOTO]


def test_product_edit_clearing_our_upload_deletes_it(client, backend):
    """Returns HTTP 200 and deletes the old photo, an upload under submissions/,
    when image_path is sent as null and no pending or approved submission (only
    a rejected one) names it."""
    fake = backend([submission(SUB_1, status="rejected")], products=[product(image_url=PUBLIC + PHOTO)],
                   stored=[PHOTO])
    assert product_patch(client, image_path=None).status_code == 200
    assert fake.storage.removals == [(BUCKET, [PHOTO])]
    assert fake.storage.keys() == []


def test_product_edit_keeps_a_photo_its_approved_submission_still_names(client, backend):
    """Deletes nothing when the replaced photo is still the image_path of the
    approved submission the product came from."""
    fake = backend([submission(SUB_1, status="approved")], products=[product(image_url=PUBLIC + PHOTO)],
                   stored=[PHOTO, NEW_PRODUCT_PHOTO])
    assert product_patch(client, image_path=NEW_PRODUCT_PHOTO).status_code == 200
    assert fake.storage.removals == []


def test_product_edit_keeps_a_photo_another_product_uses(client, backend):
    """Deletes nothing when another product's image_url shows the replaced photo."""
    fake = backend(products=[product(image_url=PUBLIC + PRODUCT_PHOTO),
                             product(PROD_B_ID, image_url=PUBLIC + PRODUCT_PHOTO)],
                   stored=[PRODUCT_PHOTO, NEW_PRODUCT_PHOTO])
    assert product_patch(client, image_path=NEW_PRODUCT_PHOTO).status_code == 200
    assert set(fake.storage.keys()) == {NEW_PRODUCT_PHOTO, PRODUCT_PHOTO}
    assert fake.storage.removals == []


SEED_AND_EXTERNAL = {
    "seed image in Products/": (PUBLIC + "Products/Biore UV Aqua Rich Watery Essence.png",
                                "Products/Biore UV Aqua Rich Watery Essence.png"),
    "seed image in products/": (PUBLIC + "products/The Ordinary Niacinamide 10 + Zinc 1.png",
                                "products/The Ordinary Niacinamide 10 + Zinc 1.png"),
    "catalogue slug image": (PUBLIC + "cosrx-snail-mucin-essence.jpg", "cosrx-snail-mucin-essence.jpg"),
    "Open Beauty Facts URL": ("https://images.openbeautyfacts.org/images/products/880/front_en.4.400.jpg",
                              "products/880/front_en.4.400.jpg"),
    "upload path in another project": (
        "https://other-project.supabase.co/storage/v1/object/public/product-images/" + PRODUCT_PHOTO, PRODUCT_PHOTO),
    "upload path in another bucket": (
        "https://test-project.supabase.co/storage/v1/object/public/other-bucket/" + PRODUCT_PHOTO, PRODUCT_PHOTO),
    "upload URL with a query string": (PUBLIC + PRODUCT_PHOTO + "?v=2", PRODUCT_PHOTO),
}


@pytest.mark.parametrize("old_url, stored_key", SEED_AND_EXTERNAL.values(), ids=SEED_AND_EXTERNAL.keys())
@pytest.mark.parametrize("new_path", [NEW_PRODUCT_PHOTO, None], ids=["replaced", "cleared"])
def test_product_edit_never_deletes_seed_or_external_images(client, backend, old_url, stored_key, new_path):
    """Returns HTTP 200 and deletes nothing from storage when the image replaced
    or cleared is a seed or catalogue image, an external URL, or anything but
    exactly the public URL of one of our uploads in our bucket."""
    fake = backend(products=[product(image_url=old_url)], stored=[stored_key, NEW_PRODUCT_PHOTO])
    assert product_patch(client, image_path=new_path).status_code == 200
    assert fake.storage.removals == []
    assert stored_key in fake.storage.keys()


def test_product_edit_without_an_image_change_deletes_nothing(client, backend, monkeypatch):
    """Deletes nothing, and makes no attempt (no in-use check runs), when
    image_path is left out, or sent as the product's current photo."""
    fake = backend(products=[product(image_url=PUBLIC + PRODUCT_PHOTO)], stored=[PRODUCT_PHOTO])
    checks = []
    monkeypatch.setattr(image_upload, "referenced_upload_paths", lambda: checks.append(1) or set())
    assert product_patch(client, category="Serums").status_code == 200
    assert product_patch(client, image_path=PRODUCT_PHOTO).status_code == 200
    assert checks == []
    assert fake.storage.removals == []


def test_product_edit_refused_by_the_database_deletes_nothing(client, backend, monkeypatch):
    """Returns HTTP 409 {"detail": "stale"} when admin_update_product refuses the
    edit, and makes no attempt to delete the old photo (no in-use check runs)."""
    fake = backend(products=[product(image_url=PUBLIC + PRODUCT_PHOTO)], stored=[PRODUCT_PHOTO, NEW_PRODUCT_PHOTO])
    checks = []
    monkeypatch.setattr(image_upload, "referenced_upload_paths", lambda: checks.append(1) or set())
    fake.rpc_results["admin_update_product"] = APIError(
        {"message": "stale", "code": "SBSTL", "details": '"2026-10-04T08:20:00.000001+00:00"', "hint": None})
    resp = product_patch(client, image_path=NEW_PRODUCT_PHOTO)
    assert resp.status_code == 409 and resp.json() == {"detail": "stale"}
    assert checks == []
    assert fake.storage.removals == []


def test_product_edit_still_saves_when_storage_fails(client, backend):
    """Returns HTTP 200 with the saved slug and updated_at when the bucket
    refuses to delete the old photo."""
    fake = backend(products=[product(image_url=PUBLIC + PRODUCT_PHOTO)], stored=[PRODUCT_PHOTO, NEW_PRODUCT_PHOTO])
    fake.storage.remove_error = RuntimeError("storage is down")
    resp = product_patch(client, image_path=NEW_PRODUCT_PHOTO)
    assert resp.status_code == 200
    assert resp.json()["updated_at"] == SAVED_AT
    assert fake.storage.removals == [(BUCKET, [PRODUCT_PHOTO])]
