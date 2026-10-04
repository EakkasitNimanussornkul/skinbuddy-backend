"""PATCH /products/{id}, and what product reads gain from migration 0013.

admin_update_product() itself is tested against a local Postgres in
tests/postgres; here the route is tested for what it sends the function and how
it answers each error. The fake ignores select strings and query parameters,
so the pack-order tests check the parameter each read sets; the live and local
runs reported with this change prove what that parameter does.
"""

import copy

import pytest
from postgrest.exceptions import APIError

PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROD_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WATER = "11111111-0000-0000-0000-000000000001"
LOADED_AT = "2026-10-04T08:15:30.123456+00:00"
SAVED_AT = "2026-10-04T09:00:00.654321+00:00"
IMAGE = "products/0f8fad5b-d9cb-469f-a165-70867728950e.webp"
PUBLIC = "https://test-project.supabase.co/storage/v1/object/public/product-images/"

PRODUCT = {
    "id": PROD_ID, "brand": "COSRX", "name": "Snail Mucin Essence", "category": "Treatments",
    "slug": "cosrx-snail-mucin-essence", "description": None, "price_thb": 690, "price_usd": 25,
    "image_url": None, "benefits": ["Hydrating"], "good_for": ["Dry skin"], "pao_months": 12,
    "updated_at": LOADED_AT, "product_sources": [],
    "product_ingredients": [{"ingredients": {"id": WATER, "name": "Water", "good_for": "All Skin Types"}}],
}

MODULES = ("app.api.products", "app.core.services.image_upload", "app.core.services.token")


@pytest.fixture
def backend(patch_backend, as_user):
    as_user("admin-1")

    def _make(products=(PRODUCT,)):
        fake = patch_backend({"users": [{"id": "admin-1", "role": "admin", "skin_type": None}],
                              "products": copy.deepcopy(list(products))}, *MODULES)
        fake.rpc_results["admin_update_product"] = {"product_id": PROD_ID, "updated_at": SAVED_AT,
                                                    "slug": "cosrx-snail-mucin-essence"}
        return fake
    return _make


def test_product_patch_passes_the_fields_sent_to_the_function(client, backend):
    """Calls admin_update_product once with the product id, updated_at exactly as sent
    (microseconds kept), and a patch of only the fields sent: image_path replaced by its
    public URL under the key image_url, each ingredient reduced to the one key given,
    sources without the optional keys left out."""
    fake = backend()
    resp = client.patch(f"/products/{PROD_ID}", json={
        "updated_at": LOADED_AT, "name": "Advanced Snail Mucin Essence", "price_usd": None,
        "image_path": IMAGE, "benefits": [], "good_for": ["Dry skin", "Redness"],
        "ingredients": [{"ingredient_id": WATER}, {"new_name": "Snail Secretion Filtrate"}],
        "sources": [{"url": "https://brand.example/p", "title": "Brand page", "claims": ["listing"]}],
    })
    assert resp.status_code == 200
    assert fake.rpc_calls == [("admin_update_product", {
        "p_product_id": PROD_ID,
        "p_expected_updated_at": LOADED_AT,
        "p_patch": {
            "name": "Advanced Snail Mucin Essence", "price_usd": None, "image_url": PUBLIC + IMAGE,
            "benefits": [], "good_for": ["Dry skin", "Redness"],
            "ingredients": [{"ingredient_id": WATER}, {"new_name": "Snail Secretion Filtrate"}],
            "sources": [{"url": "https://brand.example/p", "title": "Brand page", "claims": ["listing"]}],
        },
    })]


def test_product_patch_with_null_image_path_clears_the_image(client, backend):
    """Sends image_url null when image_path is sent as null, and sends no image key at all
    when image_path is left out."""
    fake = backend()
    client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "image_path": None})
    client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "category": "Serums"})
    assert fake.rpc_calls[0][1]["p_patch"] == {"image_url": None}
    assert fake.rpc_calls[1][1]["p_patch"] == {"category": "Serums"}


def test_product_patch_returns_the_product_with_its_new_slug_and_updated_at(client, backend):
    """Returns HTTP 200 with the GET /products/{id} shape (ingredients, safety_flags,
    benefits, good_for, pao_months) plus the slug and updated_at the function returned."""
    fake = backend()
    fake.rpc_results["admin_update_product"] = {"product_id": PROD_ID, "updated_at": SAVED_AT,
                                                "slug": "cosrx-advanced-snail-mucin-essence"}
    data = client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "name": "Advanced"}).json()
    assert data["slug"] == "cosrx-advanced-snail-mucin-essence"
    assert data["updated_at"] == SAVED_AT
    assert data["id"] == PROD_ID and "safety_flags" in data
    assert data["product_ingredients"][0]["ingredients"]["name"] == "Water"
    assert (data["benefits"], data["good_for"], data["pao_months"]) == (["Hydrating"], ["Dry skin"], 12)


def rpc_error(code, message="message from the database", details=None):
    return APIError({"code": code, "message": message, "details": details, "hint": None})


PATCH_ERRORS = [
    ("SBSTL", "stale", '"2026-10-04T08:20:00.000001+00:00"', 409, {"detail": "stale"}),
    ("SBDUP", "duplicate", '[{"id": "%s", "slug": "s", "brand": "B", "name": "N"}]' % PROD_B_ID,
     409, {"detail": "duplicate", "candidates": [{"id": PROD_B_ID, "slug": "s", "brand": "B", "name": "N"}]}),
    ("23505", "duplicate key value violates unique constraint", None, 409, {"detail": "duplicate", "candidates": []}),
    ("SBNON", "a product needs at least one ingredient", None,
     422, {"detail": "a product needs at least one ingredient", "code": "SBNON"}),
    ("SBUNK", "unknown ingredient_id", '["%s"]' % WATER,
     422, {"detail": "unknown ingredient_id", "code": "SBUNK", "details": [WATER]}),
    ("SBAMB", "\"Aqua\" matches more than one ingredient", '[{"id": "a", "name": "Water"}]',
     422, {"detail": "\"Aqua\" matches more than one ingredient", "code": "SBAMB", "details": [{"id": "a", "name": "Water"}]}),
    ("SBVAL", "brand cannot be blank", None, 422, {"detail": "brand cannot be blank", "code": "SBVAL"}),
    ("23514", "new row violates check constraint", None, 422, {"detail": "new row violates check constraint", "code": "23514"}),
    ("SBNFD", "product not found", None, 404, {"detail": "product not found", "code": "SBNFD"}),
    ("42883", "function does not exist", None, 500, {"detail": "The database refused the change."}),
]


@pytest.mark.parametrize("code, message, details, status, expected", PATCH_ERRORS, ids=[e[0] for e in PATCH_ERRORS])
def test_product_patch_maps_each_database_error_to_its_http_answer(client, backend, code, message, details,
                                                                   status, expected):
    """Returns 409 {"detail": "stale"} for SBSTL, 409 {"detail": "duplicate", "candidates": [...]}
    when the new brand+name belongs to another product (and for a 23505 race), 422 with
    the message for invalid input, 404 for an unknown product, and 500 for anything else."""
    fake = backend()
    fake.rpc_results["admin_update_product"] = rpc_error(code, message, details)
    resp = client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "name": "X"})
    assert resp.status_code == status
    assert resp.json() == expected


INVALID_PATCHES = {
    "no updated_at": {"name": "X"},
    "empty updated_at": {"updated_at": ""},
    "null name": {"updated_at": LOADED_AT, "name": None},
    "blank brand": {"updated_at": LOADED_AT, "brand": " "},
    "category not in the list": {"updated_at": LOADED_AT, "category": "Perfume"},
    "tag not in the list": {"updated_at": LOADED_AT, "good_for": ["Wrinkles"]},
    "9 benefits": {"updated_at": LOADED_AT, "benefits": ["b"] * 9},
    "pao 7": {"updated_at": LOADED_AT, "pao_months": 7},
    "negative price": {"updated_at": LOADED_AT, "price_thb": -5},
    "no ingredients": {"updated_at": LOADED_AT, "ingredients": []},
    "null ingredients": {"updated_at": LOADED_AT, "ingredients": None},
    "both id and name": {"updated_at": LOADED_AT, "ingredients": [{"ingredient_id": WATER, "new_name": "W"}]},
    "details on a product ingredient": {"updated_at": LOADED_AT, "ingredients": [{"new_name": "W", "details": {}}]},
    "claim not in the list": {"updated_at": LOADED_AT, "sources": [{"url": "https://a.example", "title": "A", "claims": ["benefits"]}]},
    "source_type not in the list": {"updated_at": LOADED_AT, "sources": [{"url": "https://a.example", "title": "A",
                                                                          "source_type": "blog", "claims": ["listing"]}]},
    "source url not http": {"updated_at": LOADED_AT, "sources": [{"url": "file:///etc/x", "title": "A", "claims": ["listing"]}]},
    "image_path not an upload": {"updated_at": LOADED_AT, "image_path": "https://elsewhere.example/a.png"},
    "unknown field": {"updated_at": LOADED_AT, "slug": "my-own-slug"},
}


@pytest.mark.parametrize("patch", list(INVALID_PATCHES.values()), ids=list(INVALID_PATCHES))
def test_product_patch_refuses_an_invalid_body_with_422(client, backend, patch):
    """Returns HTTP 422 for each invalid body listed and never calls admin_update_product."""
    fake = backend()
    assert client.patch(f"/products/{PROD_ID}", json=patch).status_code == 422
    assert fake.rpc_calls == []


def test_product_patch_accepts_a_submission_photo_as_the_image(client, backend):
    """Accepts an image_path under submissions/ as well as products/, so an admin can
    reuse the photo a user uploaded."""
    fake = backend()
    path = "submissions/0f8fad5b-d9cb-469f-a165-70867728950e.jpg"
    assert client.patch(f"/products/{PROD_ID}", json={"updated_at": LOADED_AT, "image_path": path}).status_code == 200
    assert fake.rpc_calls[0][1]["p_patch"] == {"image_url": PUBLIC + path}


def test_product_patch_answers_404_for_a_malformed_id(client, backend):
    """Returns HTTP 404 for a product id that is not a UUID, without calling the function."""
    fake = backend()
    assert client.patch("/products/not-a-uuid", json={"updated_at": LOADED_AT}).status_code == 404
    assert fake.rpc_calls == []


# --- Product reads -------------------------------------------------------------

PACK_ORDER = ("position", {"foreign_table": "product_ingredients", "nullsfirst": False})


@pytest.mark.parametrize("path", [f"/products/{PROD_ID}", "/products/slug/cosrx-snail-mucin-essence",
                                  "/products/search", f"/products/compare?product_a_id={PROD_ID}&product_b_id={PROD_ID}"],
                         ids=["detail", "slug", "search", "compare"])
def test_product_reads_ask_for_ingredients_in_pack_order(client, patch_backend, path):
    """Every product read asks PostgREST to order product_ingredients by position, NULLs
    last (product_ingredients.order=position.asc.nullslast)."""
    fake = patch_backend({"products": [copy.deepcopy(PRODUCT)], "users": [], "conflict_rules": [],
                          "category_conflict_rules": []},
                         "app.api.products", "app.core.services.compatibility_service")
    assert client.get(path).status_code == 200
    product_orders = [(column, kwargs) for table, column, kwargs in fake.orders if table == "products"]
    assert PACK_ORDER in product_orders


def test_shelf_asks_for_each_products_ingredients_in_pack_order(client, patch_backend, as_user):
    """GET /shelf/ asks PostgREST to order products.product_ingredients by position, NULLs last."""
    fake = patch_backend({"shelf_items": []}, "app.api.shelf")
    as_user("user-1")
    assert client.get("/shelf/").status_code == 200
    assert ("shelf_items", "position", {"foreign_table": "products.product_ingredients", "nullsfirst": False}) in fake.orders


def test_product_reads_return_benefits_good_for_pao_and_updated_at(client, patch_backend):
    """GET /products/{id}, /slug/{slug} and /search return benefits, good_for, pao_months
    and updated_at as stored."""
    patch_backend({"products": [copy.deepcopy(PRODUCT)], "users": []}, "app.api.products")
    wanted = {"benefits": ["Hydrating"], "good_for": ["Dry skin"], "pao_months": 12, "updated_at": LOADED_AT}
    for data in (client.get(f"/products/{PROD_ID}").json(),
                 client.get("/products/slug/cosrx-snail-mucin-essence").json(),
                 client.get("/products/search").json()[0]):
        assert {k: data[k] for k in wanted} == wanted


def test_compare_keeps_the_new_product_fields(client, patch_backend):
    """GET /products/compare returns benefits, good_for, pao_months and updated_at for both
    products: its response model declares them, so they are not dropped."""
    other = {**copy.deepcopy(PRODUCT), "id": PROD_B_ID, "name": "Other", "benefits": None,
             "good_for": None, "pao_months": None}
    patch_backend({"products": [copy.deepcopy(PRODUCT), other], "users": [], "conflict_rules": [],
                   "category_conflict_rules": []},
                  "app.api.products", "app.core.services.compatibility_service")
    data = client.get("/products/compare", params={"product_a_id": PROD_ID, "product_b_id": PROD_B_ID}).json()
    assert (data["product_a"]["benefits"], data["product_a"]["good_for"],
            data["product_a"]["pao_months"], data["product_a"]["updated_at"]) == (["Hydrating"], ["Dry skin"], 12, LOADED_AT)
    assert (data["product_b"]["benefits"], data["product_b"]["good_for"], data["product_b"]["pao_months"]) == (None, None, None)
