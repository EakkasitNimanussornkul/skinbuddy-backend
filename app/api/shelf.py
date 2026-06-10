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
            "status": item.status,
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
        # 1. Fetch User's Skin Type
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
        user_skin_type = user_res.data.get("skin_type")

        # 2. Fetch the Target Product's Ingredients
        target_res = supabase.table("products").select("*, product_ingredients(ingredients(*))").eq("id", product_id).single().execute()
        target_ingredients = [item["ingredients"] for item in target_res.data.get("product_ingredients", [])]
        target_ing_ids = [ing["id"] for ing in target_ingredients]

        # CHECK 1: BIOLOGICAL (User Skin vs Product)
        if user_skin_type:
            for ing in target_ingredients:
                if ing.get("bad_for") and user_skin_type in ing.get("bad_for"):
                    warnings.append(WarningAlert(
                        alert_type="Biological",
                        severity="Moderate",
                        message=f"Personalized Alert: {ing['name']} may be too harsh for {user_skin_type} skin."
                    ))

        # CHECK 2: CHEMICAL (Shelf vs Product)
        shelf_res = supabase.table("shelf_items").select("product_id, products(name, product_ingredients(ingredients(id, name)))").eq("user_id", user_id).execute()
        
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

@router.patch("/{item_id}/status")
async def update_shelf_status(item_id: str, req: UpdateStatusRequest, user_id: str = Depends(get_current_user_id)):
    try:
        # Prepare basic usage_state update payload
        update_data = {
            "usage_state": req.usage_state
        }

        # If archiving, latch the incoming metadata payload values
        if req.usage_state == "archived":
            update_data["archive_outcome"] = req.outcome
            update_data["archive_notes"] = req.notes
        else:
            # If unarchiving (restoring to shelf), clear old log fields cleanly
            update_data["archive_outcome"] = None
            update_data["archive_notes"] = None

        response = supabase.table("shelf_items").update(update_data).eq("id", item_id).eq("user_id", user_id).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))