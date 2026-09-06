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
    """Return (functional_group -> [(product, ingredient)], ingredient_id -> [products]).

    The comparison set is the user's shelf rows, and a shelf holds rows, not
    distinct products: nothing stops the same product being added twice, and
    people do it (a backup bottle, a re-purchase logged before the old one was
    archived). Each row carries the same ingredients, so an unguarded list here
    held one (product, ingredient) pair per row, and PASS 2 below appends one
    warning per pair - three bottles of the same serum produced the same
    warning three times, and inflated the "N Warnings" count on the card by the
    same factor.

    A conflict is a fact about a product, not about how many of it someone
    owns. Pairs are de-duplicated so it is stated once.

    De-duplicated on the pair, not on the product: two *different* products that
    each clash still produce a warning each, naming themselves, which is
    correct - the user needs to know about both. Insertion order is preserved
    so warning order stays stable between runs.

    The second map holds a *list* of product names per ingredient, for the same
    reason the first holds a list of pairs. It was a plain dict of one name, so
    when two different shelf products both contained the clashing ingredient -
    a vitamin C serum and a vitamin C moisturiser - the second assignment
    overwrote the first and PASS 1 reported only whichever row Postgres happened
    to return last. get_active_shelf_products has no ORDER BY, so which one
    survived was not even stable between calls. The other conflict was never
    reported and nothing marked it as omitted. BE-DEF-12.

    Duplicate rows of the *same* product are collapsed here too, so widening
    this to a list cannot undo BE-DEF-11 by another route.
    """
    groups: Dict[str, list] = {}
    seen_pairs: Dict[str, set] = {}
    ingredient_ids: Dict[str, List[str]] = {}
    for prod in products:
        prod_name = prod.get("name")
        for ing in prod.get("ingredients", []):
            owners = ingredient_ids.setdefault(ing["id"], [])
            if prod_name not in owners:
                owners.append(prod_name)
            fg = ing.get("functional_group")
            if fg:
                norm_fg = normalize_text_accents(fg)
                pair = (prod_name, ing["name"])
                if pair in seen_pairs.setdefault(norm_fg, set()):
                    continue
                seen_pairs[norm_fg].add(pair)
                groups.setdefault(norm_fg, []).append(pair)
    return groups, ingredient_ids


def _dedupe_warnings(warnings: List[WarningAlert]) -> List[WarningAlert]:
    """Collapse warnings that say exactly the same thing, keeping the first.

    The pair de-duplication above stops the shelf multiplying PASS 2 warnings at
    the source. This is the guarantee at the boundary: whatever the passes
    produce, the caller never receives the same sentence twice. It also covers
    the skin-type pass, which walks the target product's own ingredient rows and
    would repeat itself if a product were ever joined to the same ingredient
    more than once.

    Identity is the whole alert - type, severity and message. Two warnings that
    differ only by which product they name are different warnings and both
    survive.
    """
    seen = set()
    unique: List[WarningAlert] = []
    for warning in warnings:
        key = (warning.alert_type, warning.severity, warning.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(warning)
    return unique


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _severity_rank(severity: Optional[str]) -> int:
    """Order the three severities the rule tables use, for comparing two rules.

    An unrecognised or missing severity ranks 0, which suppresses nothing - a
    rule the system cannot grade must not be allowed to silence another one.
    """
    return _SEVERITY_RANK.get((severity or "").strip().lower(), 0)


def _is_shadowed_by_specific_rule(
    target_ing_names: List[str],
    prod_name: str,
    comp_ing_name: str,
    category_severity: str,
    covered: Dict[tuple, int],
) -> bool:
    """True when PASS 1 has already reported every ingredient pair this category
    warning would restate, at least as seriously.

    The two rule tables describe the same clash at two levels of detail.
    conflict_rules names an ingredient pair that has been curated by hand;
    category_conflict_rules generalises over functional groups so a newly
    catalogued acid still raises a warning before anyone writes a rule for it.
    Where a curated rule exists the general one adds no fact - it restates the
    same clash between the same two products in different words, and the user
    reads two warnings for one problem. Retinol + Salicylic Acid is exactly
    this: conflict_rules and category_conflict_rules (Retinoid + BHA) both
    carry it, both at high, so a retinol serum checked against a BHA exfoliant
    produced two warnings naming the same product. BE-DEF-13.

    The specific rule wins because it names the actual ingredients and carries
    the message written for them. The general rule is the fallback, and stays
    in the table - deleting the row would leave a newly added AHA unwarned.

    Suppressed only when the specific rule is at least as severe, so a general
    rule graded higher than the curated one still reaches the user rather than
    being quietly downgraded.

    Suppressed only when *every* target ingredient in the group is covered. A
    product carrying two retinoids where only one has a curated rule still
    needs the category warning, because it is the only thing that mentions the
    other one.
    """
    if not target_ing_names:
        return False
    category_rank = _severity_rank(category_severity)
    for target_ing_name in target_ing_names:
        specific_rank = covered.get((target_ing_name, prod_name, comp_ing_name), 0)
        if specific_rank == 0 or specific_rank < category_rank:
            return False
    return True


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

    # Whether the target could be assessed at all, which is not the same thing
    # as whether anything was found wrong with it. A product with no ingredient
    # rows, and a product_id that resolves to no row at all, both reach every
    # return below having been compared against nothing - and used to be handed
    # back as is_safe=true, presenting an unassessed product to the user as
    # checked and clear. FE-DEF-28.
    #
    # Deliberately not tied to the comparison set: a target with ingredients and
    # an empty shelf HAS been assessed (the skin-type pass ran against it) and
    # may correctly come back clear. GET /products/compare depends on that - it
    # calls analyze(prod_b, user_id, []) with an empty comparison on purpose.
    target_assessed = bool(target_ingredient_ids)

    comparison_groups, comparison_ingredient_ids = _build_comparison_maps(comparison_products)

    # Nothing to compare against -> only skin-type warnings (if any) apply.
    if not comparison_ingredient_ids and not comparison_groups:
        warnings = _dedupe_warnings(warnings)
        return AnalysisResponse(is_safe=target_assessed and len(warnings) == 0, warnings=warnings)

    comparison_ing_names = {
        ing["id"]: ing["name"]
        for prod in comparison_products
        for ing in prod.get("ingredients", [])
        if ing.get("id") and ing.get("name")
    }

    # What PASS 1 has already said, so PASS 2 does not restate it:
    # (target ingredient, comparison product, comparison ingredient) -> severity rank.
    covered_by_specific_rule: Dict[tuple, int] = {}

    # --- PASS 1: ingredient-to-ingredient (conflict_rules) ---
    if target_ingredient_ids and comparison_ingredient_ids:
        specific_rules = supabase.table("conflict_rules").select("*").execute()
        for rule in (specific_rules.data or []):
            id_a = rule.get("ingredient_a_id")
            id_b = rule.get("ingredient_b_id")

            # A rule states an unordered pair, so either column may be the one
            # the target carries. Resolved once here rather than in two mirrored
            # branches, which have to stay in step with each other.
            if id_a in target_ingredient_ids and id_b in comparison_ingredient_ids:
                target_id, comparison_id = id_a, id_b
            elif id_b in target_ingredient_ids and id_a in comparison_ingredient_ids:
                target_id, comparison_id = id_b, id_a
            else:
                continue

            target_ing_name = target_ing_id_to_name[target_id]
            comp_ing_name = comparison_ing_names.get(comparison_id)
            severity = rule["severity"].title()

            # Every product carrying the clashing ingredient, not just one of
            # them. BE-DEF-12.
            for clashing_product in comparison_ingredient_ids[comparison_id]:
                warnings.append(WarningAlert(
                    alert_type="Chemical Interaction Warning",
                    severity=severity,
                    message=f"Conflict with {clashing_product}: Layering {target_ing_name} directly alongside it triggers a structural clash. {rule['warning_message']}",
                ))
                if comp_ing_name:
                    key = (target_ing_name, clashing_product, comp_ing_name)
                    covered_by_specific_rule[key] = max(
                        covered_by_specific_rule.get(key, 0), _severity_rank(rule["severity"])
                    )

    # --- PASS 2: functional-group / category (category_conflict_rules) ---
    if target_groups and comparison_groups:
        rules_res = supabase.table("category_conflict_rules").select("*").execute()
        for rule in (rules_res.data or []):
            rule_a = normalize_text_accents(rule.get("group_a"))
            rule_b = normalize_text_accents(rule.get("group_b"))

            if rule_a in target_groups and rule_b in comparison_groups:
                target_group, comparison_group = rule_a, rule_b
            elif rule_b in target_groups and rule_a in comparison_groups:
                target_group, comparison_group = rule_b, rule_a
            else:
                continue

            target_ing_names = ", ".join(target_groups[target_group])
            for prod_name, comp_ing_name in comparison_groups[comparison_group]:
                if _is_shadowed_by_specific_rule(
                    target_groups[target_group], prod_name, comp_ing_name,
                    rule["severity"], covered_by_specific_rule,
                ):
                    continue
                warnings.append(WarningAlert(
                    alert_type="Active Routine Clash",
                    severity=rule["severity"].title(),
                    message=f"Category Conflict with {prod_name}: Combining {target_ing_names} with {comp_ing_name} is unadvised. {rule['warning_message']}",
                ))

    warnings = _dedupe_warnings(warnings)
    return AnalysisResponse(is_safe=target_assessed and len(warnings) == 0, warnings=warnings)
