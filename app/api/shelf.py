from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List  
from app.schemas import ShelfItemCreate
from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.schemas import AnalysisResponse, WarningAlert
from pydantic import BaseModel
import unicodedata

router = APIRouter()

def normalize_text_accents(text: str) -> str:
    """Transforms characters like Céramide into Ceramide to match core conflict engine constraints."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    ).strip().lower()


@router.get("/")
async def get_user_shelf(user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("shelf_items") \
            .select("*, products(*, product_ingredients(ingredients(*)))") \
            .eq("user_id", user_id) \
            .execute()
        
        return response.data
    except Exception as e:
        print("❌ GET /shelf/ Error:", str(e))
        raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")

@router.post("/add")
async def add_to_shelf(item: ShelfItemCreate, user_id: str = Depends(get_current_user_id)):
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
async def delete_from_shelf(item_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        supabase.table("shelf_items").delete().eq("id", item_id).eq("user_id", user_id).execute()
        return {"message": "Item removed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/analyze/{product_id}", response_model=AnalysisResponse)
async def analyze_product_compatibility(product_id: str, user_id: str = Depends(get_current_user_id)):
    warnings = []

    try:
        # 1. Fetch User Profile
        user_res = supabase.table("users").select("skin_type").eq("id", user_id).single().execute()
        user_skin_type = user_res.data.get("skin_type") if user_res.data else None

        # 2. Fetch Target Product Metadata (including ingredient_concerns)
        target_res = supabase.table("products") \
            .select("*, product_ingredients(ingredients(*))") \
            .eq("id", product_id) \
            .single() \
            .execute()
        
        target_ingredients_ids = []
        target_groups = {}
        target_ing_id_to_name = {}
        
        if target_res.data and target_res.data.get("product_ingredients"):
            for item in target_res.data["product_ingredients"]:
                ing = item.get("ingredients")
                if ing:
                    target_ingredients_ids.append(ing["id"])
                    target_ing_id_to_name[ing["id"]] = ing["name"]
                    
                    fg = ing.get("functional_group")
                    if fg:
                        norm_fg = normalize_text_accents(fg)
                        target_groups.setdefault(norm_fg, []).append(ing["name"])

        # 3. Check Baumann Skin Type Direct Conflicts
        if user_skin_type and target_res.data:
            for item in target_res.data["product_ingredients"]:
                ing = item.get("ingredients")
                if ing:
                    bad_for_str = ing.get("bad_for")
                    if bad_for_str and any(f"({letter})" in bad_for_str for letter in user_skin_type):
                        warnings.append(WarningAlert(
                            alert_type="Skin Type Conflict",
                            severity="High",
                            message=f"Personalized Alert: {ing['name']} is known to trigger adverse reactions for Baumann Type {user_skin_type}."
                        ))

        # 4. Fetch Opened Active Shelf Items
        shelf_res = supabase.table("shelf_items") \
            .select("product_id, products(name, product_ingredients(ingredients(id, name, functional_group)))") \
            .eq("user_id", user_id) \
            .eq("usage_state", "active") \
            .execute()
                
        shelf_groups = {}
        shelf_ingredient_ids = {} # Map: ingredient_id -> product_name
        
        for item in (shelf_res.data or []):
            prod = item.get("products")
            if not prod: continue
            prod_name = prod["name"]
            for pi in prod.get("product_ingredients", []):
                ing = pi.get("ingredients")
                if ing:
                    shelf_ingredient_ids[ing["id"]] = prod_name
                    if ing.get("functional_group"):
                        norm_fg = normalize_text_accents(ing["functional_group"])
                        shelf_groups.setdefault(norm_fg, []).append((prod_name, ing["name"]))

        if not shelf_ingredient_ids and not shelf_groups:
            return AnalysisResponse(is_safe=True, warnings=[])

        # 5. PASS 1: Relational Ingredient-to-Ingredient Check (Reads conflict_rules table)
        if target_ingredients_ids and shelf_ingredient_ids:
            specific_rules = supabase.table("conflict_rules").select("*").execute()
            for rule in (specific_rules.data or []):
                id_a = rule.get("ingredient_a_id")
                id_b = rule.get("ingredient_b_id")
                
                if id_a in target_ingredients_ids and id_b in shelf_ingredient_ids:
                    clashing_product = shelf_ingredient_ids[id_b]
                    warnings.append(WarningAlert(
                        alert_type="Chemical Interaction Warning",
                        severity=rule["severity"].title(),
                        message=f"Conflict with active {clashing_product}: Layering {target_ing_id_to_name[id_a]} directly alongside ingredients in your current routine triggers a structural clash. {rule['warning_message']}"
                    ))
                elif id_b in target_ingredients_ids and id_a in shelf_ingredient_ids:
                    clashing_product = shelf_ingredient_ids[id_a]
                    warnings.append(WarningAlert(
                        alert_type="Chemical Interaction Warning",
                        severity=rule["severity"].title(),
                        message=f"Conflict with active {clashing_product}: Layering {target_ing_id_to_name[id_b]} directly alongside ingredients in your current routine triggers a structural clash. {rule['warning_message']}"
                    ))

        # 6. PASS 2: Structural Category Group Check (Reads category_conflict_rules table)
        if target_groups and shelf_groups:
            rules_res = supabase.table("category_conflict_rules").select("*").execute()
            for rule in (rules_res.data or []):
                rule_a = normalize_text_accents(rule.get("group_a"))
                rule_b = normalize_text_accents(rule.get("group_b"))

                if rule_a in target_groups and rule_b in shelf_groups:
                    target_ing_names = ", ".join(target_groups[rule_a])
                    for prod_name, shelf_ing_name in shelf_groups[rule_b]:
                        warnings.append(WarningAlert(
                            alert_type="Active Routine Clash",
                            severity=rule["severity"].title(),
                            message=f"Category Conflict with {prod_name}: Combining {target_ing_names} with {shelf_ing_name} is unadvised. {rule['warning_message']}"
                        ))
                elif rule_b in target_groups and rule_a in shelf_groups:
                    target_ing_names = ", ".join(target_groups[rule_b])
                    for prod_name, shelf_ing_name in shelf_groups[rule_a]:
                        warnings.append(WarningAlert(
                            alert_type="Active Routine Clash",
                            severity=rule["severity"].title(),
                            message=f"Category Conflict with {prod_name}: Combining {target_ing_names} with {shelf_ing_name} is unadvised. {rule['warning_message']}"
                        ))

        return AnalysisResponse(
            is_safe=len(warnings) == 0,
            warnings=warnings
        )
    except Exception as e:
        print("Analysis Error:", e)
        raise HTTPException(status_code=500, detail="Failed to analyze product compatibility.")

class ItemOpenRequest(BaseModel):
    opened_date: str
    expiration_date: Optional[str] = None
    pao: Optional[int] = None

@router.patch("/{item_id}/open")
async def mark_item_opened(item_id: str, req: ItemOpenRequest, user_id: str = Depends(get_current_user_id)):
    try:
        update_data = {
            "opened_date": req.opened_date,
            "expiration_date": req.expiration_date,
            "usage_state": "active"
        }
        if req.pao is not None:
            update_data["pao"] = req.pao

        response = supabase.table("shelf_items").update(update_data).eq("id", item_id).eq("user_id", user_id).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class UpdateStatusRequest(BaseModel):
    usage_state: str
    outcome: Optional[str] = None 
    notes: Optional[str] = None    
    archived_at: Optional[str] = None 

@router.patch("/{item_id}/status")
async def update_shelf_status(item_id: str, req: UpdateStatusRequest, user_id: str = Depends(get_current_user_id)):
    try:
        update_data = {
            "usage_state": req.usage_state
        }
        if req.usage_state == "archived":
            update_data["archive_outcome"] = req.outcome
            update_data["archive_notes"] = req.notes
            update_data["archived_at"] = req.archived_at 
        
        response = supabase.table("shelf_items").update(update_data).eq("id", item_id).eq("user_id", user_id).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))