"""Finding ingredients by name: search, the paste matcher, and the review screen.

Every comparison goes through ingredient_key() from ingredient_names.py, the
key approve_submission() and admin_update_product() use in SQL
(ingredient_name_key()), so what search and the matcher say links to Water is
what approval links to.

The whole ingredients table is read and matched here rather than filtered by
the database: a stored name has to be put through the same normaliser as the
query ("Beta-Glucan" keys as "beta glucan"), which an ilike cannot do. The
table holds a few hundred rows (384 on 2026-10-04).
"""

from typing import Any, Callable, Dict, List, Optional

from app.core import cache
from app.core.services.ingredient_names import clean_ingredient_text, ingredient_key, matched_alias
from app.db.connection import supabase

PAGE_SIZE = 1000   # PostgREST's default max-rows: a bigger request is cut short silently


def fetch_all_rows(build_query: Callable[[], Any]) -> List[Dict[str, Any]]:
    """Every row of a query, a page at a time. build_query must return a fresh,
    ordered query each call."""
    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        page = build_query().range(start, start + PAGE_SIZE - 1).execute().data or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def load_ingredients() -> List[Dict[str, Any]]:
    """id, name and functional_group of every ingredient, each with its key.
    The same for every caller, so cached for 60 s (app.core.cache): treat the
    rows as read-only."""
    def load():
        rows = fetch_all_rows(
            lambda: supabase.table("ingredients").select("id, name, functional_group").order("id"))
        return [{**row, "_key": ingredient_key(row.get("name") or "")} for row in rows if row.get("name")]
    return cache.get_or_load("ingredients", load)


def matches_for(name: str, ingredients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ingredients rows whose key equals this name's key, by name then id
    (the order approve_submission() lists them in)."""
    key = ingredient_key(name or "")
    found = [row for row in ingredients if row["_key"] == key]
    return sorted(found, key=lambda row: (row["name"], str(row["id"])))


def _rank(term: str, key: str) -> Optional[int]:
    """0 same key, 1 key starts with the term, 2 a word starts with it, 3 contains it."""
    if key == term:
        return 0
    if key.startswith(term):
        return 1
    if f" {term}" in f" {key}":
        return 2
    if term in key:
        return 3
    return None


def search(query: str, limit: int, ingredients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ingredients whose normalised name equals, starts with or contains the
    normalised query, best first; ties go to the shorter name.

    An alias is searched both ways: "aqua" looks for "water" (its canonical
    name) and for "aqua" itself. A result found only through the alias carries
    matched_alias = "aqua", so the page can say why it was offered.
    """
    alias = matched_alias(query)
    key = ingredient_key(query)
    plain = clean_ingredient_text(query)
    if not key.strip():
        return []
    scored = []
    for row in ingredients:
        by_key = _rank(key, row["_key"])
        by_plain = _rank(plain, row["_key"]) if alias else None
        ranks = [r for r in (by_key, by_plain) if r is not None]
        if not ranks:
            continue
        via_alias = alias is not None and by_key is not None and (by_plain is None or by_key < by_plain)
        scored.append((min(ranks), len(row["name"]), row["name"].lower(), row, via_alias))
    scored.sort(key=lambda s: s[:3])
    return [{"id": row["id"], "name": row["name"], "functional_group": row.get("functional_group"),
             "matched_alias": alias if via_alias else None}
            for *_, row, via_alias in scored[:limit]]


def match(names: List[str], ingredients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One result per input name, in input order. id is the one ingredient the
    name keys to, or null for "new". A name that keys to more than one row
    gets id null and ambiguous true: approval refuses to pick between them
    (SBAMB), so the matcher does not either."""
    results = []
    for raw in names:
        found = matches_for(raw, ingredients) if raw.strip() else []
        one = found[0] if len(found) == 1 else None
        results.append({
            "input": raw,
            "id": one["id"] if one else None,
            "name": one["name"] if one else None,
            "matched_alias": matched_alias(raw) if one else None,
            "ambiguous": len(found) > 1,
        })
    return results


def functional_groups() -> List[str]:
    """Every distinct ingredients.functional_group, exactly as stored, sorted.
    approve_submission() accepts a with_details functional_group only if it is
    one of these, compared as exact text."""
    def load():
        rows = fetch_all_rows(
            lambda: supabase.table("ingredients").select("id, functional_group").order("id"))
        return sorted({row["functional_group"] for row in rows
                       if isinstance(row.get("functional_group"), str) and row["functional_group"].strip()})
    return cache.get_or_load("functional_groups", load)
