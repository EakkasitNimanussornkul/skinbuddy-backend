from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
from app.schemas import ShelfItemCreate
from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.core.services import compatibility_service
from app.schemas import AnalysisResponse
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
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

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
    """Analyze a product against the user's ACTIVE shelf items (UC-06)."""
    try:
        comparison = compatibility_service.get_active_shelf_products(user_id)
        return compatibility_service.analyze(product_id, user_id, comparison)
    except Exception as e:
        print("Analysis Error:", e)
        raise HTTPException(status_code=500, detail="Failed to analyze product compatibility.")

class ItemOpenRequest(BaseModel):
    opened_date: str
    expiration_date: Optional[str] = None

@router.patch("/{item_id}/open")
async def mark_item_opened(item_id: str, req: ItemOpenRequest, user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("shelf_items").update({
            "opened_date": req.opened_date,
            "expiration_date": req.expiration_date,
            "usage_state": "active"  # Flips state to active upon opening
        }).eq("id", item_id).eq("user_id", user_id).execute()
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