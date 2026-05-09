from fastapi import APIRouter, Depends, HTTPException
from app.db.connection import supabase
from app.core.services.token import get_current_user_id

router = APIRouter()

@router.get("/search")
async def search_products(q: str = "", user_id: str = Depends(get_current_user_id)):
    try:
        # If the search is empty, return an empty list
        if not q:
            return []
            
        # This searches for the query in BOTH the name and brand columns, ignoring capitalization (ilike)
        # limit to 20 so the frontend dropdown doesn't lag
        response = supabase.table("products") \
            .select("*") \
            .or_(f"name.ilike.%{q}%,brand.ilike.%{q}%") \
            .limit(20) \
            .execute()
            
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database search error: {str(e)}")