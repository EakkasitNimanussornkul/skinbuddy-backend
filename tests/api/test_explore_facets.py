"""GET /products/search?category=&brand= and GET /meta/facets.

The Explore page pages the search (limit, offset, X-Total-Count), so the category
and brand filters have to run on the server, before ranking and before the page is
cut. They read a category the way the page's cleanString does, so the page and the
server agree on what matches. GET /meta/facets lists the categories and brands that
have a product, for the page's chips and brand list.

The fake Supabase client ignores select strings, so the real select is proven by the
read-only live run reported with this change; the test here spies on the columns asked for.
"""

import copy
import inspect

import pytest


def _product(pid, name, category, brand, price=500):
    return {
        "id": pid, "brand": brand, "name": name, "category": category, "slug": None,
        "description": "d", "price_thb": price, "price_usd": 15, "image_url": None,
        "product_sources": [], "product_ingredients": [],
    }


# Stored in this order. Two cleansers, two Cetaphil products, a sun care product with a
# two-word category, and one product with no category, brand or price.
STORE = [
    _product("c-cosrx", "Low pH Foam", "Cleansers", "COSRX", 300),
    _product("t-cosrx", "AHA Toner", "Toners", "COSRX", 500),
    _product("c-ceta", "Gentle Wash", "Cleansers", "Cetaphil", 800),
    _product("s-boj", "Relief Sun", "Sun Care", "Beauty of Joseon", 700),
    _product("m-ceta", "Rich Cream", "Moisturizers", "Cetaphil", 1000),
    _product("none", "Mystery", None, None, None),
]


def ids(resp):
    return [p["id"] for p in resp.json()]


def search(client, **params):
    return client.get("/products/search", params=params)


# --- the category normaliser ----------------------------------------------------------

@pytest.mark.parametrize("value, cleaned", [
    ("Cleansers", "cleanser"),
    ("cleanser", "cleanser"),
    ("CLEANSERS", "cleanser"),
    (" Toners ", "toner"),
    ("Sun Care", "sun care"),
    ("sun+care", "sun care"),
    ("Sun%20Care", "sun care"),
    ("Moisturizers", "moisturizer"),
    ("Moisturizer", "moisturizer"),
    ("Eye Care", "eye care"),
    ("Sunscreen", "sunscreen"),
    ("Glass", "glas"),
    ("", ""),
    (None, ""),
    ("All", "all"),
    ("Something Else", "something else"),
])
def test_clean_category_reads_a_category_the_way_the_explore_page_does(value, cleaned):
    """Returns the lower-cased, trimmed category with one trailing "s" dropped and a "+" or
    "%20" turned into a space: Cleansers, cleanser, CLEANSERS and " Toners " give
    cleanser, cleanser, cleanser and toner; "Sun Care", "sun+care" and "Sun%20Care" all
    give "sun care"; Moisturizers and Moisturizer both give moisturizer; Eye Care gives
    "eye care"; "" and None give ""; "All" gives "all"; a name with no trailing s is kept."""
    from app.api.products import clean_category
    assert clean_category(value) == cleaned


def test_clean_category_has_no_sunscreen_alias_and_drops_only_one_s():
    """Returns "sunscreen" for Sunscreen, which is not "sun care", and "clas" for
    "Class" with its one trailing s dropped, not both."""
    from app.api.products import clean_category
    assert clean_category("Sunscreen") != clean_category("Sun Care")
    assert clean_category("Class") == "clas"


# --- the filters on the route ---------------------------------------------------------

@pytest.mark.parametrize("category", ["Cleansers", "cleansers", "Cleanser", "CLEANSERS", " cleansers ", "cleanser"])
def test_category_matches_however_it_is_spelled(client, patch_supabase, category):
    """Returns the two cleansers (c-cosrx, c-ceta) and X-Total-Count 2 for category
    Cleansers, cleansers, Cleanser, CLEANSERS, " cleansers " and cleanser."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = search(client, category=category)

    assert resp.status_code == 200
    assert ids(resp) == ["c-cosrx", "c-ceta"]
    assert resp.headers["x-total-count"] == "2"


@pytest.mark.parametrize("category", ["Sun Care", "sun care", "Sun+Care", "Sun%20Care", "SUN CARE"])
def test_a_two_word_category_matches_with_a_plus_a_percent_20_or_a_space(client, patch_supabase, category):
    """Returns only the Sun Care product (s-boj) for category Sun Care, sun care, Sun+Care
    (a literal plus), Sun%20Care (a literal %20) and SUN CARE."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    assert ids(search(client, category=category)) == ["s-boj"]


def test_a_plus_or_an_encoded_space_in_the_address_reads_as_a_space(client, patch_supabase):
    """Returns the Sun Care product for the raw addresses ?category=Sun+Care and
    ?category=Sun%20Care, which is how a browser sends a space."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    assert ids(client.get("/products/search?category=Sun+Care")) == ["s-boj"]
    assert ids(client.get("/products/search?category=Sun%20Care")) == ["s-boj"]


def test_sunscreen_is_not_an_alias_of_sun_care(client, patch_supabase):
    """Returns [] with X-Total-Count 0 for category Sunscreen, because no product has
    that category and no alias to Sun Care exists."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = search(client, category="Sunscreen")

    assert resp.json() == [] and resp.headers["x-total-count"] == "0"


@pytest.mark.parametrize("brand", ["COSRX", "cosrx", "Cosrx", "  cosrx  "])
def test_brand_matches_ignoring_case_and_surrounding_spaces(client, patch_supabase, brand):
    """Returns both COSRX products (c-cosrx, t-cosrx) and X-Total-Count 2 for brand COSRX,
    cosrx, Cosrx and "  cosrx  "."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = search(client, brand=brand)

    assert ids(resp) == ["c-cosrx", "t-cosrx"]
    assert resp.headers["x-total-count"] == "2"


def test_a_brand_must_match_whole_not_part_of_the_name(client, patch_supabase):
    """Returns [] for brand "Ceta" and brand "Beauty", which are only part of a brand name."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    assert search(client, brand="Ceta").json() == []
    assert search(client, brand="Beauty").json() == []


@pytest.mark.parametrize("category", ["Clean", "Care", "Sun", "Cleansers Toners", "Moisturizer s"])
def test_a_category_must_match_whole_not_part_of_a_name(client, patch_supabase, category):
    """Returns [] with X-Total-Count 0 for category Clean, Care, Sun, "Cleansers Toners"
    and "Moisturizer s", which are only part of a category name or run two together."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = search(client, category=category)

    assert resp.json() == [] and resp.headers["x-total-count"] == "0"


@pytest.mark.parametrize("params", [{}, {"category": ""}, {"brand": ""}, {"category": "All"}, {"category": "all"},
                                    {"brand": "All"}, {"brand": "ALL"}, {"category": " All ", "brand": ""}])
def test_no_category_or_brand_or_all_or_empty_means_no_filter(client, patch_supabase, params):
    """Returns all six products in stored order and X-Total-Count 6 with no category or
    brand, with either empty, and with All in any case, the page's chip value."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = search(client, **params)

    assert ids(resp) == [p["id"] for p in STORE]
    assert resp.headers["x-total-count"] == "6"


@pytest.mark.parametrize("params", [{"category": "Nothing"}, {"brand": "Nobody"}, {"category": "Cleansers", "brand": "Nobody"}])
def test_an_unknown_category_or_brand_returns_an_empty_list_not_an_error(client, patch_supabase, params):
    """Returns HTTP 200, [] and X-Total-Count 0 for a category nothing has, a brand
    nothing has, and a known category with an unknown brand, never a 422."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    resp = search(client, **params)

    assert resp.status_code == 200
    assert resp.json() == []
    assert resp.headers["x-total-count"] == "0"


def test_a_product_with_no_category_or_brand_never_matches_a_filter(client, patch_supabase):
    """Leaves the product with no category out of category "s" and category "none", and
    the product with no brand out of brand "none" and brand "None"."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    for params in ({"category": "s"}, {"category": "none"}, {"brand": "none"}, {"brand": "None"}):
        assert search(client, **params).json() == []


def test_category_brand_q_and_price_combine_as_and(client, patch_supabase):
    """Returns only the products that pass every filter given: COSRX in Cleansers gives
    c-cosrx; Cleansers with min_price 500 gives c-ceta; brand Cetaphil with q "cream"
    gives m-ceta; Cleansers priced at most 300 gives c-cosrx; COSRX in Sun Care gives []."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    assert ids(search(client, category="Cleansers", brand="cosrx")) == ["c-cosrx"]
    assert ids(search(client, category="Cleansers", min_price=500)) == ["c-ceta"]
    assert ids(search(client, brand="Cetaphil", q="cream")) == ["m-ceta"]
    assert ids(search(client, category="Cleansers", max_price=300)) == ["c-cosrx"]
    assert search(client, category="Sun Care", brand="COSRX").json() == []


def test_the_total_counts_the_matches_of_every_filter_before_the_page_is_cut(client, patch_supabase):
    """Returns X-Total-Count 2 with one product in the body for category Cleansers
    limit=1, X-Total-Count 1 once brand COSRX is added, and X-Total-Count 2 for brand
    Cetaphil limit=1, though six products are stored."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    page = search(client, category="Cleansers", limit=1)
    narrowed = search(client, category="Cleansers", brand="COSRX", limit=1)
    brand = search(client, brand="Cetaphil", limit=1)

    assert len(page.json()) == 1 and page.headers["x-total-count"] == "2"
    assert len(narrowed.json()) == 1 and narrowed.headers["x-total-count"] == "1"
    assert len(brand.json()) == 1 and brand.headers["x-total-count"] == "2"


def test_the_page_is_cut_from_the_filtered_list_not_the_whole_catalogue(client, patch_supabase):
    """Returns c-ceta, the second cleanser, for category Cleansers limit=1 offset=1, and
    c-cosrx for limit=1 offset=0, although c-ceta is the third product stored: a page is a
    slice of the filtered list, so a filter cannot leave a page short or empty."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    assert ids(search(client, category="Cleansers", limit=1, offset=0)) == ["c-cosrx"]
    assert ids(search(client, category="Cleansers", limit=1, offset=1)) == ["c-ceta"]


def test_the_pages_of_a_filtered_query_put_together_are_the_unpaged_filtered_list(client, patch_supabase):
    """Returns, for a category, a brand and both together over 30 stored products, pages of
    1, 4, 12 and 50 that join to exactly the unpaged filtered response, with the same
    X-Total-Count (the filtered length) on every page."""
    store = [_product(f"p-{i:02d}", f"Item {i}", ("Cleansers", "Toners", "Sun Care")[i % 3],
                      ("A Brand", "b brand")[i % 2]) for i in range(30)]
    patch_supabase({"products": store}, "app.api.products")

    for params in ({"category": "Cleanser"}, {"brand": "B BRAND"}, {"category": "toners", "brand": "a brand"}):
        full = search(client, **params)
        assert 0 < len(full.json()) < 30
        for step in (1, 4, 12, 50):
            out, offset = [], 0
            while True:
                page = search(client, **params, limit=step, offset=offset)
                assert page.headers["x-total-count"] == str(len(full.json()))
                out += page.json()
                if len(page.json()) < step:
                    break
                offset += step
            assert out == full.json()


def test_an_offset_past_the_end_of_a_filtered_list_returns_an_empty_list_and_the_real_total(client, patch_supabase):
    """Returns HTTP 200, [] and X-Total-Count 2 for category Cleansers offset=2 and
    offset=50, and X-Total-Count 1 for brand Beauty of Joseon offset=1."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    for params in ({"category": "Cleansers", "offset": 2}, {"category": "Cleansers", "offset": 50}):
        resp = search(client, **params)
        assert resp.status_code == 200 and resp.json() == [] and resp.headers["x-total-count"] == "2"
    resp = search(client, brand="Beauty of Joseon", offset=1)
    assert resp.json() == [] and resp.headers["x-total-count"] == "1"


def test_without_a_limit_a_filtered_search_still_returns_at_most_100(client, patch_supabase):
    """Returns 100 products and X-Total-Count 120 for category cleanser with no limit
    when 120 cleansers are stored, and the other 20 for offset=100."""
    patch_supabase({"products": [_product(f"p-{i:03d}", f"Item {i}", "Cleansers", "B") for i in range(120)]},
                   "app.api.products")

    first = search(client, category="cleanser")
    rest = search(client, category="cleanser", offset=100)

    assert len(first.json()) == 100 and first.headers["x-total-count"] == "120"
    assert ids(rest) == [f"p-{i:03d}" for i in range(100, 120)]


def test_the_category_filter_combines_with_q_so_a_q_match_outside_the_category_is_left_out(client, patch_supabase):
    """Returns c-cosrx and t-cosrx for q "cosrx" alone, and only c-cosrx for q "cosrx"
    with category Cleansers: the brand match in Toners is left out."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    assert ids(search(client, q="cosrx")) == ["c-cosrx", "t-cosrx"]
    assert ids(search(client, q="cosrx", category="Cleansers")) == ["c-cosrx"]


def test_the_card_view_filters_like_the_default_view(client, patch_supabase):
    """Returns the same ids and X-Total-Count for view=card as for the default view with
    category Cleansers, and no product_ingredients in the card items."""
    patch_supabase({"products": copy.deepcopy(STORE)}, "app.api.products")

    default = search(client, category="Cleansers")
    card = search(client, category="Cleansers", view="card")

    assert ids(card) == ids(default) == ["c-cosrx", "c-ceta"]
    assert card.headers["x-total-count"] == default.headers["x-total-count"] == "2"
    assert all("product_ingredients" not in p for p in card.json())


# --- GET /meta/facets ------------------------------------------------------------------

FACET_STORE = [
    _product("1", "A", "Toners", "cosrx"),
    _product("2", "B", "Zebra Care", "Anua"),
    _product("3", "C", "Cleansers", "Beauty of Joseon"),
    _product("4", "D", "Sun Care", "Anua"),
    _product("5", "E", "apple care", "COSRX"),
    _product("6", "F", "Cleansers", ""),
    _product("7", "G", None, None),
    _product("8", "H", "", "Anua"),
]


def facets(client, **params):
    return client.get("/meta/facets", params=params)


def test_facets_lists_the_categories_and_brands_that_have_a_product_and_the_total(client, patch_supabase):
    """Returns exactly the JSON {categories: [Cleansers, Toners, Sun Care, apple care,
    Zebra Care], brands: [Anua, Beauty of Joseon, COSRX, cosrx], total: 8}: the known
    categories in the app's order, then other values alphabetically; brands sorted
    ignoring case, with null and empty brands and categories skipped; total is every product."""
    patch_supabase({"products": copy.deepcopy(FACET_STORE)}, "app.api.meta")

    resp = facets(client)

    assert resp.status_code == 200
    assert resp.json() == {
        "categories": ["Cleansers", "Toners", "Sun Care", "apple care", "Zebra Care"],
        "brands": ["Anua", "Beauty of Joseon", "COSRX", "cosrx"],
        "total": 8,
    }


def test_facets_keys_come_in_a_fixed_order_and_there_are_no_others(client, patch_supabase):
    """Returns the keys categories, brands and total, in that order, and no others."""
    patch_supabase({"products": copy.deepcopy(FACET_STORE)}, "app.api.meta")

    assert list(facets(client).json()) == ["categories", "brands", "total"]


def test_facets_leaves_out_a_category_no_product_has(client, patch_supabase):
    """Returns no Serums, Masks, Eye Care, Exfoliators or Moisturizers in categories when
    no product has them, though the app accepts those categories for a submission."""
    patch_supabase({"products": copy.deepcopy(FACET_STORE)}, "app.api.meta")

    categories = facets(client).json()["categories"]

    assert not {"Serums", "Masks", "Eye Care", "Exfoliators", "Moisturizers"} & set(categories)


def test_facets_follow_the_apps_category_order_not_the_order_of_the_rows(client, patch_supabase):
    """Returns Cleansers, Toners, Treatments, Moisturizers, Exfoliators, Sun Care, Masks
    in the order of the app's category list for products stored in reverse order."""
    names = ["Masks", "Sun Care", "Exfoliators", "Moisturizers", "Treatments", "Toners", "Cleansers"]
    patch_supabase({"products": [_product(str(i), "N", c, "B") for i, c in enumerate(names)]}, "app.api.meta")

    assert facets(client).json()["categories"] == [
        "Cleansers", "Toners", "Treatments", "Moisturizers", "Exfoliators", "Sun Care", "Masks"]


def test_facets_brands_are_sorted_ignoring_case(client, patch_supabase):
    """Returns the brands apple, Banana, cherry, Date in that order, not capitals first."""
    brands = ["cherry", "Date", "Banana", "apple"]
    patch_supabase({"products": [_product(str(i), "N", "Toners", b) for i, b in enumerate(brands)]}, "app.api.meta")

    assert facets(client).json()["brands"] == ["apple", "Banana", "cherry", "Date"]


def test_facets_do_not_depend_on_q_price_or_paging(client, patch_supabase):
    """Returns the same facets with q, min_price, max_price, limit, offset and category
    sent as with none of them, because the route takes no parameters."""
    patch_supabase({"products": copy.deepcopy(FACET_STORE)}, "app.api.meta")

    plain = facets(client).json()
    extra = facets(client, q="zzz", min_price=999999, max_price=1, limit=1, offset=50, category="Toners").json()

    assert extra == plain


def test_facets_need_no_sign_in(client, patch_supabase):
    """Returns HTTP 200 for a request with no Authorization header and no cookie."""
    patch_supabase({"products": copy.deepcopy(FACET_STORE)}, "app.api.meta")

    assert facets(client).status_code == 200


def test_facets_of_an_empty_catalogue_are_empty(client, patch_supabase):
    """Returns {categories: [], brands: [], total: 0} when no product is stored."""
    patch_supabase({"products": []}, "app.api.meta")

    assert facets(client).json() == {"categories": [], "brands": [], "total": 0}


def test_facets_ask_for_only_the_category_and_brand_columns(client, patch_supabase):
    """Reads the products table once with the select "category,brand" and no other,
    not the ingredient tree the product search selects."""
    fake = patch_supabase({"products": copy.deepcopy(FACET_STORE)}, "app.api.meta")
    asked = []
    original = fake.table

    def table(name):
        query = original(name)
        real = query.select
        query.select = lambda *a, **k: (asked.append((name, a)), real(*a, **k))[1]
        return query

    fake.table = table

    facets(client)

    assert asked == [("products", ("category,brand",))]


def test_the_facets_route_is_a_plain_function_run_in_the_threadpool():
    """Is a plain function, not a coroutine function, because it reads the database with
    the synchronous client."""
    from app.api.meta import get_facets
    assert not inspect.iscoroutinefunction(get_facets)


def test_the_openapi_description_lists_the_facets_route_and_the_two_new_parameters(client):
    """Lists GET /meta/facets, and category and brand as optional query parameters of
    GET /products/search, in /openapi.json."""
    spec = client.get("/openapi.json").json()
    search_params = {p["name"]: p for p in spec["paths"]["/products/search"]["get"]["parameters"]}

    assert "get" in spec["paths"]["/meta/facets"]
    assert not search_params["category"]["required"] and not search_params["brand"]["required"]
