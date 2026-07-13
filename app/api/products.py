import re
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.schemas import ProductDetail, CompareResponse, SharedIngredient, WarningAlert

router = APIRouter()

def create_slug(brand: str, name: str) -> str:
    """Converts 'COSRX', 'Advanced Snail 96 Mucin Power Essence' -> 'cosrx-advanced-snail-96-mucin-power-essence'"""
    raw = f"{brand}-{name}".lower()
    slug = re.sub(r'[^a-z0-9]+', '-', raw).strip('-')
    return slug

def compute_baumann_compatibility(user_skin_type: str, ingredients: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluates product ingredients against Baumann 16-Type combinations."""
    if not user_skin_type or len(user_skin_type) < 4:
        return {"score": 85, "match_reasons": ["Formulated for general barrier maintenance."], "caution_reasons": []}

    score = 75
    match_reasons = []
    caution_reasons = []

    f_groups = {ing.get("functional_group", "").strip(): ing.get("name") for ing in ingredients if ing.get("functional_group")}
    all_names = [ing.get("name", "").lower() for ing in ingredients]

    # Axis 1: Oily (O) vs. Dry (D)
    if "D" in user_skin_type:
        if any(g in f_groups for g in ["Humectant", "Barrier Support", "Heavy Occlusive"]):
            score += 10
            match_reasons.append("Barrier-repair formula: Contains humectants and lipids that deeply replenish dry skin.")
        if any(any(bad in n for bad in ["alcohol denat", "isopropyl alcohol"]) for n in all_names):
            score -= 20
            caution_reasons.append("Drying risk: High concentrations of volatile alcohols may strip natural barrier moisture.")
    elif "O" in user_skin_type:
        if any(g in f_groups for g in ["Direct Acid (BHA)", "Direct Acid (AHA/BHA)", "Vitamin B3"]):
            score += 10
            match_reasons.append("Oil-control support: Formulated with clarifying actives to regulate excess sebum.")
        if any(g == "Heavy Occlusive" for g in f_groups):
            score -= 15
            caution_reasons.append("Heavy texture warning: Rich occlusive emollients may feel heavy or congest oily pores.")

    # Axis 2: Sensitive (S) vs. Resistant (R)
    if "S" in user_skin_type:
        if any(g in f_groups for g in ["Botanical Soother", "Pro-Vitamin B5"]):
            score += 10
            match_reasons.append("Calming complex: Features soothing botanicals to visibly relieve redness and irritation.")
        if any("parfum" in n or "fragrance" in n for n in all_names):
            score -= 25
            caution_reasons.append("Sensitivity trigger: Contains synthetic fragrance/parfum which frequently triggers contact dermatitis.")
    elif "R" in user_skin_type:
        score += 5

    # Axis 3: Pigmented (P) vs. Non-Pigmented (N)
    if "P" in user_skin_type:
        if any(g in f_groups for g in ["Vitamin C", "Vitamin B3", "Active Acid Component"]):
            score += 10
            match_reasons.append("Tone-refining profile: Includes targeted brightening actives to fade post-acne marks and hyperpigmentation.")

    # Axis 4: Wrinkle-Prone (W) vs. Tight (T)
    if "W" in user_skin_type:
        if any(g in f_groups for g in ["Retinoid", "Peptide", "Antioxidant"]):
            score += 10
            match_reasons.append("Cellular renewal: Powered by age-supporting actives that stimulate collagen and smooth fine lines.")

    return {
        "score": max(15, min(99, score)),
        "match_reasons": match_reasons if match_reasons else ["Suitable for daily routine wear."],
        "caution_reasons": caution_reasons
    }

async def resolve_product_record(identifier: str) -> dict or None:
    """Helper that finds a product whether passed a UUID, an exact slug, or a partial string."""
    clean_id = identifier.lower().strip()
    
    # 1. Check if identifier is a direct UUID
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', clean_id):
        res = supabase.table("products").select("*, product_ingredients(ingredients(*))").eq("id", clean_id).single().execute()
        if res.data:
            return res.data

    # 2. Check exact slug or normalized name matches across up to 2000 items
    clean_target = re.sub(r'[^a-z0-9]', '', clean_id)
    res = supabase.table("products").select("*, product_ingredients(ingredients(*))").limit(2000).execute()
    
    words = [w for w in clean_id.replace("-", " ").split() if len(w) > 2]
    
    for prod in (res.data or []):
        brand = prod.get("brand", "") or ""
        name = prod.get("name", "") or ""
        prod_slug = create_slug(brand, name)
        clean_prod = re.sub(r'[^a-z0-9]', '', prod_slug)
        clean_name = re.sub(r'[^a-z0-9]', '', name.lower())
        
        if clean_target in [clean_prod, clean_name] or clean_prod in clean_target or clean_target in clean_prod:
            return prod

    # 3. Fuzzy keyword match fallback (catches truncated DB strings like 'Essen')
    if words:
        for prod in (res.data or []):
            full_text = f"{prod.get('brand', '')} {prod.get('name', '')}".lower()
            if all(w in full_text for w in words[:3]):
                return prod
    return None

# 🌟 HIGH-PRIORITY SLUG ROUTE 🌟
@router.get("/slug/{slug}")
async def get_product_by_slug(slug: str, user_id: str = Depends(get_current_user_id)):
    try:
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
        user_skin_type = user_res.data.get("skin_type", "") if user_res.data else ""
        
        prod = await resolve_product_record(slug)
        if prod:
            ings = [item["ingredients"] for item in prod.get("product_ingredients", []) if item.get("ingredients")]
            match_info = compute_baumann_compatibility(user_skin_type, ings)
            
            # 🌟 Fetch 4 Dynamic "Often Compared With" Recommendations 🌟
            cat = prod.get("category", "Moisturizer")
            sim_res = supabase.table("products").select("id, brand, name, image_url, price_thb, price_usd").eq("category", cat).neq("id", prod["id"]).limit(4).execute()
            similar_products = []
            for sp in (sim_res.data or []):
                similar_products.append({**sp, "slug": create_slug(sp.get("brand", ""), sp.get("name", ""))})

            return {
                **prod,
                **match_info,
                "slug": create_slug(prod.get("brand", ""), prod.get("name", "")),
                "similar_products": similar_products
            }
            
        raise HTTPException(status_code=404, detail=f"Product matching slug '{slug}' not found.")
    except HTTPException as he: raise he
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/search")
async def search_products(
    q: str = "", 
    min_price: Optional[int] = None, 
    max_price: Optional[int] = None, 
    user_id: str = Depends(get_current_user_id)
):
    try:
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
        user_skin_type = user_res.data.get("skin_type", "") if user_res.data else ""

        query = supabase.table("products").select("*, product_ingredients(ingredients(*))")
        if q:
            clean_q = q.strip()
            query = query.or_(f"name.ilike.%{clean_q}%,brand.ilike.%{clean_q}%,category.ilike.%{clean_q}%")
        
        # 🌟 Price Range Filtering 🌟
        if min_price is not None:
            query = query.gte("price_thb", min_price)
        if max_price is not None:
            query = query.lte("price_thb", max_price)

        response = query.limit(100).execute()
        products = response.data or []

        enriched_products = []
        lower_q = q.lower().strip()

        for prod in products:
            ings = [item["ingredients"] for item in prod.get("product_ingredients", []) if item.get("ingredients")]
            match_info = compute_baumann_compatibility(user_skin_type, ings)
            preview_names = [ing["name"] for ing in ings[:3]]
            prod_slug = create_slug(prod.get("brand", ""), prod.get("name", ""))

            rank_priority = 3
            if lower_q and lower_q in (prod.get("name") or "").lower(): rank_priority = 1
            elif lower_q and lower_q in (prod.get("brand") or "").lower(): rank_priority = 2

            enriched_products.append({
                **prod,
                "skin_match_score": match_info["score"],
                "match_reasons": match_info["match_reasons"],
                "caution_reasons": match_info["caution_reasons"],
                "has_conflict": len(match_info["caution_reasons"]) > 0,
                "top_ingredients": preview_names,
                "slug": prod_slug,
                "_rank": rank_priority
            })

        enriched_products.sort(key=lambda x: x["_rank"])
        for p in enriched_products: p.pop("_rank", None)
        return enriched_products
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database search error: {str(e)}")

@router.get("/compare", response_model=CompareResponse)
async def compare_two_products(product_a_id: str, product_b_id: str):
    try:
        prod_a = await resolve_product_record(product_a_id)
        prod_b = await resolve_product_record(product_b_id)

        if not prod_a or not prod_b:
            raise HTTPException(status_code=404, detail="One or both products could not be resolved.")

        ings_a = {item["ingredients"]["id"]: item["ingredients"] for item in prod_a.get("product_ingredients", []) if item.get("ingredients")}
        ings_b = {item["ingredients"]["id"]: item["ingredients"] for item in prod_b.get("product_ingredients", []) if item.get("ingredients")}

        set_a = set(ings_a.keys())
        set_b = set(ings_b.keys())

        shared_ids = set_a.intersection(set_b)
        shared_ingredients = [
            SharedIngredient(id=i, name=ings_a[i]["name"], benefits=ings_a[i].get("benefits")) 
            for i in shared_ids
        ]

        union_ids = set_a.union(set_b)
        similarity = (len(shared_ids) / len(union_ids)) * 100 if union_ids else 0.0

        conflicts = []
        groups_a = {}
        for ing in ings_a.values():
            fg = ing.get("functional_group")
            if fg: groups_a.setdefault(fg.strip().lower(), []).append(ing["name"])

        groups_b = {}
        for ing in ings_b.values():
            fg = ing.get("functional_group")
            if fg: groups_b.setdefault(fg.strip().lower(), []).append(ing["name"])

        if groups_a and groups_b:
            rules_res = supabase.table("category_conflict_rules").select("*").execute()
            for rule in (rules_res.data or []):
                rule_a = (rule.get("group_a") or "").strip().lower()
                rule_b = (rule.get("group_b") or "").strip().lower()

                if rule_a in groups_a and rule_b in groups_b:
                    conflicts.append(WarningAlert(
                        alert_type="Category Clash",
                        severity=rule["severity"],
                        message=f"Interaction between {', '.join(groups_a[rule_a])} ({rule['group_a']}) and {', '.join(groups_b[rule_b])} ({rule['group_b']}): {rule['warning_message']}"
                    ))
                elif rule_b in groups_a and rule_a in groups_b:
                    conflicts.append(WarningAlert(
                        alert_type="Category Clash",
                        severity=rule["severity"],
                        message=f"Interaction between {', '.join(groups_a[rule_b])} ({rule['group_b']}) and {', '.join(groups_b[rule_a])} ({rule['group_a']}): {rule['warning_message']}"
                    ))

        return CompareResponse(
            product_a=prod_a,
            product_b=prod_b,
            shared_ingredients=shared_ingredients,
            similarity_score=round(similarity, 1),
            conflicts=conflicts
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        print("Compare Error:", e)
        raise HTTPException(status_code=400, detail="Failed to compare products.")