from pydantic import BaseModel
from typing import Optional, Dict

class LineAuthRequest(BaseModel):
    code: str

# Updated to match the frontend expectations!
class UserBase(BaseModel):
    id: str
    name: str
    picture: Optional[str] = None # Changed from avatar to picture
    skin_type: Optional[str] = None 

class TokenResponse(BaseModel):
    message: Optional[str] = None
    access_token: str
    token_type: str = "bearer"
    user: UserBase

# --- NEW: Schema for the Quiz ---
class QuizResultCreate(BaseModel):
    skinType: str
    scores: Dict[str, float] # Tells FastAPI to expect a JSON object of numbers

class ShelfItemCreate(BaseModel):
    brand: str
    name: str           # Changed from product_name to match DB
    category: str
    status: str         # Changed from routine to match DB
    opened_date: Optional[str] = None      # Added for DB
    expiration_date: Optional[str] = None  # Added for DB

class ShelfItemResponse(ShelfItemCreate):
    id: str
    user_id: str
    created_at: str