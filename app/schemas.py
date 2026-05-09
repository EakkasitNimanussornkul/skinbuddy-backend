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