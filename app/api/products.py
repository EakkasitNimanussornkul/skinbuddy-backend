from fastapi import APIRouter, Depends, HTTPException
from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.schemas import ProductDetail, CompareResponse, SharedIngredient, WarningAlert

router = APIRouter()

@router.get("/search")
async def search_products(q: str = "", user_id: str = Depends(get_current_user_id)):
    try:
        query = supabase.table("products").select("*, product_ingredients(ingredients(*))")
        
        if q:
            # If the user typed something, filter by name or brand
            query = query.or_(f"name.ilike.%{q}%,brand.ilike.%{q}%")
        # Execute the query (limit to 100 so we don't overload the frontend if the DB gets huge)
        response = query.limit(100).execute()
            
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database search error: {str(e)}")
async def get_product_deep_join(product_id: str):
    try:
        # THE MAGIC DEEP JOIN: 
        # Notice the syntax: table(nested_table(nested_nested_table(*)))
        response = supabase.table("products").select(
            "*, product_ingredients(ingredients(*))"
        ).eq("id", product_id).single().execute()
        
        return response.data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.get("/compare", response_model=CompareResponse)
async def compare_two_products(product_a_id: str, product_b_id: str):
    try:
        # 1. Fetch both products with the Deep Join
        res_a = supabase.table("products").select("*, product_ingredients(ingredients(*))").eq("id", product_a_id).single().execute()
        res_b = supabase.table("products").select("*, product_ingredients(ingredients(*))").eq("id", product_b_id).single().execute()

        prod_a = res_a.data
        prod_b = res_b.data

        # 2. Extract ingredients into dictionaries for fast matching
        ings_a = {item["ingredients"]["id"]: item["ingredients"] for item in prod_a.get("product_ingredients", []) if item.get("ingredients")}
        ings_b = {item["ingredients"]["id"]: item["ingredients"] for item in prod_b.get("product_ingredients", []) if item.get("ingredients")}

        set_a = set(ings_a.keys())
        set_b = set(ings_b.keys())

        # 3. Find Shared Ingredients (Dupes)
        shared_ids = set_a.intersection(set_b)
        shared_ingredients = [
            SharedIngredient(id=i, name=ings_a[i]["name"], benefits=ings_a[i].get("benefits")) 
            for i in shared_ids
        ]

        # 4. Calculate Similarity Score (Jaccard Index formula)
        union_ids = set_a.union(set_b)
        similarity = (len(shared_ids) / len(union_ids)) * 100 if union_ids else 0.0

        # 5. Check for Chemical Conflicts
        conflicts = []
        if set_a and set_b:
            rules_res = supabase.table("conflict_rules").select("*").execute()
            for rule in rules_res.data:
                r_a = rule["ingredient_a_id"]
                r_b = rule["ingredient_b_id"]

                # Does A have ingredient 1 and B have ingredient 2? (Or vice versa)
                if (r_a in set_a and r_b in set_b) or (r_b in set_a and r_a in set_b):
                    conflicts.append(WarningAlert(
                        alert_type="Chemical",
                        severity=rule["severity"],
                        message=f"Interaction Alert: {rule['warning_message']}"
                    ))

        # 6. Return the full analysis
        return CompareResponse(
            product_a=prod_a,
            product_b=prod_b,
            shared_ingredients=shared_ingredients,
            similarity_score=round(similarity, 1),
            conflicts=conflicts
        )

    except Exception as e:
        print("Compare Error:", e)
        raise HTTPException(status_code=400, detail="Failed to compare products.")