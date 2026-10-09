"""The 60-second cache (app/core/cache.py) and what is cached with it.

Cached: the product tree behind GET /products/search (one entry per select, the
default and the card one), conflict_rules and category_conflict_rules, and the
ingredient list behind /ingredients/* and /meta/functional-groups. Never cached:
anything about a user. Writes through this app (approving a submission, PATCH
/products/{id}) clear it.

The fake Supabase client ignores select strings, so what a real select returns
is proven by the read-only live run reported with this change, not here. These
tests count reads of each table, which the fake can show.
"""

import collections
import copy

import pytest
from postgrest.exceptions import APIError

from app.core import cache
from app.core.services.token import get_optional_user_id

SUB_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
PROD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UPDATED_AT = "2026-10-04T08:15:30+00:00"


def _ingredient(name, **extra):
    return {"id": f"ing-{name.lower()}", "name": name, "functional_group": "Humectant", **extra}


def _product(pid, name, ingredients, **extra):
    return {
        "id": pid, "brand": "Brand", "name": name, "category": "Serum", "slug": None,
        "description": "d", "price_thb": 500, "price_usd": 15, "image_url": None,
        "product_sources": [],
        "product_ingredients": [{"ingredients": i} for i in ingredients],
        **extra,
    }


# A product that is helpful to dry skin and one whose ingredient is flagged for
# sensitive skin, so two different skin types score it differently.
CATALOGUE = [
    _product("p-1", "Dry Serum", [
        _ingredient("Glycerin", good_for="Dry Skin"),
        _ingredient("Alcohol Denat.", bad_for="Sensitive Skin (S)",
                    ingredient_concerns=[{"target_profile": "Sensitive Skin (S)", "severity": "High",
                                          "concern_title": "Strips oil", "concern_sources": []}]),
    ]),
    _product("p-2", "Plain Cream", [_ingredient("Water")], price_thb=900),
]


def counting(fake):
    """Count the table() calls the code makes on this fake, by table name."""
    reads = collections.Counter()
    original = fake.table

    def table(name):
        reads[name] += 1
        return original(name)

    fake.table = table
    return reads


def edit_row(fake, table, index, **changes):
    """Change a stored row the way a database write would: the rows already handed out
    (the fake returns the stored dicts themselves) are left as they were."""
    fake.store[table] = [{**row, **changes} if i == index else row for i, row in enumerate(fake.store[table])]


def as_user(user_id):
    from app.main import app
    app.dependency_overrides[get_optional_user_id] = lambda: user_id


# --- the cache module --------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache, "_clock", lambda: now[0])
    return now


def test_a_cached_value_is_served_without_calling_the_loader_again():
    """Returns the loader's value on the first call and the same object on a second
    call within 60 seconds, with the loader called once."""
    calls = []
    first = cache.get_or_load("k", lambda: calls.append(1) or ["row"])
    second = cache.get_or_load("k", lambda: calls.append(1) or ["other"])

    assert second is first and first == ["row"]
    assert len(calls) == 1


def test_a_cached_value_expires_after_sixty_seconds(clock):
    """Returns the cached value at 59.9 seconds and loads a fresh one at 60 seconds:
    the loader runs twice in total, and the second call returns its new value."""
    cache.get_or_load("k", lambda: "old")
    clock[0] += 59.9
    assert cache.get_or_load("k", lambda: "new") == "old"
    clock[0] += 0.1
    assert cache.get_or_load("k", lambda: "new") == "new"
    clock[0] += 59.9
    assert cache.get_or_load("k", lambda: "newer") == "new"


def test_clear_makes_the_next_call_load_again():
    """Returns the loader's new value on the call after clear(), although the
    previous value was under 60 seconds old."""
    cache.get_or_load("k", lambda: "old")
    cache.clear()
    assert cache.get_or_load("k", lambda: "new") == "new"


def test_entries_with_different_keys_do_not_share_a_value():
    """Returns each key's own loader value, and clear() drops both."""
    assert cache.get_or_load("a", lambda: 1) == 1
    assert cache.get_or_load("b", lambda: 2) == 2
    assert (cache.get_or_load("a", lambda: 9), cache.get_or_load("b", lambda: 9)) == (1, 2)


def test_a_loader_that_raises_leaves_nothing_cached():
    """Raises the loader's error to the caller, and the next call loads again and
    returns its value instead of a stored failure."""
    def broken():
        raise RuntimeError("database down")

    with pytest.raises(RuntimeError):
        cache.get_or_load("k", broken)
    assert cache.get_or_load("k", lambda: "recovered") == "recovered"


def test_a_value_loaded_while_clear_ran_is_returned_but_not_kept():
    """Returns the loader's value to the caller whose load overlapped a clear(), and
    loads again on the next call, so a read that began before a write cannot store
    its stale rows after the write cleared the cache."""
    def overlapped():
        cache.clear()
        return "read before the write finished"

    assert cache.get_or_load("k", overlapped) == "read before the write finished"
    assert cache.get_or_load("k", lambda: "after") == "after"


# --- the product tree behind /products/search --------------------------------------

def test_a_second_search_does_not_read_the_products_table(client, patch_backend):
    """Reads the products table once for two identical GET /products/search calls,
    and returns the same body both times."""
    fake = patch_backend({"products": copy.deepcopy(CATALOGUE)}, "app.api.products")
    reads = counting(fake)

    first = client.get("/products/search")
    second = client.get("/products/search")

    assert reads["products"] == 1
    assert first.json() == second.json() and len(first.json()) == 2


def test_different_filters_share_one_read_of_the_tree(client, patch_backend):
    """Reads the products table once for a plain search, a text query and a price
    band, and each answers with its own rows."""
    fake = patch_backend({"products": copy.deepcopy(CATALOGUE)}, "app.api.products")
    reads = counting(fake)

    everything = client.get("/products/search").json()
    by_text = client.get("/products/search", params={"q": "cream"}).json()
    by_price = client.get("/products/search", params={"max_price": 600}).json()

    assert reads["products"] == 1
    assert [p["id"] for p in everything] == ["p-1", "p-2"]
    assert [p["id"] for p in by_text] == ["p-2"]
    assert [p["id"] for p in by_price] == ["p-1"]


def test_the_default_and_card_views_cache_separately(client, patch_backend):
    """Reads the products table once for the default view and once for the card view,
    and not again for either on a repeat, so the card view is never served the
    default view's tree or the other way round."""
    fake = patch_backend({"products": copy.deepcopy(CATALOGUE)}, "app.api.products")
    reads = counting(fake)

    for _ in range(2):
        default = client.get("/products/search").json()
        card = client.get("/products/search", params={"view": "card"}).json()

    assert reads["products"] == 2
    assert "product_ingredients" in default[0] and "product_ingredients" not in card[0]


def test_the_cached_tree_is_read_again_after_sixty_seconds(client, patch_backend, clock):
    """Reads the products table again for a search made 60 seconds after the first,
    and returns a product edited in between."""
    fake = patch_backend({"products": copy.deepcopy(CATALOGUE)}, "app.api.products")
    reads = counting(fake)

    client.get("/products/search")
    edit_row(fake, "products", 1, name="Renamed Cream")
    clock[0] += 59.9
    assert client.get("/products/search").json()[1]["name"] == "Plain Cream"
    clock[0] += 0.1
    assert client.get("/products/search").json()[1]["name"] == "Renamed Cream"
    assert reads["products"] == 2


def test_each_user_is_scored_from_their_own_skin_type_on_the_shared_tree(client, patch_backend):
    """Returns for a dry-skin user and then a sensitive-skin user, on one cached
    tree, each user's own skin_match_score and the guest's null; a score computed
    for the first user is not returned to the second or to a guest."""
    fake = patch_backend({
        "products": copy.deepcopy(CATALOGUE),
        "users": [{"id": "u-dry", "skin_type": "DRNT"}, {"id": "u-sens", "skin_type": "DSPT"}],
    }, "app.api.products")
    reads = counting(fake)

    as_user("u-dry")
    dry = client.get("/products/search").json()[0]
    as_user("u-sens")
    sens = client.get("/products/search").json()[0]
    as_user(None)
    guest = client.get("/products/search").json()[0]
    as_user("u-dry")
    dry_again = client.get("/products/search").json()[0]

    assert reads["products"] == 1
    assert dry["skin_match_score"] is not None and sens["skin_match_score"] is not None
    assert dry["skin_match_score"] != sens["skin_match_score"]
    assert guest["skin_match_score"] is None and guest["match_breakdown"] is None
    assert dry_again == dry


def test_the_users_row_is_read_on_every_search_never_cached(client, patch_backend):
    """Reads the users table once per signed-in search, so a skin type changed
    between two searches is used by the second, and a guest search reads none."""
    fake = patch_backend({"products": copy.deepcopy(CATALOGUE),
                          "users": [{"id": "u-1", "skin_type": "DRNT"}]}, "app.api.products")
    reads = counting(fake)

    as_user("u-1")
    before = client.get("/products/search").json()[0]["skin_match_score"]
    edit_row(fake, "users", 0, skin_type="DSPT")
    after = client.get("/products/search").json()[0]["skin_match_score"]
    as_user(None)
    client.get("/products/search")

    assert reads["users"] == 2
    assert before != after


def test_searching_does_not_change_the_cached_rows(client, patch_backend):
    """Leaves every seeded product row equal to a copy taken before any request after
    guest, signed-in, text, price and card-view searches, so scoring and the card
    view's removal of fields work on copies and not on the shared rows."""
    seeded = copy.deepcopy(CATALOGUE)
    fake = patch_backend({"products": seeded, "users": [{"id": "u-1", "skin_type": "DRNT"}]},
                         "app.api.products")
    snapshot = copy.deepcopy(fake.store["products"])

    for params in ({}, {"view": "card"}, {"q": "serum"}, {"min_price": 100, "view": "card"}):
        as_user("u-1")
        client.get("/products/search", params=params)
        as_user(None)
        client.get("/products/search", params=params)

    assert fake.store["products"] == snapshot


def test_a_second_card_search_is_not_affected_by_the_first(client, patch_backend):
    """Returns the same card items on a second view=card call as on the first, still
    with ingredient_count and without product_ingredients, and a default-view call
    in between still has its product_ingredients."""
    patch_backend({"products": copy.deepcopy(CATALOGUE)}, "app.api.products")

    first = client.get("/products/search", params={"view": "card"}).json()
    default = client.get("/products/search").json()
    second = client.get("/products/search", params={"view": "card"}).json()

    assert first == second
    assert first[0]["ingredient_count"] == 2
    assert len(default[0]["product_ingredients"]) == 2


# --- filtering the cached tree in Python, as the database filter did ----------------

FILTER_ROWS = [
    {"id": "a", "name": "Niacinamide 10% Serum", "brand": "The Ordinary", "category": "Serum", "price_thb": 600},
    {"id": "b", "name": "Hyaluronic Acid", "brand": "CeraVe", "category": "Moisturizer", "price_thb": None},
    {"id": "c", "name": None, "brand": "Zinc Labs", "category": None, "price_thb": 1200},
]


@pytest.mark.parametrize("q, expected", [
    ("SERUM", ["a"]),                 # case-insensitive, on the name and on the category
    ("  cerave  ", ["b"]),            # surrounding spaces are not part of the term
    ("moisturizer", ["b"]),           # category
    ("zinc", ["c"]),                  # brand, where name and category are null
    ("nia%serum", ["a"]),             # % matches any run of characters
    ("nia*serum", ["a"]),             # PostgREST reads * as %
    ("hyaluroni_", ["b"]),            # _ matches exactly one character
    ("hyal_ic", []),                  # ... not a run
    ("10\%", ["a"]),                 # a backslash makes the % literal: "10%" is in the name
    ("a\%s", []),                    # ... so "a%s" as text is nowhere, though a%s as a wildcard is
    ("   ", ["a", "b", "c"]),         # blank but sent: matches every non-null column
    ("nothing like this", []),
])
def test_text_filter_matches_name_brand_and_category_like_ilike(q, expected):
    """Keeps the rows an ilike '%q%' on name, brand or category kept, in their order:
    case-insensitive, % and * any run, _ one character, a backslash making the next
    character literal, surrounding spaces trimmed, a null column never matching."""
    from app.api.products import filter_products
    assert [r["id"] for r in filter_products(FILTER_ROWS, q, None, None)] == expected


@pytest.mark.parametrize("low, high, expected", [
    (600, None, ["a", "c"]),          # the lower bound is inclusive
    (None, 600, ["a"]),               # the upper bound is inclusive
    (601, 1199, []),
    (600, 1200, ["a", "c"]),
    (None, None, ["a", "b", "c"]),    # no bound, no filter: a product with no price stays
])
def test_price_filter_is_inclusive_and_drops_a_product_with_no_price(low, high, expected):
    """Keeps the products priced at or above min_price and at or below max_price, and
    drops a product whose price_thb is null as soon as either bound is given."""
    from app.api.products import filter_products
    assert [r["id"] for r in filter_products(FILTER_ROWS, "", low, high)] == expected


def test_the_filters_leave_the_rows_they_are_given_untouched():
    """Returns new lists and leaves the input list and its rows as they were."""
    from app.api.products import filter_products
    rows = copy.deepcopy(FILTER_ROWS)
    kept = filter_products(rows, "serum", 100, 900)
    assert rows == FILTER_ROWS and kept == [rows[0]] and kept is not rows


# --- the rule tables ---------------------------------------------------------------

RETINOL = {"id": "ing-retinol", "name": "Retinol", "functional_group": "Retinoid"}
SALICYLIC = {"id": "ing-sa", "name": "Salicylic Acid", "functional_group": "Beta Hydroxy Acid (BHA)"}
RULE = {"ingredient_a_id": "ing-retinol", "ingredient_b_id": "ing-sa", "severity": "high",
        "warning_message": "Alternate evenings.", "conflict_rule_sources": []}
CATEGORY_RULE = {"group_a": "Retinoid", "group_b": "Beta Hydroxy Acid (BHA)", "severity": "high",
                 "warning_message": "Raises irritation.", "category_rule_sources": []}


def _analyze_store():
    return {
        "products": [_product("p-a", "Retinol Serum", [RETINOL]), _product("p-b", "BHA Liquid", [SALICYLIC])],
        "conflict_rules": [copy.deepcopy(RULE)],
        "category_conflict_rules": [copy.deepcopy(CATEGORY_RULE)],
    }


def _analyze(comparison=None):
    from app.core.services import compatibility_service as svc
    other = svc.normalize_product(_analyze_store()["products"][1])
    return svc.analyze("p-a", None, [other] if comparison is None else comparison)


def test_the_rule_tables_are_read_once_for_repeated_analyses(patch_backend):
    """Reads conflict_rules once and category_conflict_rules once for three analyze
    calls that each reach both rule passes, and every call returns the same warnings."""
    fake = patch_backend(_analyze_store(), "app.core.services.compatibility_service")
    reads = counting(fake)

    results = [_analyze() for _ in range(3)]

    assert reads["conflict_rules"] == 1 and reads["category_conflict_rules"] == 1
    assert len(results[0].warnings) == 1
    assert results[0] == results[1] == results[2]


def test_the_rule_tables_are_read_again_after_sixty_seconds(patch_backend, clock):
    """Still reports the clash 59.9 seconds after the rule tables were emptied in the
    database, and reports none at 60 seconds, when each rule table is read again."""
    fake = patch_backend(_analyze_store(), "app.core.services.compatibility_service")
    reads = counting(fake)

    assert len(_analyze().warnings) == 1
    fake.store["conflict_rules"] = []
    fake.store["category_conflict_rules"] = []
    clock[0] += 59.9
    assert len(_analyze().warnings) == 1
    clock[0] += 0.1
    assert _analyze().warnings == []
    assert reads["conflict_rules"] == 2


def test_analysis_does_not_change_the_cached_rule_rows(patch_backend):
    """Leaves the stored rule rows equal to a copy taken before the first analysis,
    after two analyses, so the second sees the rules the first saw."""
    fake = patch_backend(_analyze_store(), "app.core.services.compatibility_service")
    snapshot = copy.deepcopy((fake.store["conflict_rules"], fake.store["category_conflict_rules"]))

    first, second = _analyze(), _analyze()

    assert (fake.store["conflict_rules"], fake.store["category_conflict_rules"]) == snapshot
    assert first == second


# --- the ingredient list -----------------------------------------------------------

INGREDIENT_ROWS = [{"id": "i-water", "name": "Water", "functional_group": "Solvent"},
                   {"id": "i-gly", "name": "Glycerin", "functional_group": "Humectant"}]


def test_ingredient_search_and_functional_groups_read_the_table_once_each(client, patch_backend):
    """Reads the ingredients table once for repeated GET /ingredients/search calls with
    different terms, plus POST /ingredients/match, and once for repeated GET
    /meta/functional-groups calls; every answer is the same as an uncached read."""
    fake = patch_backend({"ingredients": copy.deepcopy(INGREDIENT_ROWS)}, "app.core.services.ingredient_lookup")
    reads = counting(fake)

    water = client.get("/ingredients/search", params={"q": "wat"}).json()["results"]
    glycerin = client.get("/ingredients/search", params={"q": "gly"}).json()["results"]
    matched = client.post("/ingredients/match", json={"names": ["Glycerin"]}).json()["matches"]
    groups = [client.get("/meta/functional-groups").json() for _ in range(2)]

    assert reads["ingredients"] == 2
    assert [r["id"] for r in water] == ["i-water"] and [r["id"] for r in glycerin] == ["i-gly"]
    assert matched[0]["id"] == "i-gly"
    assert groups[0] == groups[1] == {"functional_groups": ["Humectant", "Solvent"]}


def test_an_ingredient_added_in_the_database_appears_after_sixty_seconds(client, patch_backend, clock):
    """Returns no result for a new ingredient 59.9 seconds after the list was loaded,
    and returns it at 60 seconds."""
    fake = patch_backend({"ingredients": copy.deepcopy(INGREDIENT_ROWS)}, "app.core.services.ingredient_lookup")
    client.get("/ingredients/search", params={"q": "wat"})
    fake.store["ingredients"].append({"id": "i-new", "name": "Watermelon Extract", "functional_group": "Extract"})

    clock[0] += 59.9
    assert [r["id"] for r in client.get("/ingredients/search", params={"q": "wat"}).json()["results"]] == ["i-water"]
    clock[0] += 0.1
    assert "i-new" in [r["id"] for r in client.get("/ingredients/search", params={"q": "wat"}).json()["results"]]


# --- writes through this app clear the cache ---------------------------------------

ADMIN_MODULES = ("app.api.products", "app.api.submissions", "app.core.services.submission_service",
                 "app.core.services.ingredient_lookup", "app.core.services.image_upload",
                 "app.core.services.token", "app.core.services.compatibility_service")


@pytest.fixture
def admin_backend(patch_backend):
    from app.core.services.token import get_current_user_id
    from app.main import app
    app.dependency_overrides[get_current_user_id] = lambda: "admin-1"

    def _make():
        fake = patch_backend({
            "users": [{"id": "admin-1", "role": "admin", "skin_type": None}],
            "products": [{**copy.deepcopy(CATALOGUE[0]), "id": PROD_ID, "updated_at": UPDATED_AT}],
            "ingredients": copy.deepcopy(INGREDIENT_ROWS),
            "product_submissions": [{"id": SUB_ID, "submitted_by": "u", "status": "pending",
                                     "payload": {"name": "N", "image_path": None}, "edited_payload": None}],
        }, *ADMIN_MODULES)
        fake.rpc_results["approve_submission"] = {"product_id": "new", "slug": "new"}
        fake.rpc_results["admin_update_product"] = {"product_id": PROD_ID, "updated_at": UPDATED_AT, "slug": "s"}
        return fake
    return _make


def _warm(client):
    """Fill the product tree and the ingredient list, then change the store behind them."""
    client.get("/products/search")
    client.get("/ingredients/search", params={"q": "wat"})


def _stale_and_fresh(client, fake, write):
    """The names a search and the ingredient list show before and after `write()`."""
    edit_row(fake, "products", 0, name="Edited Name")
    edit_row(fake, "ingredients", 0, name="Edited Water")
    stale = (client.get("/products/search").json()[0]["name"],
             [r["name"] for r in client.get("/ingredients/search", params={"q": "wat"}).json()["results"]])
    write()
    fresh = (client.get("/products/search").json()[0]["name"],
             [r["name"] for r in client.get("/ingredients/search", params={"q": "edited"}).json()["results"]])
    return stale, fresh


def test_approving_a_submission_clears_the_product_and_ingredient_caches(client, admin_backend):
    """Shows the old product name and ingredient list while the cache is warm, and the
    new ones on the first search after POST /submissions/admin/{id}/approve."""
    fake = admin_backend()
    _warm(client)

    stale, fresh = _stale_and_fresh(
        client, fake, lambda: client.post(f"/submissions/admin/{SUB_ID}/approve", json={}))

    assert stale == ("Dry Serum", ["Water"])
    assert fresh == ("Edited Name", ["Edited Water"])


def test_editing_a_product_clears_the_product_and_ingredient_caches(client, admin_backend):
    """Shows the old product name and ingredient list while the cache is warm, and the
    new ones on the first search after PATCH /products/{id}."""
    fake = admin_backend()
    _warm(client)

    stale, fresh = _stale_and_fresh(
        client, fake, lambda: client.patch(f"/products/{PROD_ID}", json={"updated_at": UPDATED_AT, "price_thb": 1}))

    assert stale == ("Dry Serum", ["Water"])
    assert fresh == ("Edited Name", ["Edited Water"])


@pytest.mark.parametrize("route", ["approve", "patch"])
def test_a_refused_admin_write_still_clears_the_caches(client, admin_backend, route):
    """Clears the product and ingredient caches even when the database function
    answers with an error (HTTP 409 for a stale edit or a duplicate), because the
    write may have committed before the error came back."""
    fake = admin_backend()
    error = APIError({"code": "SBDUP" if route == "approve" else "SBSTL", "message": "no", "details": None, "hint": None})
    fake.rpc_results["approve_submission" if route == "approve" else "admin_update_product"] = error
    _warm(client)

    def write():
        if route == "approve":
            return client.post(f"/submissions/admin/{SUB_ID}/approve", json={})
        return client.patch(f"/products/{PROD_ID}", json={"updated_at": UPDATED_AT, "price_thb": 1})

    stale, fresh = _stale_and_fresh(client, fake, write)

    assert stale == ("Dry Serum", ["Water"])
    assert fresh == ("Edited Name", ["Edited Water"])
