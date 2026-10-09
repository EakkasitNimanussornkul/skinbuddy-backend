"""The unknown-slug fallback of resolve_product_record reads only the columns its
name matching uses, then loads the one product it picks.

Before, step 2 fetched every product with its whole ingredient and source tree just
to compare names in Python (audit 2026-10-09, finding 4). Which product each
identifier resolves to must not change, so these tests also pin that.

The fake client ignores select strings, so the fake used here applies the narrow
select itself: it keeps only the listed columns, as the database would, and records
every query. tests/unit/test_product_resolution.py covers the resolution rules.
"""

import asyncio

from app.api.products import PRODUCT_SELECT, resolve_product_record
from tests.conftest import FakeQuery, FakeSupabase

NARROW = "id, brand, name"


class SelectAwareQuery(FakeQuery):
    def __init__(self, table_name, store, log, log_orders):
        super().__init__(table_name, store)
        self._log = log
        self._log_orders = log_orders
        self._columns = None
        self._ordered_by = []

    def select(self, columns, *args, **kwargs):
        self._columns = columns
        return self

    def order(self, column, **kwargs):
        self._ordered_by.append((column, kwargs.get("foreign_table")))
        return self

    def execute(self):
        res = super().execute()
        self._log.append((self._columns, [(c, v) for c, v, _ in self._filters]))
        self._log_orders.append((self._ordered_by, self._limit))
        if self._columns == NARROW:
            res.data = [{k: row[k] for k in ("id", "brand", "name")} for row in res.data]
        return res


class SelectAwareFake(FakeSupabase):
    def __init__(self, store):
        super().__init__(store)
        self.log = []
        self.orders = []      # (the .order() calls, the .limit()) of each query, parallel to log

    def table(self, name):
        return SelectAwareQuery(name, self.store, self.log, self.orders)


def row(pid, brand, name, slug=None):
    return {
        "id": pid, "brand": brand, "name": name, "category": "Cleanser", "slug": slug,
        "description": None, "price_thb": 500, "price_usd": 15, "image_url": None,
        "product_ingredients": [{"ingredients": {"id": f"ing-{pid}", "name": f"Ingredient of {pid}"}}],
    }


CERAVE = row("id-cerave", "CeraVe", "Hydrating Facial Cleanser")
CERAVE_TWO = row("id-cerave-two", "CeraVe", "Hydrating Facial Lotion")
ORDINARY = row("id-ordinary", "The Ordinary", "Niacinamide 10% + Zinc 1%")


def test_a_typo_slug_resolves_through_the_narrow_scan_to_the_full_product(monkeypatch):
    """Returns the CeraVe cleanser, with its ingredient rows, for the misspelt slug
    "cerave-hydrating-facial-cleanzer": the exact-slug lookup finds nothing, the scan
    reads only id, brand and name of every product, and the matched product is then
    loaded by id with the full select."""
    import app.api.products as module
    fake = SelectAwareFake({"products": [ORDINARY, CERAVE, CERAVE_TWO]})
    monkeypatch.setattr(module, "supabase", fake)

    found = asyncio.run(resolve_product_record("cerave-hydrating-facial-cleanzer"))

    assert found["id"] == "id-cerave"
    assert found["product_ingredients"][0]["ingredients"]["name"] == "Ingredient of id-cerave"
    assert fake.log == [
        (PRODUCT_SELECT, [("slug", "cerave-hydrating-facial-cleanzer")]),
        (NARROW, []),
        (PRODUCT_SELECT, [("id", "id-cerave")]),
    ]
    # the full load keeps the ingredients in pack order, as every other read of the tree does
    assert fake.orders[-1] == ([("position", "product_ingredients")], 1)
    # the scan is capped at 2000 rows, as it always was
    assert fake.orders[1][1] == 2000


def test_an_unknown_slug_reads_only_the_narrow_columns_and_returns_none(monkeypatch):
    """Returns None for a slug no strategy matches, after reading the exact-slug column
    and then id, brand and name of every product, and never loads a product tree for
    the scan."""
    import app.api.products as module
    fake = SelectAwareFake({"products": [ORDINARY, CERAVE]})
    monkeypatch.setattr(module, "supabase", fake)

    assert asyncio.run(resolve_product_record("completely-unknown-thing")) is None

    assert [columns for columns, _ in fake.log] == [PRODUCT_SELECT, NARROW]
    assert fake.log[0][1] == [("slug", "completely-unknown-thing")]


def test_an_exact_slug_is_answered_by_one_query_and_never_reaches_the_scan(monkeypatch):
    """Returns the product whose slug column equals the identifier after exactly one
    query, so the scan below it is not read at all."""
    import app.api.products as module
    exact = row("id-exact", "COSRX", "Snail Mucin Essence", slug="cosrx-snail-mucin-essence")
    fake = SelectAwareFake({"products": [ORDINARY, exact]})
    monkeypatch.setattr(module, "supabase", fake)

    found = asyncio.run(resolve_product_record("cosrx-snail-mucin-essence"))

    assert found["id"] == "id-exact"
    assert len(fake.log) == 1


def test_the_scan_resolves_the_first_match_in_database_order_as_before(monkeypatch):
    """Returns the first stored of two products that both contain every word of the
    identifier, and loads that one by its id, not the other and not the first row."""
    import app.api.products as module
    fake = SelectAwareFake({"products": [ORDINARY, CERAVE_TWO, CERAVE]})
    monkeypatch.setattr(module, "supabase", fake)

    found = asyncio.run(resolve_product_record("cerave hydrating facial"))

    assert found["id"] == "id-cerave-two"
    assert fake.log[-1] == (PRODUCT_SELECT, [("id", "id-cerave-two")])


def test_a_name_scan_match_is_also_loaded_whole(monkeypatch):
    """Returns the full product, ingredient rows included, for an identifier that only
    the normalised-name scan (step 2) can answer, not just the fuzzy step 3."""
    import app.api.products as module
    fake = SelectAwareFake({"products": [ORDINARY, CERAVE]})
    monkeypatch.setattr(module, "supabase", fake)

    found = asyncio.run(resolve_product_record("hydratingfacialcleanser"))

    assert found["id"] == "id-cerave" and found["product_ingredients"]
    assert fake.log[-1] == (PRODUCT_SELECT, [("id", "id-cerave")])
