"""Ingredient search and the paste matcher, for the submission form. Public."""

from fastapi import APIRouter, HTTPException, Query

from app.core.services import ingredient_lookup
from app.schemas import IngredientMatchRequest

router = APIRouter()


@router.get("/search")
async def search_ingredients(
    q: str = Query(..., min_length=1, max_length=60),
    limit: int = Query(8, ge=1, le=20),
):
    try:
        results = ingredient_lookup.search(q, limit, ingredient_lookup.load_ingredients())
        return {"results": results}
    except Exception as e:
        print("GET /ingredients/search error:", e)
        raise HTTPException(status_code=500, detail="Failed to search ingredients.")


@router.post("/match")
async def match_ingredients(body: IngredientMatchRequest):
    try:
        if not body.names:
            return {"matches": []}
        return {"matches": ingredient_lookup.match(body.names, ingredient_lookup.load_ingredients())}
    except Exception as e:
        print("POST /ingredients/match error:", e)
        raise HTTPException(status_code=500, detail="Failed to match ingredients.")
