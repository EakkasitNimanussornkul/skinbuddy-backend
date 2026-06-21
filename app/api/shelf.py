from fastapi import APIRouter, Depends, HTTPException
from typing import Optional  
from app.schemas import ShelfItemCreate
from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.schemas import AnalysisResponse, WarningAlert
from pydantic import BaseModel

router = APIRouter()

@router.get("/")
async def get_user_shelf(user_id: str = Depends(get_current_user_id)):
    try:
        # Fetch shelf item -> product -> product_ingredients -> ingredients!
        response = supabase.table("shelf_items") \
            .select("*, products(*, product_ingredients(ingredients(*)))") \
            .eq("user_id", user_id) \
            .execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.post("/add")
async def add_to_shelf(
    item: ShelfItemCreate, 
    user_id: str = Depends(get_current_user_id)
):
    try:
        new_item = {
            "user_id": user_id,
            "product_id": item.product_id, 
            "usage_state": item.usage_state, 
            "opened_date": item.opened_date,
            "expiration_date": item.expiration_date,
            "pao": item.pao
        }
        
        response = supabase.table("shelf_items").insert(new_item).execute()
        return response.data[0] 
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.delete("/{item_id}")
async def delete_from_shelf(
    item_id: str, 
    user_id: str = Depends(get_current_user_id)
):
    try:
        supabase.table("shelf_items").delete().eq("id", item_id).eq("user_id", user_id).execute()
        return {"message": "Item removed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/analyze/{product_id}", response_model=AnalysisResponse)
async def analyze_product_compatibility(product_id: str, user_id: str = Depends(get_current_user_id)):
    warnings = []

    try:
        # Fetch User Profile
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
        user_skin_type = user_res.data.get("skin_type") # e.g., "OSPW"

        # Fetch Target Product Ingredients
        target_res = supabase.table("products").select("*, product_ingredients(ingredients(*))").eq("id", product_id).single().execute()
        
        target_ingredients = []
        if target_res.data and target_res.data.get("product_ingredients"):
            target_ingredients = [item["ingredients"] for item in target_res.data["product_ingredients"] if item.get("ingredients")]
            
        target_ing_ids = [ing["id"] for ing in target_ingredients]

        # ---------------------------------------------------------
        # PASS 1: BIOLOGICAL VERIFICATION (The Bug Fix)
        # ---------------------------------------------------------
        if user_skin_type:
            for ing in target_ingredients:
                bad_for_str = ing.get("bad_for")
                if bad_for_str:
                    # Loop through each letter (O, S, P, W)
                    for letter in user_skin_type:
                        # Check if "(S)" is inside "Sensitive (S), Very Dry (D)"
                        if f"({letter})" in bad_for_str:
                            warnings.append(WarningAlert(
                                alert_type="Biological",
                                severity="High",
                                message=f"Personalized Alert: {ing['name']} is known to trigger adverse reactions for your skin profile."
                            ))
                            break # Break inner loop to avoid double-warning for the same ingredient

        # ---------------------------------------------------------
        # PASS 2: CHEMICAL CONFLICT VERIFICATION
        # ---------------------------------------------------------
        shelf_res = supabase.table("shelf_items") \
            .select("product_id, products(name, product_ingredients(ingredients(id, name)))") \
            .eq("user_id", user_id) \
            .eq("usage_state", "active") \
            .execute()
                
        shelf_ing_ids = []
        shelf_product_map = {} 
        
        for item in shelf_res.data:
            if not item.get("products"): continue
            prod_name = item["products"]["name"]
            for pi in item["products"].get("product_ingredients", []):
                ing_id = pi["ingredients"]["id"]
                shelf_ing_ids.append(ing_id)
                shelf_product_map[ing_id] = prod_name

        if target_ing_ids and shelf_ing_ids:
            rules_res = supabase.table("conflict_rules").select("*").execute()
        
            for rule in rules_res.data:
                a_id = rule["ingredient_a_id"]
                b_id = rule["ingredient_b_id"]

                if a_id in target_ing_ids and b_id in shelf_ing_ids:
                    warnings.append(WarningAlert(
                        alert_type="Chemical",
                        severity=rule["severity"],
                        message=f"Conflict with your {shelf_product_map[b_id]}: {rule['warning_message']}"
                    ))
                elif b_id in target_ing_ids and a_id in shelf_ing_ids:
                    warnings.append(WarningAlert(
                        alert_type="Chemical",
                        severity=rule["severity"],
                        message=f"Conflict with your {shelf_product_map[a_id]}: {rule['warning_message']}"
                    ))

        return AnalysisResponse(
            is_safe=len(warnings) == 0,
            warnings=warnings
        )
    except Exception as e:
        print("Analysis Error:", e)
        raise HTTPException(status_code=500, detail="Failed to analyze product.")

class ItemOpenRequest(BaseModel):
    opened_date: str
    expiration_date: Optional[str] = None

@router.patch("/{item_id}/open")
async def mark_item_opened(item_id: str, req: ItemOpenRequest, user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("shelf_items").update({
            "opened_date": req.opened_date,
            "expiration_date": req.expiration_date
        }).eq("id", item_id).eq("user_id", user_id).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── UPDATED SPECIFICALLY FOR THE ARCHIVE FORM METRICS ──
class UpdateStatusRequest(BaseModel):
    usage_state: str
    outcome: Optional[str] = None  # Accepts: 'empty', 'discarded', 'expired'
    notes: Optional[str] = None    # User tracking reflection text
    archived_at: Optional[str] = None # 

# shelf.py - UPDATED
@router.patch("/{item_id}/status")
async def update_shelf_status(item_id: str, req: UpdateStatusRequest, user_id: str = Depends(get_current_user_id)):
    try:
        # We now only care about the transition TO archived
        update_data = {
            "usage_state": req.usage_state
        }

        if req.usage_state == "archived":
            update_data["archive_outcome"] = req.outcome
            update_data["archive_notes"] = req.notes
            update_data["archived_at"] = req.archived_at 
        
        # Logic to "nullify" or restore has been removed.
        
        response = supabase.table("shelf_items").update(update_data).eq("id", item_id).eq("user_id", user_id).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))