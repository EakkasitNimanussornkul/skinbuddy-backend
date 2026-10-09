import re
import uuid
from typing import List, Dict, Any, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from postgrest.exceptions import APIError
from app.db.connection import supabase
from app.core.services.token import get_admin_user_id, get_current_user_id, get_optional_user_id
from app.core import cache
from app.core.services import image_upload
from app.core.services.rpc_errors import http_error_for_rpc
from app.schemas import ProductDetail, CompareResponse, SharedIngredient, BAUMANN_PATTERN, ProductPatch
from app.core.services.ingredientcheck_service import calculate_safety_flags  # 🌟 ADDED IMPORT
from app.core.services import compatibility_service
# compute_ingredient_similarity now lives in compatibility_service.py, not
# here - this module already imports compatibility_service above, and dupe
# detection (compatibility_service.find_shelf_duplicates) needed this same
# function, so defining it here and importing it back into compatibility_
# service would be a circular import. Re-imported under its original name so
# the two call sites below, and generate_test_record.py's GROUPS entry naming
# it, don't change.
from app.core.services.compatibility_service import compute_ingredient_similarity
from app.core.utils import create_slug

router = APIRouter()

# Every product fetch here nests the same ingredient data: the concerns that
# explain and grade skin-type flags, and the sources (migration 0009) behind
# both an ingredient's stored claims and each concern. One definition, so the
# routes cannot drift apart - a route missing ingredient_concerns once scored
# the same product differently from the others.
PRODUCT_INGREDIENTS_JOIN = (
    "product_ingredients(ingredients(*, "
    "ingredient_sources(claim, sources(*)), "
    "ingredient_concerns(*, concern_sources(sources(*)))))"
)

# A product row with its own sources (migration 0010: where its ingredient
# list, price, image or description was seen) and its ingredients as above.
PRODUCT_SELECT = "*, product_sources(claim, sources(*)), " + PRODUCT_INGREDIENTS_JOIN

# The same join cut down to what GET /products/search?view=card reads: the match
# score and its breakdown (name, good_for, bad_for, each ingredient's claim sources
# and each concern's target_profile/severity/concern_title; sources only by whether
# one exists) and the safety flags (name). No product_sources and no source bodies:
# the card does not return them. Measured at 46% of PRODUCT_SELECT's bytes (audit
# 2026-10-09 section 3). Add a column to scoring and this must follow; the live
# comparison of default against card scores is what proves they still agree.
CARD_PRODUCT_SELECT = (
    "*, product_ingredients(ingredients(name, good_for, bad_for, "
    "ingredient_sources(claim, sources(id)), "
    "ingredient_concerns(target_profile, severity, concern_title, concern_sources(sources(id)))))"
)

# Ingredients come back in the order printed on the pack (migration 0013's
# product_ingredients.position), with NULLs last. PostgREST orders an embedded
# resource through a query parameter (<embed>.order=position.asc.nullslast),
# not inside the select string, so every query that uses the join above goes
# through this helper. Products added before 0013 have NULL positions, so their
# order stays unspecified, as it always was.
INGREDIENT_ORDER_COLUMN = "position"


def in_pack_order(query, embed: str = "product_ingredients"):
    """Order the query's embedded product_ingredients rows by pack position.
    `embed` is the path to them: "products.product_ingredients" from shelf_items."""
    return query.order(INGREDIENT_ORDER_COLUMN, foreign_table=embed, nullsfirst=False)

def compute_product_display_fields(prod: dict, user_skin_type: str) -> dict:
    """Shared per-product enrichment (score/reasons/safety_flags) used by search, slug, and detail endpoints."""
    ings = [item["ingredients"] for item in prod.get("product_ingredients", []) if item.get("ingredients")]
    if user_skin_type:
        match_info = compute_baumann_compatibility(user_skin_type, ings)
        score, match_reasons, caution_reasons = match_info["score"], match_info["match_reasons"], match_info["caution_reasons"]
        breakdown = match_info["breakdown"]
    else:
        score, match_reasons, caution_reasons, breakdown = None, [], [], None
    return {
        "skin_match_score": score,
        "match_breakdown": breakdown,
        "match_reasons": match_reasons,
        "caution_reasons": caution_reasons,
        "safety_flags": calculate_safety_flags(prod.get("product_ingredients", [])),
    }

def ingredients_of(prod: dict) -> List[Dict[str, Any]]:
    """Flatten a product_ingredients(ingredients(...)) join into a plain list."""
    return [item["ingredients"] for item in prod.get("product_ingredients", []) or [] if item.get("ingredients")]


def postgrest_quote(value: str) -> str:
    """Quote a user-supplied value for use inside a PostgREST filter.

    `or_()` takes a COMMA-SEPARATED list of conditions, so an unescaped comma in
    the value ends one condition and begins another. That is not hypothetical:
    searching "Vitamin C, 10%" produced a malformed logic tree and a 500 from an
    entirely ordinary query (BE-DEF-06).

    PostgREST treats a double-quoted value as a single literal, with `"` and a
    backslash escaped by a backslash inside it. Quoting therefore neutralises
    `,` `.` `(` `)` and `:` in one step rather than stripping them, so the user
    keeps searching for what they typed.

    Deliberately does NOT escape the SQL LIKE wildcards `%` and `_`. They have
    always behaved as wildcards here, "niacinamide 10%" relies on it, and
    changing that is a search-semantics decision rather than part of fixing the
    crash.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _like_regex(pattern: str):
    """A SQL LIKE pattern as a compiled regex: % any run, _ any one character,
    backslash escapes the next character. Matched against lower-cased text, which
    is what ilike does."""
    out, i = [], 0
    pattern = pattern.lower()
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "%" else "." if c == "_" else re.escape(c))
        i += 1
    return re.compile("".join(out), re.S)


def filter_products(rows: List[dict], q: str, min_price: Optional[int], max_price: Optional[int]) -> List[dict]:
    """The rows the old database filter kept, in their order: `q` as
    name/brand/category ilike '%q%' (the % and _ wildcards and the backslash escape
    still apply, and PostgREST reads * as %), then the price bounds, a product with
    no price failing either. Done here so the unfiltered product tree can be cached."""
    if q:
        like = _like_regex(("%" + q.strip() + "%").replace("*", "%"))
        rows = [r for r in rows if any(
            r.get(col) is not None and like.fullmatch(str(r[col]).lower())
            for col in ("name", "brand", "category"))]
    if min_price is not None:
        rows = [r for r in rows if r.get("price_thb") is not None and r["price_thb"] >= min_price]
    if max_price is not None:
        rows = [r for r in rows if r.get("price_thb") is not None and r["price_thb"] <= max_price]
    return rows


# PostgREST's max-rows; the old query asked for 100. ponytail: one page; past 1000
# products the tree needs paging (and an order) like ingredient_lookup.fetch_all_rows.
PRODUCT_TREE_LIMIT = 1000

# The most products one search response carries: the old query's .limit(100), now the
# default page size and the largest limit a caller may ask for. More come by offset.
SEARCH_PAGE_MAX = 100


def load_product_tree(view: Optional[str]) -> List[dict]:
    """Every product with its ingredient tree, the same for every caller, so cached
    for 60 s (app.core.cache) per select: the default and the card one. Rows are
    shared between requests and must be treated as read-only. The database's own
    order is kept, as the old unordered query returned it."""
    select, name = (CARD_PRODUCT_SELECT, "card") if view == "card" else (PRODUCT_SELECT, "default")
    return cache.get_or_load(("product_tree", name), lambda: (
        in_pack_order(supabase.table("products").select(select)).limit(PRODUCT_TREE_LIMIT).execute().data or []))


# good_for is free text, not "(<letter>)" markers like bad_for, so each phrase
# the catalogue uses is mapped to the Baumann trait it describes. A phrase not
# listed here ("All Skin Types", "Rough Texture", "None", ...) describes no trait
# and counts for no one. Keys are lower-cased; test_baumann_scoring checks that
# every phrase the ingredient dictionary can write is either mapped or listed in
# NEUTRAL_GOOD_FOR, so a new phrase cannot silently count for nothing.
#
# Nothing in the catalogue describes the Resistant, Non-pigmented or Tight
# traits, so those letters are never helped. Dry skin is described far more
# often than oily (43 ingredients against 4, 2026-09-25), which the score
# reflects: it measures what the catalogue records, and that record is uneven.
GOOD_FOR_TRAITS = {
    "dry skin": "D", "extremely dry skin": "D", "dehydrated skin": "D",
    "damaged barriers": "D", "compromised barriers": "D", "eczema": "D",
    "oily skin": "O", "oily skin (in low concentration)": "O",
    "acne-prone skin": "O", "acne-prone": "O",
    "sensitive skin": "S", "irritated skin": "S", "redness-prone skin": "S",
    "acne-prone skin (redness)": "S",
    "pigmentation": "P", "dark spots": "P", "dull skin": "P",
    "aging skin": "W", "fine lines": "W",
}
NEUTRAL_GOOD_FOR = {"all skin types", "none", "rough texture", "environmentally-stressed skin"}

TRAIT_NAMES = {"D": "dry", "O": "oily", "S": "sensitive", "P": "pigmentation-prone", "W": "wrinkle-prone"}

# How much one flagged ingredient counts against a product, by its concern's
# grade. An unrecognised grade counts as High.
CONCERN_WEIGHT = {"High": 1.0, "Medium": 0.6, "Low": 0.3}

# Fewer ingredients than this saying anything about the code, and the score is
# marked limited: a 100% or 0% resting on one ingredient reads the same as one
# resting on twenty-four, and the page needs to be able to say which it is.
LIMITED_EVIDENCE_BELOW = 3


def _suited_traits(ingredient: Dict[str, Any], user_skin_type: str) -> List[str]:
    """The letters of this code the ingredient's good_for says it suits, in code order."""
    letters = {GOOD_FOR_TRAITS.get(part.strip().lower()) for part in (ingredient.get("good_for") or "").split(",")}
    return [letter for letter in user_skin_type if letter in letters]


def _list_names(names: List[str], shown: int = 3) -> str:
    """'A', 'A and B', 'A, B and C', 'A, B, C and 4 more'."""
    if len(names) > shown:
        return f"{', '.join(names[:shown])} and {len(names) - shown} more"
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def compute_baumann_compatibility(user_skin_type: str, ingredients: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How well a product's ingredients suit this Baumann code, as a percentage.

    Replaces a score that started at 75 and moved in fixed steps of 5 to 25 per
    rule, so it could only take a handful of values (60, 65, 75, 80, ...), and
    whose rules mostly named functional groups no ingredient carries.
    """
    # Validate the code rather than measure it. A length test passed anything
    # with four characters through to the axis checks below, which are
    # case-sensitive substring matches - so "dspt" matched no axis, adjusted
    # nothing, and returned a bare 75 that is indistinguishable from a valid
    # code scored against a product with no ingredient data. A short or absent
    # code took the other limb and returned 85, which is not a neutral default
    # but the top of the green band, above the 75 every real evaluation starts
    # from. Reusing the pattern POST /quiz/save and PATCH /auth/me validate
    # against makes the scorer agree with the writer about what a valid skin
    # type is. (FE-DEF-10)
    if not user_skin_type or not BAUMANN_PATTERN.match(user_skin_type):
        # No score rather than a default, and no reason either: the generic
        # reason asserted something about the product without reading its
        # ingredients, and an explanation for a verdict the UI is not showing
        # is worse than none. Every consumer already handles None - it is the
        # anonymous-caller shape this endpoint serves today.
        return {"score": None, "breakdown": None, "match_reasons": [], "caution_reasons": []}

    # Each ingredient is read for the traits of this code it suits (good_for)
    # and the ones it is flagged for (bad_for, explained and graded by
    # ingredient_concerns - the same reading the skin-type warnings use, so the
    # score and the warnings cannot disagree). One ingredient can do both:
    # niacinamide suits oily skin and is flagged for sensitive skin.
    helpful: Dict[str, List[str]] = {}          # trait letter -> ingredient names, in code order
    cautions: List[tuple] = []                  # (weight, sentence)
    helpful_count = 0
    concern_count = 0
    concern_weight = 0.0
    considered = 0
    verified_considered = 0
    for ing in ingredients:
        name = ing.get("name") or "An ingredient"
        claims_sourced = {link.get("claim") for link in (ing.get("ingredient_sources") or []) if link.get("sources")}
        suited = _suited_traits(ing, user_skin_type)
        if suited:
            helpful_count += 1
            for letter in suited:
                helpful.setdefault(letter, []).append(name)
        reasons = compatibility_service._skin_type_reasons(ing, user_skin_type)
        if reasons:
            worst = max(reasons, key=lambda r: compatibility_service._severity_rank(r.severity))
            weight = CONCERN_WEIGHT.get(worst.severity, CONCERN_WEIGHT["High"])
            concern_count += 1
            concern_weight += weight
            label = worst.title or f"flagged for {worst.trait}"
            cautions.append((weight, f"{name}: {label} ({worst.severity})"))
        if suited or reasons:
            considered += 1
            # Verified on every side it counts: a good_for source for the side
            # it helps; a bad_for source, or a source on the concern that
            # graded it, for the side it counts against.
            helpful_backed = not suited or "good_for" in claims_sourced
            concern_backed = not reasons or "bad_for" in claims_sourced or bool(worst.sources)
            if helpful_backed and concern_backed:
                verified_considered += 1

    # The working, returned beside the score so the page can show it: "Based
    # on 9 of 23 ingredients: 7 suit your skin, 2 are a concern for it".
    breakdown = {
        "helpful": helpful_count,
        "concerns": concern_count,
        "concern_weight": round(concern_weight, 2),
        "considered": considered,
        "total_ingredients": len(ingredients),
        "limited": considered < LIMITED_EVIDENCE_BELOW,
        "verified_considered": verified_considered,
    }

    # Nothing in the formula says anything about this skin type either way.
    # A number here would be a guess; None is what every consumer already
    # shows as "not scored". The breakdown still says why: 0 considered.
    if considered == 0:
        return {"score": None, "breakdown": breakdown, "match_reasons": [], "caution_reasons": []}

    # The plain share: helpful ingredients against weighted concerns. Not
    # smoothed - the owner chose a number whose making is visible over one
    # nudged away from 0% and 100%. So 100% means every ingredient that said
    # anything about this code was on the helpful side, however few there
    # were; `limited` in the breakdown is how a reader tells the difference.
    score = 100 * helpful_count / (helpful_count + concern_weight)

    # rstrip before the full stop: "Alcohol Denat." would otherwise end "Denat..".
    match_reasons = [
        f"Suits {TRAIT_NAMES[letter]} skin: {_list_names(names).rstrip('.')}."
        for letter in user_skin_type if (names := helpful.get(letter))
    ]
    # Most serious first. Stable, so equal weights keep ingredient order.
    caution_reasons = [sentence for _, sentence in sorted(cautions, key=lambda c: -c[0])]

    return {
        "score": round(score, 1),
        "breakdown": breakdown,
        "match_reasons": match_reasons,
        "caution_reasons": caution_reasons,
    }

async def resolve_product_record(identifier: str) -> dict or None:
    """Helper that finds a product whether passed a UUID, an exact slug, or a partial string."""
    clean_id = identifier.lower().strip()
    
    # 1. Check if identifier is a direct UUID
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', clean_id):
        # .limit(1), not .single(): the real client's .single() RAISES
        # APIError PGRST116 on zero rows, so the `if res.data` guard below was
        # unreachable for a well-formed UUID that matches no catalogue row. The
        # exception escaped this helper into its callers' generic clauses, and
        # GET /products/compare answered 400 for an unknown product id while
        # its test asserted 404 and passed - the fake used to return None here
        # rather than raising. Same defect as BE-DEF-07, missed in that sweep
        # because this helper resolves the row rather than the handlers do.
        res = in_pack_order(supabase.table("products").select(PRODUCT_SELECT)).eq("id", clean_id).limit(1).execute()
        if res.data:
            return res.data[0]

    # 1b. Indexed exact-slug lookup (fast path for the common case, avoids the
    # full-catalog fetch-and-loop below). Falls through if the products.slug
    # column/migration hasn't been applied yet or no exact match is found.
    try:
        slug_res = in_pack_order(supabase.table("products").select(PRODUCT_SELECT)).eq("slug", clean_id).limit(1).execute()
        if slug_res.data:
            return slug_res.data[0]
    except Exception:
        pass

    # 2. Check exact slug or normalized name matches across up to 2000 items. Only the
    # columns the matching reads are fetched (the full ingredient tree of every product
    # was the cost of an unknown slug, audit 2026-10-09 finding 4); the one product that
    # matches is then loaded whole, by id.
    clean_target = re.sub(r'[^a-z0-9]', '', clean_id)
    res = supabase.table("products").select("id, brand, name").limit(2000).execute()
    
    words = [w for w in clean_id.replace("-", " ").split() if len(w) > 2]
    
    for prod in (res.data or []):
        brand = prod.get("brand", "") or ""
        name = prod.get("name", "") or ""
        prod_slug = create_slug(brand, name)
        clean_prod = re.sub(r'[^a-z0-9]', '', prod_slug)
        clean_name = re.sub(r'[^a-z0-9]', '', name.lower())
        
        if clean_target in [clean_prod, clean_name] or clean_prod in clean_target or clean_target in clean_prod:
            return _load_full_record(prod["id"])

    # 3. Fuzzy keyword match fallback
    if words:
        for prod in (res.data or []):
            full_text = f"{prod.get('brand', '')} {prod.get('name', '')}".lower()
            if all(w in full_text for w in words[:3]):
                return _load_full_record(prod["id"])
    return None


def _load_full_record(product_id: str) -> Optional[dict]:
    """The product with its whole tree, as resolve_product_record returns it."""
    res = in_pack_order(supabase.table("products").select(PRODUCT_SELECT)).eq("id", product_id).limit(1).execute()
    return res.data[0] if res.data else None

@router.get("/slug/{slug}")
async def get_product_by_slug(
    slug: str, 
    user_id: Optional[str] = Depends(get_optional_user_id) # 🌟 Extracts logged-in user
):
    try:
        user_skin_type = ""
        if user_id:
            user_res = supabase.table("users").select("skin_type").eq("id", user_id).execute()
            if user_res.data and len(user_res.data) > 0:
                user_skin_type = user_res.data[0].get("skin_type", "")
        
        prod = await resolve_product_record(slug)
        if prod:
            # "Similar products" is presented to the user as products "matched
            # with similar active ingredient profiles", so it has to actually
            # compare ingredients. It previously returned whichever four rows
            # of the same category Supabase happened to hand back, with no
            # ingredient data fetched at all.
            #
            # The category filter stays: ranking raw ingredient overlap across
            # the whole catalogue would call a cleanser and a sunscreen similar
            # for sharing water and glycerin. Rank within the category instead.
            #
            # The limit moves after the sort — scoring requires every candidate.
            # Fine at this catalogue size; if a category ever grows into the
            # hundreds this needs database-side ranking, not a larger limit.
            cat = prod.get("category", "Moisturizer")
            base_ings = ingredients_of(prod)

            sim_res = supabase.table("products").select(
                "id, brand, name, image_url, price_thb, price_usd, product_ingredients(ingredients(*))"
            ).eq("category", cat).neq("id", prod["id"]).execute()

            candidates = []
            for sp in (sim_res.data or []):
                candidates.append({
                    "id": sp.get("id"),
                    "brand": sp.get("brand"),
                    "name": sp.get("name"),
                    "image_url": sp.get("image_url"),
                    "price_thb": sp.get("price_thb"),
                    "price_usd": sp.get("price_usd"),
                    "slug": create_slug(sp.get("brand", ""), sp.get("name", "")),
                    "ingredient_similarity": compute_ingredient_similarity(base_ings, ingredients_of(sp)),
                })

            # Ranks, does not filter: a zero-overlap product still appears
            # rather than leaving the widget short.
            candidates.sort(key=lambda c: c["ingredient_similarity"], reverse=True)
            similar_products = candidates[:4]

            return {
                **prod,
                **compute_product_display_fields(prod, user_skin_type),
                "slug": create_slug(prod.get("brand", ""), prod.get("name", "")),
                "similar_products": similar_products
            }

        raise HTTPException(status_code=404, detail=f"Product matching slug '{slug}' not found.")
    except HTTPException as he: raise he
    except Exception as e:
        print("GET /products/slug error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch product.")




@router.get("/search")
async def search_products(
    response: Response,
    q: str = "", 
    min_price: Optional[int] = None, 
    max_price: Optional[int] = None, 
    view: Optional[Literal["card"]] = None,
    limit: Optional[int] = Query(None, ge=1, le=SEARCH_PAGE_MAX),
    offset: int = Query(0, ge=0),
    user_id: Optional[str] = Depends(get_optional_user_id)
):
    """`view=card` returns each product without its ingredient rows and source trees
    (product_ingredients, product_sources) and adds ingredient_count; every other
    field, the score included, is what the default returns. An unknown view is a 422.

    Order: a name match first, then a brand match, then the rest (a category match);
    within each, the database's own order, which is not defined by any column.

    `limit` (1 to 100, default 100) and `offset` (default 0) cut a page out of that
    order after filtering and ranking. X-Total-Count is how many products match
    before the cut. Without them the response is what it always was."""
    try:
        user_skin_type = ""
        if user_id:
            # .limit(1), not .single(): an authenticated caller whose users row
            # is missing raised PGRST116 here, and the generic clause below
            # reported it as a 500 for the whole catalogue search. A missing
            # profile means "no skin type to personalise with", which is the
            # empty string this already falls back to. BE-DEF-07's class.
            user_res = supabase.table("users").select("skin_type").eq("id", user_id).limit(1).execute()
            user_skin_type = user_res.data[0].get("skin_type", "") if user_res.data else ""

        matching = filter_products(load_product_tree(view), q, min_price, max_price)
        lower_q = q.lower().strip()

        def rank_of(prod):
            if lower_q and lower_q in (prod.get("name") or "").lower(): return 1
            if lower_q and lower_q in (prod.get("brand") or "").lower(): return 2
            return 3

        # Ranked before the page is cut, so a page is a slice of the whole order and
        # offsets mean the same thing on every call. sorted() is stable.
        ranked = sorted(matching, key=rank_of)
        response.headers["X-Total-Count"] = str(len(ranked))
        page = ranked[offset:offset + (limit or SEARCH_PAGE_MAX)]

        enriched_products = []
        for prod in page:
            display_fields = compute_product_display_fields(prod, user_skin_type)
            ings = [item["ingredients"] for item in prod.get("product_ingredients", []) if item.get("ingredients")]
            preview_names = [ing["name"] for ing in ings[:3]]
            prod_slug = create_slug(prod.get("brand", ""), prod.get("name", ""))

            item = {
                **prod,
                **display_fields,
                "has_conflict": len(display_fields["caution_reasons"]) > 0,
                "top_ingredients": preview_names,
                "slug": prod_slug,
            }
            if view == "card":
                item.pop("product_ingredients", None)
                item.pop("product_sources", None)
                item["ingredient_count"] = len(ings)
            enriched_products.append(item)

        return enriched_products
    except Exception as e:
        print("GET /products/search error:", e)
        raise HTTPException(status_code=500, detail="Failed to search products.")

@router.get("/compare", response_model=CompareResponse)
async def compare_two_products(
    product_a_id: str, 
    product_b_id: str,
    user_id: Optional[str] = Depends(get_optional_user_id) # 🌟 ADDED DEPENDENCY
):
    try:
        user_skin_type = ""
        if user_id:
            user_res = supabase.table("users").select("skin_type").eq("id", user_id).execute()
            if user_res.data and len(user_res.data) > 0:
                user_skin_type = user_res.data[0].get("skin_type", "")

        prod_a = await resolve_product_record(product_a_id)
        prod_b = await resolve_product_record(product_b_id)

        if not prod_a or not prod_b:
            raise HTTPException(status_code=404, detail="One or both products could not be resolved.")

        # 🌟 COMPUTE compatibility scores for both Product A & Product B
        ings_a = [item["ingredients"] for item in prod_a.get("product_ingredients", []) if item.get("ingredients")]
        ings_b = [item["ingredients"] for item in prod_b.get("product_ingredients", []) if item.get("ingredients")]

        if user_skin_type:
            match_a = compute_baumann_compatibility(user_skin_type, ings_a)
            match_b = compute_baumann_compatibility(user_skin_type, ings_b)
            prod_a["skin_match_score"] = match_a.get("score")
            prod_b["skin_match_score"] = match_b.get("score")
            prod_a["match_breakdown"] = match_a.get("breakdown")
            prod_b["match_breakdown"] = match_b.get("breakdown")
        else:
            prod_a["skin_match_score"] = None
            prod_b["skin_match_score"] = None

        prod_a["safety_flags"] = calculate_safety_flags(prod_a.get("product_ingredients", []))
        prod_b["safety_flags"] = calculate_safety_flags(prod_b.get("product_ingredients", []))

        dict_a = {item["ingredients"]["id"]: item["ingredients"] for item in prod_a.get("product_ingredients", []) if item.get("ingredients")}
        dict_b = {item["ingredients"]["id"]: item["ingredients"] for item in prod_b.get("product_ingredients", []) if item.get("ingredients")}

        set_a = set(dict_a.keys())
        set_b = set(dict_b.keys())

        shared_ids = set_a.intersection(set_b)
        shared_ingredients = [
            SharedIngredient(id=i, name=dict_a[i]["name"], benefits=dict_a[i].get("benefits")) 
            for i in shared_ids
        ]

        # Same definition of "similar" the slug endpoint ranks by. dict_a/dict_b
        # stay above: shared_ingredients reads names and benefits out of them.
        similarity = compute_ingredient_similarity(list(dict_a.values()), list(dict_b.values()))

        # Full 3-pass check (skin-type + ingredient-pair + category rules), reusing the
        # same engine Shelf/Routine use instead of a partial category-only duplicate.
        # A single analyze(target=A, comparison=[B]) call already captures every A<->B
        # pairwise/category clash in both rule orderings; the second call (empty
        # comparison) only adds B's own skin-type warnings, so nothing double-counts.
        result_a = compatibility_service.analyze(prod_a["id"], user_id, [compatibility_service.normalize_product(prod_b)])
        result_b = compatibility_service.analyze(prod_b["id"], user_id, [])
        # One card per clashing product, as on the shelf. Every pairwise clash
        # names product B, so this collapses them into one; result_b holds only
        # skin-type alerts, which pass through as they are.
        conflicts = compatibility_service.group_warnings_by_product(result_a.warnings + result_b.warnings)

        return CompareResponse(
            product_a=prod_a,
            product_b=prod_b,
            shared_ingredients=shared_ingredients,
            similarity_score=similarity,  # already rounded by compute_ingredient_similarity
            conflicts=conflicts
        )
    except HTTPException as he: raise he
    except Exception as e:
        print("GET /products/compare error:", e)
        raise HTTPException(status_code=400, detail="Failed to compare products.")

def load_product_detail(product_id: str, user_id: Optional[str]) -> Optional[dict]:
    """GET /products/{id}'s body, or None when no product has this id. PATCH
    /products/{id} answers with the same shape."""
    # .limit(1) rather than .single(): the real client's .single() RAISES on
    # zero rows (PGRST116), which the generic handler turned into a 500 for a
    # product that simply does not exist. The sibling resolvers
    # (get_product_by_slug, compare_two_products) already read a list for the
    # same reason. BE-DEF-07.
    user_skin_type = ""
    if user_id:
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).limit(1).execute()
        if user_res.data:
            user_skin_type = user_res.data[0].get("skin_type") or ""

    # ingredient_concerns grades the concerns the match score weighs; every
    # other product fetch here already selects it.
    res = in_pack_order(supabase.table("products").select(PRODUCT_SELECT)).eq("id", product_id).limit(1).execute()
    if not res.data:
        return None

    data = res.data[0]
    data.update(compute_product_display_fields(data, user_skin_type))
    return data


@router.get("/{product_id}")
async def get_product_detail(product_id: str, user_id: Optional[str] = Depends(get_optional_user_id)):
    try:
        data = load_product_detail(product_id, user_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found.")
        return data
    except HTTPException:
        # Ahead of the generic clause below, which catches HTTPException too and
        # would re-raise the 404 above as a 500 - the trap BE-DEF-02 describes.
        raise
    except Exception as e:
        print("GET /products/{id} error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch product.")


def _require_product_uuid(product_id: str) -> str:
    """A malformed id is a 404, not a Postgres type error answered as 500."""
    try:
        return str(uuid.UUID(product_id))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found.")


def _current_image_url(product_id: str) -> Optional[str]:
    """The product's image_url before an edit, so the photo it replaces can be
    deleted afterwards. None if it cannot be read: the edit goes ahead, and
    deleting the old photo is left to the cleanup route."""
    try:
        res = supabase.table("products").select("image_url").eq("id", product_id).limit(1).execute()
        return res.data[0].get("image_url") if res.data else None
    except Exception as e:
        print("PATCH /products/{id}: could not read the current image_url:", e)
        return None


@router.patch("/{product_id}")
async def update_product(product_id: str, body: ProductPatch, admin_id: str = Depends(get_admin_user_id)):
    """Admin edit of any product: admin_update_product() in migration 0013, one
    transaction. Only the fields sent change; ingredients and sources, when
    sent, replace the product's whole list. 409 {"detail": "stale"} when
    updated_at is not the product's current value, so a second admin's save is
    never silently overwritten.

    A brand or name change regenerates the slug. The old slug is not kept and
    does not resolve afterwards (owner's decision, 2026-10-04): the response
    carries the new one.
    """
    try:
        product_id = _require_product_uuid(product_id)
        patch = body.patch_fields()
        old_image_url = None
        if "image_path" in patch:
            path = patch.pop("image_path")
            patch["image_url"] = image_upload.public_url(path) if path else None
            old_image_url = _current_image_url(product_id)
        try:
            res = supabase.rpc("admin_update_product", {
                "p_product_id": product_id,
                "p_expected_updated_at": body.updated_at,
                "p_patch": patch,
            }).execute()
        except APIError as err:
            raise http_error_for_rpc(err, "PATCH /products/{id}")
        finally:
            # The edit may have committed even when the answer was an error, so
            # the cached product tree and ingredient list go either way.
            cache.clear()
        # The photo replaced or cleared is deleted once the change is saved, but
        # only if it was one of our uploads (never a seed or catalogue image or
        # an external URL) and nothing else still uses it: another product, or a
        # pending submission. The approved submission this product came from
        # no longer keeps it, now that the product no longer shows it (owner's
        # decision, 2026-10-06). A failure is printed; the PATCH answers 200.
        if "image_url" in patch and old_image_url and old_image_url != patch["image_url"]:
            old_path = image_upload.upload_path_of_url(old_image_url)
            if old_path:
                image_upload.delete_upload_if_unused(old_path, "PATCH /products/{id}")
        saved = res.data or {}
        detail = load_product_detail(product_id, admin_id) or {}
        return {**detail, "id": product_id, "slug": saved.get("slug"), "updated_at": saved.get("updated_at")}
    except HTTPException:
        raise
    except Exception as e:
        print("PATCH /products/{id} error:", e)
        raise HTTPException(status_code=500, detail="Failed to update the product.")


@router.post("/{product_id}/image", openapi_extra=image_upload.UPLOAD_OPENAPI)
async def upload_product_image(product_id: str, request: Request, admin_id: str = Depends(get_admin_user_id)):
    """Stores a new image for the product and returns its image_path, for a
    following PATCH /products/{id}. The product itself is not changed here."""
    product_id = _require_product_uuid(product_id)
    data, ext, content_type = await image_upload.read_image_upload(request)
    try:
        found = supabase.table("products").select("id").eq("id", product_id).limit(1).execute()
        if not found.data:
            raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found.")
        return image_upload.store_image(data, ext, content_type, "products")
    except HTTPException:
        raise
    except Exception as e:
        print("POST /products/{id}/image error:", e)
        raise HTTPException(status_code=500, detail="Failed to store the image.")
