"""Reading product submissions for the review screens.

A submission's payload is what the user sent; edited_payload holds the admin's
corrections. approve_submission() (migration 0013) approves
payload || edited_payload, a top-level merge where a key in edited_payload
replaces the same key in payload. merged_payload() merges the same way, so the
admin approves exactly what the review screen showed.

One legacy row exists live in the old shape (ingredients as a list of plain
names). It is read without crashing: each name is shown as a new ingredient.
Approving it as it is answers 422 (SBLEG) until an admin saves it in the new shape.
"""

import difflib
import re
from typing import Any, Dict, List

from app.core.services.ingredient_lookup import fetch_all_rows, matches_for
from app.core.utils import create_slug
from app.db.connection import supabase

# Names at least this similar (difflib ratio, 0 to 1) under the same brand are
# offered as possible duplicates. Only an exact brand+name or slug match blocks
# approval; a close one is for the admin to judge.
CLOSE_NAME_RATIO = 0.8


def merged_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    edited = row.get("edited_payload") if isinstance(row.get("edited_payload"), dict) else {}
    return {**payload, **edited}


def ingredient_list(payload: Dict[str, Any]) -> List[Any]:
    items = payload.get("ingredients")
    return items if isinstance(items, list) else []


def _is_new(item: Any) -> bool:
    return isinstance(item, str) or (isinstance(item, dict) and not item.get("ingredient_id"))


def review_ingredients(payload: Dict[str, Any], ingredients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The merged payload's ingredients for the review screen, numbered from 0
    in list order: the position approve_submission() expects in a decision.

    existing_matches lists the ingredients rows a new name keys to: none means
    approving inserts it, one means approving links that row instead, more than
    one means approving refuses (SBAMB) until the admin picks.
    """
    by_id = {str(row["id"]): row for row in ingredients}
    rows = []
    for position, item in enumerate(ingredient_list(payload)):
        if isinstance(item, dict) and item.get("ingredient_id"):
            known = by_id.get(str(item["ingredient_id"]).lower())
            rows.append({"position": position, "ingredient_id": str(item["ingredient_id"]),
                         "name": known["name"] if known else None, "status": "known",
                         "details": None, "existing_matches": []})
            continue
        if isinstance(item, str):                       # the legacy shape
            name, details = item.strip(), None
        elif isinstance(item, dict):
            name = (item.get("new_name") or "").strip() or None
            details = item.get("details") if isinstance(item.get("details"), dict) else None
        else:
            name, details = None, None
        found = matches_for(name, ingredients) if name else []
        rows.append({"position": position, "ingredient_id": None, "name": name, "status": "new",
                     "details": details,
                     "existing_matches": [{"id": m["id"], "name": m["name"]} for m in found]})
    return rows


def load_products() -> List[Dict[str, Any]]:
    return fetch_all_rows(lambda: supabase.table("products").select("id, slug, brand, name").order("id"))


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def duplicate_candidates(brand: Any, name: Any, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Products that may be the one submitted. exact is true for a
    case-insensitive brand+name match or the same slug, which approve refuses
    with 409; false for a close name under the same brand, which it allows."""
    brand = brand.strip() if isinstance(brand, str) else ""
    name = name.strip() if isinstance(name, str) else ""
    if not brand or not name:
        return []
    slug = create_slug(brand, name)
    found = []
    for p in products:
        p_brand, p_name = p.get("brand") or "", p.get("name") or ""
        exact = (p_brand.lower() == brand.lower() and p_name.lower() == name.lower()) or p.get("slug") == slug
        close = (not exact and p_brand.strip().lower() == brand.lower()
                 and difflib.SequenceMatcher(None, _name_key(p_name), _name_key(name)).ratio() >= CLOSE_NAME_RATIO)
        if exact or close:
            found.append({"id": p["id"], "slug": p.get("slug"), "brand": p_brand, "name": p_name, "exact": exact})
    found.sort(key=lambda c: (not c["exact"], c["brand"], c["name"]))
    return found


def summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": payload.get("name"), "brand": payload.get("brand"),
            "category": payload.get("category"), "ingredient_count": len(ingredient_list(payload))}


def flags(payload: Dict[str, Any], products: List[Dict[str, Any]]) -> Dict[str, Any]:
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    return {
        "possible_duplicate": bool(duplicate_candidates(payload.get("brand"), payload.get("name"), products)),
        "new_ingredient_count": sum(1 for item in ingredient_list(payload) if _is_new(item)),
        "has_source": bool(sources),
        "has_photo": bool(payload.get("image_path")),
    }


def submitter_name(row: Dict[str, Any]):
    submitter = row.get("submitter")
    return submitter.get("display_name") if isinstance(submitter, dict) else None
