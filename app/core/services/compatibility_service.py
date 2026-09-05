import unicodedata
from typing import Any, List, Dict, Optional

from app.db.connection import supabase
from app.schemas import AnalysisResponse, DuplicateMatch, WarningAlert

# Functional groups that describe filler, not what a product actually does.
# Plain ingredient-overlap similarity is useless for dupe detection without
# this: water, glycerin and phenoxyethanol are in almost everything, so every
# moisturiser would read as a dupe of every other moisturiser. Mirrors the
# exclusion list already used by the frontend's KeyActivesGrid.vue, moved here
# so both halves agree on one definition of "active".
NON_ACTIVE_INGREDIENT_GROUPS = {"formulation stabilizer", "solvent", "vehicle"}

# Calibrated 2026-08-20 against the seeded catalogue (app/db/seeders/catalog.json,
# 7 products): the only category with more than one product is Treatments (3
# products), whose 3 same-category pairs score 0.0, 0.0 and 16.7. Nothing
# crosses this threshold, which is the correct outcome for a curated catalogue
# of deliberately distinct hero products, not evidence the threshold is too
# high. Re-check this once the catalogue has enough same-category near-repeats
# to give the threshold a real positive example to test against.
DUPE_SIMILARITY_THRESHOLD = 60.0


def normalize_text_accents(text: str) -> str:
    """Transforms characters like Céramide into Ceramide to match core conflict engine constraints."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).strip().lower()


def normalize_product(prod: dict) -> dict:
    """Flatten a Supabase products(...) join into { name, ingredients:[...] },
    plus id/brand/category/slug when the caller's select included them.

    The extra keys are additive: dupe detection needs product identity (to
    link back to it, exclude it from matching itself, and apply the
    same-category constraint), but every existing caller - analyze(), via
    _build_comparison_maps() - only ever reads name/ingredients, so this
    changes nothing for them. A select that didn't fetch id/brand/category/
    slug simply yields None for those keys, same as today.
    """
    ingredients = []
    for pi in prod.get("product_ingredients", []) or []:
        ing = pi.get("ingredients")
        if ing:
            ingredients.append(ing)
    return {
        "id": prod.get("id"),
        "brand": prod.get("brand"),
        "name": prod.get("name"),
        "category": prod.get("category"),
        "slug": prod.get("slug"),
        "ingredients": ingredients,
    }


def compute_ingredient_similarity(ings_a: List[Dict[str, Any]], ings_b: List[Dict[str, Any]]) -> float:
    """Jaccard similarity (0-100) over two products' ingredient ID sets.

    Lives here rather than in app/api/products.py (which is where it was
    originally written) because products.py imports this module - defining it
    there and importing it back from here would be a circular import.
    products.py imports it from here instead; see its compute_ingredient_similarity
    re-export.
    """
    ids_a = {ing["id"] for ing in ings_a if ing.get("id")}
    ids_b = {ing["id"] for ing in ings_b if ing.get("id")}
    union = ids_a | ids_b
    if not union:
        return 0.0
    return round((len(ids_a & ids_b) / len(union)) * 100, 1)


def filter_active_ingredients(ingredients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop filler ingredients so similarity reflects what a product actually does.

    An ingredient with no functional_group is also excluded, not treated as an
    active. ingredients.source records that many entries were guessed
    heuristically from the name rather than curated (see
    DOCUMENTATION_QUALITY_BRIEF.md) - an ungrouped ingredient is one the
    system knows least about, and counting it as an active would inflate
    similarity on the weakest data. This is the conservative choice, made
    deliberately rather than left as an accident of the filter's shape.
    """
    out = []
    for ing in ingredients:
        group = (ing.get("functional_group") or "").strip().lower()
        if group and group not in NON_ACTIVE_INGREDIENT_GROUPS:
            out.append(ing)
    return out


def get_active_shelf_products(user_id: str) -> List[dict]:
    """Comparison set for the Shelf feature: the user's active (opened) products."""
    res = (
        supabase.table("shelf_items")
        .select("products(name, product_ingredients(ingredients(id, name, functional_group)))")
        .eq("user_id", user_id)
        .eq("usage_state", "active")
        .execute()
    )
    out = []
    for item in (res.data or []):
        prod = item.get("products")
        if prod:
            out.append(normalize_product(prod))
    return out


def get_shelf_products_for_dupe_check(user_id: str) -> List[dict]:
    """Comparison set for dupe detection: everything the user owns except what
    they've archived.

    Deliberately different from get_active_shelf_products(), which is
    active-only (opened products, for conflict analysis - only an opened
    product can clash with something else in a routine). An unopened backup is
    still something already owned, and buying a third of it is exactly what
    dupe detection should catch, so it stays in scope here. A sibling function
    rather than a parameter on get_active_shelf_products(), so the existing
    active-only behaviour analyze() depends on cannot be changed by accident.
    """
    res = (
        supabase.table("shelf_items")
        .select(
            "products(id, brand, name, category, slug, "
            "product_ingredients(ingredients(id, name, functional_group)))"
        )
        .eq("user_id", user_id)
        .neq("usage_state", "archived")
        .execute()
    )
    out = []
    for item in (res.data or []):
        prod = item.get("products")
        if prod:
            out.append(normalize_product(prod))
    return out


def find_shelf_duplicates(
    target_id: str,
    target_ingredients: List[Dict[str, Any]],
    target_category: Optional[str],
    shelf_products: List[dict],
) -> List[DuplicateMatch]:
    """Shelf products whose active ingredients substantially overlap the target's.

    Advisory only. The caller must never fold this into `warnings` or let it
    affect `is_safe` - see the AnalysisResponse.duplicates field docstring.

    Restricted to the same category: two products from different categories
    sharing an active (a cleanser and a serum both containing salicylic acid,
    say) is not a redundant purchase - it's a possible over-exfoliation risk,
    and analyze()'s category_conflict_rules pass already covers that. Keeping
    the category constraint stops this feature from duplicating a warning the
    system already gives elsewhere.
    """
    target_actives = filter_active_ingredients(target_ingredients)
    if not target_actives:
        return []

    matches: List[DuplicateMatch] = []
    for prod in shelf_products:
        if not prod.get("id") or not prod.get("name"):
            continue
        if prod["id"] == target_id:
            continue  # never report a product as its own dupe
        if target_category and prod.get("category") != target_category:
            continue

        prod_actives = filter_active_ingredients(prod.get("ingredients", []))
        if not prod_actives:
            continue

        score = compute_ingredient_similarity(target_actives, prod_actives)
        if score < DUPE_SIMILARITY_THRESHOLD:
            continue

        shared = sorted(
            {i["name"] for i in target_actives if i.get("name")}
            & {i["name"] for i in prod_actives if i.get("name")}
        )
        matches.append(DuplicateMatch(
            product_id=prod["id"],
            name=prod["name"],
            brand=prod.get("brand"),
            slug=prod.get("slug"),
            similarity=score,
            shared_actives=shared,
        ))

    return sorted(matches, key=lambda m: m.similarity, reverse=True)


def get_routine_products(user_id: str, exclude_product_id: Optional[str] = None) -> List[dict]:
    """Comparison set for the Routine feature (UC-17): items in the active routine."""
    routine = (
        supabase.table("routines")
        .select("id")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not routine.data:
        return []
    routine_id = routine.data[0]["id"]
    res = (
        supabase.table("routine_steps")
        .select("product_id, products(name, product_ingredients(ingredients(id, name, functional_group)))")
        .eq("routine_id", routine_id)
        .execute()
    )
    out = []
    for item in (res.data or []):
        if exclude_product_id and item.get("product_id") == exclude_product_id:
            continue
        prod = item.get("products")
        if prod:
            out.append(normalize_product(prod))
    return out


def _build_comparison_maps(products: List[dict]):
    """Return (functional_group -> [(product, ingredient)], ingredient_id -> product)."""
    groups: Dict[str, list] = {}
    ingredient_ids: Dict[str, str] = {}
    for prod in products:
        prod_name = prod.get("name")
        for ing in prod.get("ingredients", []):
            ingredient_ids[ing["id"]] = prod_name
            fg = ing.get("functional_group")
            if fg:
                norm_fg = normalize_text_accents(fg)
                groups.setdefault(norm_fg, []).append((prod_name, ing["name"]))
    return groups, ingredient_ids


# --- Core analysis -----------------------------------------------------------

def analyze(product_id: str, user_id: Optional[str], comparison_products: List[dict]) -> AnalysisResponse:
    """Run the full compatibility analysis of a target product against a set.

    Checks performed:
      1. Baumann skin-type direct conflicts (target ingredient bad_for user type).
      2. Ingredient-to-ingredient rules (conflict_rules table).
      3. Functional-group / category rules (category_conflict_rules table).
    """
    warnings: List[WarningAlert] = []

    # 1. User Baumann skin type (anonymous callers skip this check)
    #
    # .limit(1) rather than .single() throughout: the real client's .single()
    # RAISES on zero rows (PGRST116), so an unknown user or product turned into
    # a 500 at whichever handler called this. Reading a list lets a missing row
    # be the empty, meaningful answer it already is below. BE-DEF-07.
    user_skin_type = None
    if user_id:
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).limit(1).execute()
        user_skin_type = user_res.data[0].get("skin_type") if user_res.data else None

    # 2. Target product metadata
    target_res = (
        supabase.table("products")
        .select("*, product_ingredients(ingredients(*))")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    target_data = target_res.data[0] if target_res.data else None

    target_ingredient_ids = []
    target_groups: Dict[str, list] = {}
    target_ing_id_to_name: Dict[str, str] = {}

    if target_data and target_data.get("product_ingredients"):
        for item in target_data["product_ingredients"]:
            ing = item.get("ingredients")
            if ing:
                target_ingredient_ids.append(ing["id"])
                target_ing_id_to_name[ing["id"]] = ing["name"]
                fg = ing.get("functional_group")
                if fg:
                    target_groups.setdefault(normalize_text_accents(fg), []).append(ing["name"])

    # --- Skin type direct conflicts ---
    if user_skin_type and target_data:
        for item in target_data["product_ingredients"]:
            ing = item.get("ingredients")
            if ing:
                bad_for_str = ing.get("bad_for")
                if bad_for_str and any(f"({letter})" in bad_for_str for letter in user_skin_type):
                    warnings.append(WarningAlert(
                        alert_type="Skin Type Conflict",
                        severity="High",
                        message=f"Personalized Alert: {ing['name']} is known to trigger adverse reactions for Baumann Type {user_skin_type}.",
                    ))

    comparison_groups, comparison_ingredient_ids = _build_comparison_maps(comparison_products)

    # Nothing to compare against -> only skin-type warnings (if any) apply.
    if not comparison_ingredient_ids and not comparison_groups:
        return AnalysisResponse(is_safe=len(warnings) == 0, warnings=warnings)

    # --- PASS 1: ingredient-to-ingredient (conflict_rules) ---
    if target_ingredient_ids and comparison_ingredient_ids:
        specific_rules = supabase.table("conflict_rules").select("*").execute()
        for rule in (specific_rules.data or []):
            id_a = rule.get("ingredient_a_id")
            id_b = rule.get("ingredient_b_id")
            if id_a in target_ingredient_ids and id_b in comparison_ingredient_ids:
                clashing_product = comparison_ingredient_ids[id_b]
                warnings.append(WarningAlert(
                    alert_type="Chemical Interaction Warning",
                    severity=rule["severity"].title(),
                    message=f"Conflict with {clashing_product}: Layering {target_ing_id_to_name[id_a]} directly alongside it triggers a structural clash. {rule['warning_message']}",
                ))
            elif id_b in target_ingredient_ids and id_a in comparison_ingredient_ids:
                clashing_product = comparison_ingredient_ids[id_a]
                warnings.append(WarningAlert(
                    alert_type="Chemical Interaction Warning",
                    severity=rule["severity"].title(),
                    message=f"Conflict with {clashing_product}: Layering {target_ing_id_to_name[id_b]} directly alongside it triggers a structural clash. {rule['warning_message']}",
                ))

    # --- PASS 2: functional-group / category (category_conflict_rules) ---
    if target_groups and comparison_groups:
        rules_res = supabase.table("category_conflict_rules").select("*").execute()
        for rule in (rules_res.data or []):
            rule_a = normalize_text_accents(rule.get("group_a"))
            rule_b = normalize_text_accents(rule.get("group_b"))

            if rule_a in target_groups and rule_b in comparison_groups:
                target_ing_names = ", ".join(target_groups[rule_a])
                for prod_name, comp_ing_name in comparison_groups[rule_b]:
                    warnings.append(WarningAlert(
                        alert_type="Active Routine Clash",
                        severity=rule["severity"].title(),
                        message=f"Category Conflict with {prod_name}: Combining {target_ing_names} with {comp_ing_name} is unadvised. {rule['warning_message']}",
                    ))
            elif rule_b in target_groups and rule_a in comparison_groups:
                target_ing_names = ", ".join(target_groups[rule_b])
                for prod_name, comp_ing_name in comparison_groups[rule_a]:
                    warnings.append(WarningAlert(
                        alert_type="Active Routine Clash",
                        severity=rule["severity"].title(),
                        message=f"Category Conflict with {prod_name}: Combining {target_ing_names} with {comp_ing_name} is unadvised. {rule['warning_message']}",
                    ))

    return AnalysisResponse(is_safe=len(warnings) == 0, warnings=warnings)
