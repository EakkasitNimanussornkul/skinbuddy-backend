import re
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from app.db.connection import supabase
from app.core.services.token import get_current_user_id, get_optional_user_id
from app.schemas import ProductDetail, CompareResponse, SharedIngredient
from app.core.services.ingredientcheck_service import calculate_safety_flags  # 🌟 ADDED IMPORT
from app.core.services import compatibility_service

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
        res = supabase.table("products").select("*, product_ingredients(ingredients(*, ingredient_concerns(*)))").eq("id", clean_id).single().execute()
        if res.data:
            return res.data

    # 2. Check exact slug or normalized name matches across up to 2000 items
    clean_target = re.sub(r'[^a-z0-9]', '', clean_id)
    res = supabase.table("products").select("*, product_ingredients(ingredients(*, ingredient_concerns(*)))").limit(2000).execute()
    
    words = [w for w in clean_id.replace("-", " ").split() if len(w) > 2]
    
    for prod in (res.data or []):
        brand = prod.get("brand", "") or ""
        name = prod.get("name", "") or ""
        prod_slug = create_slug(brand, name)
        clean_prod = re.sub(r'[^a-z0-9]', '', prod_slug)
        clean_name = re.sub(r'[^a-z0-9]', '', name.lower())
        
        if clean_target in [clean_prod, clean_name] or clean_prod in clean_target or clean_target in clean_prod:
            return prod

    # 3. Fuzzy keyword match fallback
    if words:
        for prod in (res.data or []):
            full_text = f"{prod.get('brand', '')} {prod.get('name', '')}".lower()
            if all(w in full_text for w in words[:3]):
                return prod
    return None

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
            ings = [item["ingredients"] for item in prod.get("product_ingredients", []) if item.get("ingredients")]
            
            # 🌟 Compute compatibility score if skin type exists
            if user_skin_type:
                match_info = compute_baumann_compatibility(user_skin_type, ings)
            else:
                match_info = {"skin_match_score": None, "match_reasons": [], "caution_reasons": []}
            
            cat = prod.get("category", "Moisturizer")
            sim_res = supabase.table("products").select("id, brand, name, image_url, price_thb, price_usd").eq("category", cat).neq("id", prod["id"]).limit(4).execute()
            similar_products = []
            for sp in (sim_res.data or []):
                similar_products.append({**sp, "slug": create_slug(sp.get("brand", ""), sp.get("name", ""))})

            safety_flags = calculate_safety_flags(prod.get("product_ingredients", []))

            return {
                **prod,
                "skin_match_score": match_info.get("score"),
                "match_reasons": match_info.get("match_reasons", []),
                "caution_reasons": match_info.get("caution_reasons", []),
                "safety_flags": safety_flags,
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
    user_id: Optional[str] = Depends(get_optional_user_id)
):
    try:
        user_skin_type = ""
        if user_id:
            user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
            user_skin_type = user_res.data.get("skin_type", "") if user_res.data else ""

        query = supabase.table("products").select("*, product_ingredients(ingredients(*, ingredient_concerns(*)))")
        if q:
            clean_q = q.strip()
            query = query.or_(f"name.ilike.%{clean_q}%,brand.ilike.%{clean_q}%,category.ilike.%{clean_q}%")
        
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
            
            if user_skin_type:
                match_info = compute_baumann_compatibility(user_skin_type, ings)
                score = match_info["score"]
                match_reasons = match_info["match_reasons"]
                caution_reasons = match_info["caution_reasons"]
            else:
                score = None
                match_reasons = []
                caution_reasons = []

            preview_names = [ing["name"] for ing in ings[:3]]
            prod_slug = create_slug(prod.get("brand", ""), prod.get("name", ""))

            rank_priority = 3
            if lower_q and lower_q in (prod.get("name") or "").lower(): rank_priority = 1
            elif lower_q and lower_q in (prod.get("brand") or "").lower(): rank_priority = 2

            # 🌟 ATTACH DYNAMIC SAFETY FLAGS
            safety_flags = calculate_safety_flags(prod.get("product_ingredients", []))

            enriched_products.append({
                **prod,
                "skin_match_score": score,
                "match_reasons": match_reasons,
                "caution_reasons": caution_reasons,
                "safety_flags": safety_flags,
                "has_conflict": len(caution_reasons) > 0,
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

        union_ids = set_a.union(set_b)
        similarity = (len(shared_ids) / len(union_ids)) * 100 if union_ids else 0.0

        # Full 3-pass check (skin-type + ingredient-pair + category rules), reusing the
        # same engine Shelf/Routine use instead of a partial category-only duplicate.
        # A single analyze(target=A, comparison=[B]) call already captures every A<->B
        # pairwise/category clash in both rule orderings; the second call (empty
        # comparison) only adds B's own skin-type warnings, so nothing double-counts.
        result_a = compatibility_service.analyze(prod_a["id"], user_id, [compatibility_service.normalize_product(prod_b)])
        result_b = compatibility_service.analyze(prod_b["id"], user_id, [])
        conflicts = result_a.warnings + result_b.warnings

        return CompareResponse(
            product_a=prod_a,
            product_b=prod_b,
            shared_ingredients=shared_ingredients,
            similarity_score=round(similarity, 1),
            conflicts=conflicts
        )
    except HTTPException as he: raise he
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@router.get("/{product_id}")
async def get_product_detail(product_id: str):
    res = supabase.table("products").select("*, product_ingredients(ingredients(*))").eq("id", product_id).single().execute()
    data = res.data
    
    if data:
        data["safety_flags"] = calculate_safety_flags(data.get("product_ingredients", []))
    
    return data