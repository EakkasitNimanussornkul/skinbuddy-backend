"""GET /meta/*, GET /ingredients/search and POST /ingredients/match.

Search and the matcher compare names through ingredient_key(), the shared
normaliser approve_submission() mirrors in SQL, so "aqua" finds Water here and
links to Water on approval.
"""

import pytest

from app.core.services import ingredient_lookup
from app.schemas import CATEGORIES, CONCERN_TAGS

ROWS = [
    {"id": "i-water", "name": "Water", "functional_group": "Solvent"},
    {"id": "i-rose", "name": "Rosa Damascena Flower Water", "functional_group": "Skin-Conditioning Agent"},
    {"id": "i-gly", "name": "Glycerin", "functional_group": "Humectant"},
    {"id": "i-pca", "name": "Sodium PCA", "functional_group": "Humectant"},
    {"id": "i-bg", "name": "Beta-Glucan", "functional_group": "Skin-Conditioning Agent"},
    {"id": "i-sh", "name": "Sodium Hyaluronate", "functional_group": "Humectant"},
    {"id": "i-hy", "name": "Hydrolyzed Sodium Hyaluronate", "functional_group": " Humectant"},
    {"id": "i-gt", "name": "Camellia Sinensis (Green Tea) Leaf Extract", "functional_group": None},
    {"id": "i-nia", "name": "Niacinamide", "functional_group": ""},
]


@pytest.fixture
def ingredients(patch_backend):
    def _make(rows=ROWS):
        return patch_backend({"ingredients": [dict(r) for r in rows]}, "app.core.services.ingredient_lookup")
    return _make


# --- /meta ------------------------------------------------------------------

def test_meta_categories_are_the_contract_list(client):
    """Returns {"categories": [...]} with the nine contract categories in order."""
    assert client.get("/meta/categories").json() == {"categories": [
        "Cleansers", "Toners", "Serums", "Treatments", "Moisturizers",
        "Exfoliators", "Sun Care", "Masks", "Eye Care"]}


def test_meta_concern_tags_are_the_contract_list(client):
    """Returns {"concern_tags": [...]} with the nine contract tags in order."""
    assert client.get("/meta/concern-tags").json() == {"concern_tags": [
        "Dry skin", "Dehydrated", "Sensitive", "Oily", "Acne-prone",
        "Dark spots", "Dullness", "Fine lines", "Redness"]}


def test_meta_lists_are_the_ones_the_request_models_validate_against(client):
    """Serves the same CATEGORIES and CONCERN_TAGS the request models check, so the page
    cannot offer a value the API refuses."""
    assert tuple(client.get("/meta/categories").json()["categories"]) == CATEGORIES
    assert tuple(client.get("/meta/concern-tags").json()["concern_tags"]) == CONCERN_TAGS


def test_meta_functional_groups_lists_each_stored_value_once_exactly(client, ingredients):
    """Returns every distinct functional_group sorted, as stored (a leading space kept,
    since approval compares exact text), leaving out null and blank values."""
    ingredients()
    assert client.get("/meta/functional-groups").json() == {"functional_groups": [
        " Humectant", "Humectant", "Skin-Conditioning Agent", "Solvent"]}


# --- /ingredients/search -----------------------------------------------------

def search(client, q, **params):
    resp = client.get("/ingredients/search", params={"q": q, **params})
    assert resp.status_code == 200
    return resp.json()["results"]


def test_search_finds_water_for_aqua_and_names_the_alias(client, ingredients):
    """Returns Water first for q="aqua", with matched_alias "aqua", id and functional_group.
    A name found only through the alias ("...Flower Water") also carries matched_alias
    "aqua"; one that contains "aqua" itself ("Aquaxyl") carries null."""
    ingredients(ROWS + [{"id": "i-aquaxyl", "name": "Aquaxyl", "functional_group": None}])
    results = {r["id"]: r for r in search(client, "aqua")}
    assert list(results)[0] == "i-water"
    assert results["i-water"] == {"id": "i-water", "name": "Water", "functional_group": "Solvent", "matched_alias": "aqua"}
    assert results["i-rose"]["matched_alias"] == "aqua"
    assert results["i-aquaxyl"]["matched_alias"] is None


def test_search_strips_prefixes_brackets_and_hyphens(client, ingredients):
    """Finds Beta-Glucan for "en:beta-glucan", Sodium PCA for "(sodium pca)", and the green
    tea extract for "Camellia Sinensis (Green Tea)", the way the importer normalises names."""
    ingredients()
    assert search(client, "en:beta-glucan")[0]["id"] == "i-bg"
    assert search(client, "(sodium pca)")[0]["id"] == "i-pca"
    assert search(client, "Camellia Sinensis (Green Tea)")[0]["id"] == "i-gt"


def test_search_ranks_same_name_then_prefix_then_word_then_contains(client, ingredients):
    """Orders results: the same name first, then names starting with the query, then names
    with a word starting with it, then names containing it; ties go to the shorter name."""
    rows = [{"id": "contains", "name": "Ethylhexylglycerin", "functional_group": None},
            {"id": "word", "name": "Polyglyceryl Glycerin", "functional_group": None},
            {"id": "prefix-long", "name": "Glycerin Monostearate", "functional_group": None},
            {"id": "prefix", "name": "Glyceryl", "functional_group": None},
            {"id": "same", "name": "glycerin", "functional_group": None}]
    ingredients(rows)
    assert [r["id"] for r in search(client, "Glycerin")] == ["same", "prefix-long", "word", "contains"]
    assert [r["id"] for r in search(client, "glycer")] == ["same", "prefix", "prefix-long", "word", "contains"]


def test_search_returns_at_most_limit_results(client, ingredients):
    """Returns 8 results by default and no more than ?limit= asks for."""
    ingredients([{"id": f"i{n}", "name": f"Extract {n}", "functional_group": None} for n in range(30)])
    assert len(search(client, "extract")) == 8
    assert len(search(client, "extract", limit=3)) == 3


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "x" * 61}, {"q": "water", "limit": 0},
                                    {"q": "water", "limit": 21}, {}],
                         ids=["empty q", "q over 60", "limit 0", "limit 21", "no q"])
def test_search_refuses_an_out_of_range_query_with_422(client, ingredients, params):
    """Returns HTTP 422 for q empty, missing or over 60 characters, or limit outside 1-20."""
    ingredients()
    assert client.get("/ingredients/search", params=params).status_code == 422


def test_search_for_only_brackets_returns_nothing(client, ingredients):
    """Returns no results for a query that normalises to nothing, such as "()"."""
    ingredients()
    assert search(client, "()") == []


# --- /ingredients/match ------------------------------------------------------

def test_match_keeps_input_order_and_marks_new_names(client, ingredients):
    """Returns one match per input in input order: the stored name and id for a name that
    normalises to one ingredient ("AQUA" -> Water, with matched_alias "aqua"; "sodium pca"
    -> Sodium PCA), and id, name and matched_alias null for a name not in the list."""
    ingredients()
    resp = client.post("/ingredients/match", json={"names": ["sodium pca", "Mystery Extract", "AQUA"]})
    assert resp.json() == {"matches": [
        {"input": "sodium pca", "id": "i-pca", "name": "Sodium PCA", "matched_alias": None, "ambiguous": False},
        {"input": "Mystery Extract", "id": None, "name": None, "matched_alias": None, "ambiguous": False},
        {"input": "AQUA", "id": "i-water", "name": "Water", "matched_alias": "aqua", "ambiguous": False},
    ]}


def test_match_does_not_pick_between_two_rows_with_the_same_key(client, ingredients):
    """Returns id null and ambiguous true for a name that normalises to two ingredients
    rows, as approval refuses to choose between them."""
    ingredients(ROWS + [{"id": "i-water-2", "name": "WATER", "functional_group": None}])
    [match] = client.post("/ingredients/match", json={"names": ["Aqua"]}).json()["matches"]
    assert match == {"input": "Aqua", "id": None, "name": None, "matched_alias": None, "ambiguous": True}


def test_match_needs_whole_name_equality_not_a_prefix(client, ingredients):
    """Returns id null for "Sodium", which only starts an ingredient's name: the matcher
    links whole names, unlike search."""
    ingredients()
    assert client.post("/ingredients/match", json={"names": ["Sodium"]}).json()["matches"][0]["id"] is None


def test_match_accepts_100_names_and_refuses_101(client, ingredients):
    """Returns HTTP 200 for 100 names and HTTP 422 for 101."""
    ingredients()
    assert client.post("/ingredients/match", json={"names": ["Water"] * 100}).status_code == 200
    assert client.post("/ingredients/match", json={"names": ["Water"] * 101}).status_code == 422


def test_match_of_no_names_returns_an_empty_list(client, ingredients):
    """Returns {"matches": []} for an empty names list, and a blank name as new."""
    ingredients()
    assert client.post("/ingredients/match", json={"names": []}).json() == {"matches": []}
    assert client.post("/ingredients/match", json={"names": ["  "]}).json()["matches"][0]["id"] is None


# --- paging ------------------------------------------------------------------

class PagedQuery:
    """A query that serves `rows` a page at a time through .range(), as PostgREST does."""

    def __init__(self, rows, log):
        self._rows, self._log = rows, log
        self._slice = None

    def range(self, start, end):
        self._log.append((start, end))
        self._slice = self._rows[start:end + 1]
        return self

    def execute(self):
        return type("Resp", (), {"data": self._slice})()


def test_fetch_all_rows_reads_every_page():
    """Returns all 2500 rows by asking for 0-999, 1000-1999 and 2000-2999, so a table past
    PostgREST's 1000-row cap is read in full."""
    rows = [{"id": n} for n in range(2500)]
    log = []
    assert ingredient_lookup.fetch_all_rows(lambda: PagedQuery(rows, log)) == rows
    assert log == [(0, 999), (1000, 1999), (2000, 2999)]


def test_fetch_all_rows_reads_one_more_page_after_an_exactly_full_one():
    """Returns all 1000 rows of a table of exactly one full page, and stops after the
    empty page that follows."""
    rows = [{"id": n} for n in range(1000)]
    log = []
    assert ingredient_lookup.fetch_all_rows(lambda: PagedQuery(rows, log)) == rows
    assert log == [(0, 999), (1000, 1999)]
