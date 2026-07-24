import unicodedata
from typing import List, Dict, Optional

from app.db.connection import supabase
from app.schemas import AnalysisResponse, WarningAlert


def normalize_text_accents(text: str) -> str:
    """Transforms characters like Céramide into Ceramide to match core conflict engine constraints."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).strip().lower()


def _normalize_product(prod: dict) -> dict:
    """Flatten a Supabase products(...) join into { name, ingredients:[...] }."""
    ingredients = []
    for pi in prod.get("product_ingredients", []) or []:
        ing = pi.get("ingredients")
        if ing:
            ingredients.append(ing)
    return {"name": prod.get("name"), "ingredients": ingredients}


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
            out.append(_normalize_product(prod))
    return out


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
            out.append(_normalize_product(prod))
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

def analyze(product_id: str, user_id: str, comparison_products: List[dict]) -> AnalysisResponse:
    """Run the full compatibility analysis of a target product against a set.

    Checks performed:
      1. Baumann skin-type direct conflicts (target ingredient bad_for user type).
      2. Ingredient-to-ingredient rules (conflict_rules table).
      3. Functional-group / category rules (category_conflict_rules table).
    """
    warnings: List[WarningAlert] = []

    # 1. User Baumann skin type
    user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
    user_skin_type = user_res.data.get("skin_type") if user_res.data else None

    # 2. Target product metadata
    target_res = (
        supabase.table("products")
        .select("*, product_ingredients(ingredients(*))")
        .eq("id", product_id)
        .single()
        .execute()
    )

    target_ingredient_ids = []
    target_groups: Dict[str, list] = {}
    target_ing_id_to_name: Dict[str, str] = {}

    if target_res.data and target_res.data.get("product_ingredients"):
        for item in target_res.data["product_ingredients"]:
            ing = item.get("ingredients")
            if ing:
                target_ingredient_ids.append(ing["id"])
                target_ing_id_to_name[ing["id"]] = ing["name"]
                fg = ing.get("functional_group")
                if fg:
                    target_groups.setdefault(normalize_text_accents(fg), []).append(ing["name"])

    # --- Skin type direct conflicts ---
    if user_skin_type and target_res.data:
        for item in target_res.data["product_ingredients"]:
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
