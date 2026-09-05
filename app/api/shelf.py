from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
from app.schemas import ShelfItemCreate, UsageState
from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.schemas import AnalysisResponse
from app.core.services import compatibility_service
from pydantic import BaseModel

router = APIRouter()


@router.get("/")
async def get_user_shelf(user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("shelf_items") \
            .select("*, products(*, product_ingredients(ingredients(*)))") \
            .eq("user_id", user_id) \
            .execute()
        
        return response.data
    except Exception as e:
        print("GET /shelf/ error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch shelf.")

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
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to add item to shelf.")
        return response.data[0]
    except HTTPException:
        # Without this the generic handler below would swallow the status above
        # and answer 500 for every refusal.
        raise
    except Exception as e:
        print("POST /shelf/add error:", e)
        raise HTTPException(status_code=500, detail="Failed to add item to shelf.")

@router.delete("/{item_id}")
async def delete_from_shelf(item_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("shelf_items").delete().eq("id", item_id).eq("user_id", user_id).execute()
        # An id that is unknown, or that belongs to someone else, matches no row.
        # Reporting success there left the client unable to tell a refused delete
        # from a real one, so its shelf list could drift out of sync silently.
        if not response.data:
            raise HTTPException(status_code=404, detail="Shelf item not found.")
        return {"message": "Item removed successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print("DELETE /shelf/{item_id} error:", e)
        raise HTTPException(status_code=500, detail="Failed to remove item from shelf.")

@router.get("/analyze/{product_id}", response_model=AnalysisResponse)
async def analyze_product_compatibility(product_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        # Resolve the target first so an unknown product is refused as a 404
        # rather than analysed into a confident "safe" verdict. Previously
        # .single() raised PGRST116 here and the generic clause below reported
        # it as a 500. BE-DEF-07.
        target_res = (
            supabase.table("products")
            .select("category, product_ingredients(ingredients(id, name, functional_group))")
            .eq("id", product_id)
            .limit(1)
            .execute()
        )
        if not target_res.data:
            raise HTTPException(status_code=404, detail="Product not found.")
        target = target_res.data[0]

        comparison = compatibility_service.get_active_shelf_products(user_id)
        result = compatibility_service.analyze(product_id, user_id, comparison)

        # Dupe detection ("you already own something like this") is computed
        # here rather than inside analyze(), which is also called by the
        # routine and product-compare paths - those don't want it, and adding
        # a mode flag to analyze() for one caller isn't worth the complexity.
        # It is advisory only and must never touch is_safe/warnings above.
        # `target` was resolved at the top of this handler, so the row is
        # already known to exist and is not re-fetched.
        target_ingredients = [
            item["ingredients"]
            for item in (target.get("product_ingredients") or [])
            if item.get("ingredients")
        ]
        dupe_shelf = compatibility_service.get_shelf_products_for_dupe_check(user_id)
        result.duplicates = compatibility_service.find_shelf_duplicates(
            product_id, target_ingredients, target.get("category"), dupe_shelf
        )

        return result
    except HTTPException:
        # Ahead of the generic clause: it catches HTTPException too and would
        # re-raise the 404 above as a 500 - the trap BE-DEF-02 describes.
        raise
    except Exception as e:
        print("Shelf analysis error:", e)
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
        # No match means the item is unknown or owned by someone else. Indexing
        # an empty list here used to raise IndexError into the handler below,
        # answering 500 for a request that was correctly refused.
        if not response.data:
            raise HTTPException(status_code=404, detail="Shelf item not found.")
        return response.data[0]
    except HTTPException:
        raise
    except Exception as e:
        print("PATCH /shelf/{item_id}/open error:", e)
        raise HTTPException(status_code=500, detail="Failed to mark item as opened.")

class UpdateStatusRequest(BaseModel):
    usage_state: UsageState
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
        else:
            # Clear them on the way out. Leaving the previous archive values in
            # place made an un-archived row read as an active item that also
            # carried a completed archive record.
            update_data["archive_outcome"] = None
            update_data["archive_notes"] = None
            update_data["archived_at"] = None

        response = supabase.table("shelf_items").update(update_data).eq("id", item_id).eq("user_id", user_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Shelf item not found.")
        return response.data[0]
    except HTTPException:
        raise
    except Exception as e:
        print("PATCH /shelf/{item_id}/status error:", e)
        raise HTTPException(status_code=500, detail="Failed to update item status.")