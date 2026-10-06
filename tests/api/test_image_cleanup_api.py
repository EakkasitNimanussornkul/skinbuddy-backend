"""POST /submissions/admin/cleanup-images: deleting stored photos nothing uses.

The bucket is the fake in tests/conftest.py, listed with list_v2 a page at a
time. An upload is "unreferenced" when no product's image_url, no pending
submission's effective image_path, and no approved submission's whose product
still shows it points at it; only those last written at least older_than_hours
ago are reported, and deleted unless dry_run.
"""

import copy

import pytest

from app.core.services import image_upload

BUCKET = "product-images"
PUBLIC = "https://test-project.supabase.co/storage/v1/object/public/product-images/"
URL = "/submissions/admin/cleanup-images"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
APPROVED_PROD_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def upload(n, folder="submissions", ext="png"):
    return f"{folder}/00000000-0000-4000-8000-{n:012d}.{ext}"


ORPHAN_OLD = upload(1)                       # never used, 48 h old: deleted
ORPHAN_NEW = upload(2)                       # never used, 1 h old: a form still being filled in
PENDING_PHOTO = upload(3)                    # a pending submission's payload
APPROVED_PHOTO = upload(4)                   # an approved submission's edits; its product shows it
REJECTED_PHOTO = upload(5)                   # only a rejected submission: deleted
PRODUCT_PHOTO = upload(6, "products", "webp")   # a product's image_url
PRODUCT_ORPHAN = upload(7, "products", "jpg")   # an admin upload never saved: deleted
SEED_LOWER = "products/The Ordinary Niacinamide 10 + Zinc 1.png"   # seed image, not an upload
SEED_UPPER = "Products/" + upload(8).split("/")[1]                # another folder (case differs)
ELSEWHERE = upload(9).split("/")[1]                                # bucket root


def submission(sid, status, payload_image=None, edited=None, product_id=None):
    return {"id": sid, "status": status, "payload": {"name": "N", "image_path": payload_image},
            "edited_payload": edited, "product_id": product_id}


SUBMISSIONS = [
    submission("5ab00000-0000-0000-0000-000000000001", "pending", PENDING_PHOTO),
    submission("5ab00000-0000-0000-0000-000000000002", "approved", None, {"image_path": APPROVED_PHOTO},
               APPROVED_PROD_ID),
    submission("5ab00000-0000-0000-0000-000000000003", "rejected", REJECTED_PHOTO),
    # The user's original photo, replaced by the admin's edit: no longer used.
    submission("5ab00000-0000-0000-0000-000000000004", "pending", ORPHAN_OLD, {"image_path": PENDING_PHOTO}),
]
PRODUCTS = [{"id": PROD_ID, "image_url": PUBLIC + PRODUCT_PHOTO},
            {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "image_url": PUBLIC + "Products/Seed.png"},
            {"id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "image_url": None},
            {"id": APPROVED_PROD_ID, "image_url": PUBLIC + APPROVED_PHOTO}]
AGES = {ORPHAN_OLD: 48, ORPHAN_NEW: 1, PENDING_PHOTO: 48, APPROVED_PHOTO: 48, REJECTED_PHOTO: 48,
        PRODUCT_PHOTO: 48, PRODUCT_ORPHAN: 30, SEED_LOWER: 2000, SEED_UPPER: 2000, ELSEWHERE: 2000}
MODULES = ("app.api.submissions", "app.core.services.submission_service",
           "app.core.services.ingredient_lookup", "app.core.services.image_upload",
           "app.core.services.token")


@pytest.fixture
def backend(patch_backend, as_user):
    """A fake database and bucket, with the caller logged in as an admin."""
    def _make(ages=AGES, submissions=SUBMISSIONS, products=PRODUCTS, user="admin-1"):
        as_user(user)
        fake = patch_backend({"users": [{"id": "admin-1", "role": "admin"}, {"id": "user-1", "role": "user"}],
                              "products": copy.deepcopy(products),
                              "product_submissions": copy.deepcopy(submissions)}, *MODULES)
        for key, hours in ages.items():
            fake.storage.put(key, hours_old=hours)
        return fake
    return _make


def test_cleanup_dry_run_reports_and_deletes_nothing(client, backend):
    """With no body (dry_run defaults to true, older_than_hours to 24), returns
    HTTP 200 {"checked": 8, "unreferenced": 3, "deleted": 0, "failed": 0,
    "dry_run": true, "paths": [the three unused uploads over 24 h old, sorted]}
    and deletes nothing."""
    fake = backend()
    resp = client.post(URL)
    assert resp.status_code == 200
    assert resp.json() == {"checked": 8, "unreferenced": 3, "deleted": 0, "failed": 0, "dry_run": True,
                           "paths": [PRODUCT_ORPHAN, ORPHAN_OLD, REJECTED_PHOTO]}
    assert fake.storage.removals == []
    assert len(fake.storage.keys()) == len(AGES)


def test_cleanup_deletes_only_old_unreferenced_uploads(client, backend):
    """With {"dry_run": false}, deletes exactly the uploads over 24 h old that no
    product, no pending submission, and no approved submission whose product
    still shows it uses (a never-used upload, one only a rejected submission
    names, a user's photo the admin replaced, and an admin upload never saved
    to a product), and answers "deleted": 3.
    Newer uploads, photos in use, seed images and anything outside
    submissions/ and products/ (exact case) all stay."""
    fake = backend()
    resp = client.post(URL, json={"dry_run": False})
    assert resp.status_code == 200
    assert resp.json() == {"checked": 8, "unreferenced": 3, "deleted": 3, "failed": 0, "dry_run": False,
                           "paths": [PRODUCT_ORPHAN, ORPHAN_OLD, REJECTED_PHOTO]}
    assert fake.storage.removals == [(BUCKET, [PRODUCT_ORPHAN, ORPHAN_OLD, REJECTED_PHOTO])]
    assert set(fake.storage.keys()) == {ORPHAN_NEW, PENDING_PHOTO, APPROVED_PHOTO, PRODUCT_PHOTO,
                                        SEED_LOWER, SEED_UPPER, ELSEWHERE}


def test_cleanup_age_threshold_is_the_one_sent(client, backend):
    """With {"older_than_hours": 40, "dry_run": false}, deletes only the unused
    uploads at least 40 h old: the 30-hour-old one stays."""
    fake = backend()
    resp = client.post(URL, json={"older_than_hours": 40, "dry_run": False})
    assert resp.json()["paths"] == [ORPHAN_OLD, REJECTED_PHOTO]
    assert PRODUCT_ORPHAN in fake.storage.keys()
    assert ORPHAN_OLD not in fake.storage.keys()


def test_cleanup_with_one_hour_threshold_includes_newer_uploads(client, backend):
    """With {"older_than_hours": 1}, a never-used upload exactly 1 h old is
    reported too."""
    backend()
    assert ORPHAN_NEW in client.post(URL, json={"older_than_hours": 1}).json()["paths"]


def test_cleanup_rechecks_use_right_before_deleting(client, backend, monkeypatch):
    """Skips an upload that came into use between the listing and its deletion:
    answers "unreferenced": 1, "deleted": 0, "failed": 0, and the file stays."""
    fake = backend(ages={ORPHAN_OLD: 48}, submissions=[], products=[])
    answers = iter([set(), {ORPHAN_OLD}])
    monkeypatch.setattr(image_upload, "referenced_upload_paths", lambda: next(answers))
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["unreferenced"], body["deleted"], body["failed"]) == (1, 0, 0)
    assert fake.storage.removals == []
    assert fake.storage.keys() == [ORPHAN_OLD]


def test_cleanup_counts_as_deleted_only_what_storage_confirms(client, backend, monkeypatch):
    """Answers "deleted": 0 and "failed": 1 when storage deletes nothing for a key
    (here it vanished between the listing and the batch): deleted is read off
    storage's answer, not assumed."""
    fake = backend(ages={ORPHAN_OLD: 48}, submissions=[], products=[])
    calls = []

    def referenced():
        calls.append(1)
        if len(calls) == 2:                       # the re-check right before deleting
            fake.storage.objects[BUCKET].pop(ORPHAN_OLD)
        return set()
    monkeypatch.setattr(image_upload, "referenced_upload_paths", referenced)
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["unreferenced"], body["deleted"], body["failed"]) == (1, 0, 1)
    assert fake.storage.removals == [(BUCKET, [ORPHAN_OLD])]


def test_cleanup_never_deletes_an_object_of_unknown_age(client, backend, monkeypatch):
    """Treats an object whose created and updated times are unknown as new: it is
    counted in checked but never reported or deleted."""
    fake = backend(ages={ORPHAN_OLD: 48}, submissions=[], products=[])
    monkeypatch.setattr(image_upload, "list_uploads", lambda: [(ORPHAN_OLD, None)])
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["checked"], body["unreferenced"], body["deleted"]) == (1, 0, 0)
    assert fake.storage.keys() == [ORPHAN_OLD]


def test_cleanup_counts_a_failed_batch_and_still_answers(client, backend):
    """Returns HTTP 200 with "deleted": 0 and "failed": 3 when the bucket refuses
    to delete; the files stay."""
    fake = backend()
    fake.storage.remove_error = RuntimeError("storage is down")
    resp = client.post(URL, json={"dry_run": False})
    assert resp.status_code == 200
    assert (resp.json()["deleted"], resp.json()["failed"]) == (0, 3)
    assert len(fake.storage.keys()) == len(AGES)


def test_cleanup_answers_500_when_the_bucket_cannot_be_listed(client, backend):
    """Returns HTTP 500 {"detail": "Failed to check the stored images."} when the
    listing fails, and deletes nothing."""
    fake = backend()
    fake.storage.list_error = RuntimeError("storage is down")
    resp = client.post(URL, json={"dry_run": False})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Failed to check the stored images."}
    assert fake.storage.removals == []


def test_cleanup_pages_through_many_objects(client, backend):
    """Lists 251 uploads under submissions/ 100 at a time (3 pages, each after the
    last page's cursor), deletes the 250 unused ones in batches of 100, 100 and
    50, keeps the one a pending submission uses, and names only the first 100
    paths in the answer."""
    used = upload(500)
    ages = {upload(n): 48 for n in range(250)}
    ages[used] = 48
    fake = backend(ages=ages, products=[],
                   submissions=[submission("5ab00000-0000-0000-0000-000000000001", "pending", used)])
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["checked"], body["unreferenced"], body["deleted"], body["failed"]) == (251, 250, 250, 0)
    assert body["paths"] == [upload(n) for n in range(100)]
    pages = [opts for bucket, opts in fake.storage.list_calls if opts["prefix"] == "submissions/"]
    assert [opts.get("cursor") for opts in pages] == [None, upload(99), upload(199)]
    assert all(opts["limit"] == 100 for opts in pages)
    assert [len(keys) for _bucket, keys in fake.storage.removals] == [100, 100, 50]
    assert fake.storage.keys() == [used]


@pytest.mark.parametrize("bad", [
    {"older_than_hours": 0}, {"older_than_hours": -5}, {"older_than_hours": 87601},
    {"older_than_hours": "24"}, {"older_than_hours": 1.5}, {"dry_run": "false"}, {"dry_run": 0},
    {"dry_run": None}, {"surprise": True},
], ids=["zero hours", "negative hours", "over ten years", "hours as text", "fractional hours",
        "dry_run as text", "dry_run as 0", "dry_run null", "unknown key"])
def test_cleanup_refuses_an_invalid_body(client, backend, bad):
    """Returns HTTP 422 and deletes nothing for older_than_hours below 1, over
    87600 (ten years) or not a whole number, a dry_run that is not true or
    false, or an unknown key."""
    fake = backend()
    resp = client.post(URL, json={"dry_run": False, **bad})     # a real run, unless dry_run is the bad key
    assert resp.status_code == 422
    assert fake.storage.removals == []


def test_cleanup_is_refused_without_a_login(client, backend):
    """Returns HTTP 401 with no Authorization header, and deletes nothing."""
    from app.main import app
    from app.core.services.token import get_current_user_id
    fake = backend()
    app.dependency_overrides.pop(get_current_user_id, None)
    resp = client.post(URL, json={"dry_run": False})
    assert resp.status_code == 401
    assert fake.storage.removals == [] and fake.storage.list_calls == []


def test_cleanup_is_refused_to_a_normal_user(client, backend):
    """Returns HTTP 403 {"detail": "Admin access required"} to a logged-in user
    whose role is 'user', and neither lists nor deletes anything."""
    fake = backend(user="user-1")
    resp = client.post(URL, json={"dry_run": False})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required"}
    assert fake.storage.removals == [] and fake.storage.list_calls == []


# --- An approved submission keeps its photo only while its product shows it ------

REPLACED_PHOTO = upload(10)          # an approved submission's photo its product no longer shows
SUB_A = "5ab00000-0000-0000-0000-00000000000a"
SUB_B = "5ab00000-0000-0000-0000-00000000000b"
MISSING_PROD_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"     # in no products row


def test_cleanup_deletes_an_approved_photo_its_product_no_longer_shows(client, backend):
    """With {"dry_run": false}, deletes an approved submission's photo once the
    product it created shows a different one (replaced before or after this
    rule), and keeps an approved submission's photo its product still shows:
    answers {"checked": 3, "unreferenced": 1, "deleted": 1, "failed": 0,
    "dry_run": false, "paths": [the replaced photo]}."""
    fake = backend(ages={REPLACED_PHOTO: 48, APPROVED_PHOTO: 48, PRODUCT_PHOTO: 48},
                   submissions=[submission(SUB_A, "approved", REPLACED_PHOTO, None, PROD_ID),
                                submission(SUB_B, "approved", APPROVED_PHOTO, None, APPROVED_PROD_ID)],
                   products=[{"id": PROD_ID, "image_url": PUBLIC + PRODUCT_PHOTO},
                             {"id": APPROVED_PROD_ID, "image_url": PUBLIC + APPROVED_PHOTO}])
    resp = client.post(URL, json={"dry_run": False})
    assert resp.status_code == 200
    assert resp.json() == {"checked": 3, "unreferenced": 1, "deleted": 1, "failed": 0, "dry_run": False,
                           "paths": [REPLACED_PHOTO]}
    assert fake.storage.removals == [(BUCKET, [REPLACED_PHOTO])]
    assert set(fake.storage.keys()) == {APPROVED_PHOTO, PRODUCT_PHOTO}


def test_cleanup_dry_run_reports_an_approved_photo_its_product_cleared(client, backend):
    """With no body (a dry run), reports an approved submission's photo whose
    product's image_url is now null in "paths", and deletes nothing."""
    fake = backend(ages={REPLACED_PHOTO: 48},
                   submissions=[submission(SUB_A, "approved", None, {"image_path": REPLACED_PHOTO}, PROD_ID)],
                   products=[{"id": PROD_ID, "image_url": None}])
    body = client.post(URL).json()
    assert (body["unreferenced"], body["paths"], body["dry_run"]) == (1, [REPLACED_PHOTO], True)
    assert fake.storage.removals == []


def test_cleanup_deletes_an_approved_photo_whose_product_is_gone(client, backend):
    """With {"dry_run": false}, deletes an approved submission's photo when its
    product_id names no existing product: a missing product does not show the
    photo."""
    fake = backend(ages={REPLACED_PHOTO: 48}, products=[],
                   submissions=[submission(SUB_A, "approved", REPLACED_PHOTO, None, MISSING_PROD_ID)])
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["unreferenced"], body["deleted"]) == (1, 1)
    assert fake.storage.keys() == []


def test_cleanup_keeps_an_approved_photo_a_pending_submission_also_names(client, backend):
    """With {"dry_run": false}, keeps an approved submission's photo its product
    no longer shows while a pending submission's image_path names the same
    photo: "unreferenced": 0, "deleted": 0."""
    fake = backend(ages={REPLACED_PHOTO: 48}, products=[{"id": PROD_ID, "image_url": PUBLIC + PRODUCT_PHOTO}],
                   submissions=[submission(SUB_A, "approved", REPLACED_PHOTO, None, PROD_ID),
                                submission(SUB_B, "pending", REPLACED_PHOTO)])
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["unreferenced"], body["deleted"]) == (0, 0)
    assert fake.storage.keys() == [REPLACED_PHOTO]


def test_cleanup_keeps_the_photo_of_an_approved_submission_with_no_product_id(client, backend):
    """With {"dry_run": false}, keeps an approved submission's photo when its
    product_id is null (there is no product to check): "unreferenced": 0,
    "deleted": 0."""
    fake = backend(ages={REPLACED_PHOTO: 48}, products=[],
                   submissions=[submission(SUB_A, "approved", REPLACED_PHOTO, None, None)])
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["unreferenced"], body["deleted"]) == (0, 0)
    assert fake.storage.keys() == [REPLACED_PHOTO]


def test_cleanup_keeps_a_pending_submissions_photo_even_with_a_product_id(client, backend):
    """With {"dry_run": false}, keeps a pending submission's photo even when the
    row carries a product_id whose product shows another photo: only an
    approved submission's photo depends on its product. "unreferenced": 0,
    "deleted": 0."""
    fake = backend(ages={REPLACED_PHOTO: 48}, products=[{"id": PROD_ID, "image_url": PUBLIC + PRODUCT_PHOTO}],
                   submissions=[submission(SUB_A, "pending", REPLACED_PHOTO, None, PROD_ID)])
    body = client.post(URL, json={"dry_run": False}).json()
    assert (body["unreferenced"], body["deleted"]) == (0, 0)
    assert fake.storage.keys() == [REPLACED_PHOTO]
