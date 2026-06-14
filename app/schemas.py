from pydantic import BaseModel
from typing import Optional, Dict, List

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

class QuizResultCreate(BaseModel):
    skinType: str
    scores: Dict[str, float] # Tells FastAPI to expect a JSON object of numbers

class ProductResponse(BaseModel):
    id: str
    brand: str
    name: str
    category: str
    ingredients: Optional[str] = None
    image_url: Optional[str] = None  

class ProductCreate(BaseModel):
    brand: str
    name: str
    category: str
    ingredients: Optional[str] = None
    image_url: Optional[str] = None 

class ShelfItemCreate(BaseModel):
    product_id: str  
    usage_state: str  
    opened_date: Optional[str] = None
    expiration_date: Optional[str] = None
    pao: Optional[int] = None

class Ingredient(BaseModel):
    id: str
    name: str
    benefits: Optional[str] = None
    good_for: Optional[str] = None
    bad_for: Optional[str] = None

class ProductIngredient(BaseModel):
    # This represents the bridge table. It holds the nested ingredient object.
    ingredients: Ingredient

class ProductDetail(ProductResponse): 
    product_ingredients: list[ProductIngredient] = []
class WarningAlert(BaseModel):
    alert_type: str  # "Biological" or "Chemical"
    severity: str    # "High", "Moderate", etc.
    message: str

class AnalysisResponse(BaseModel):
    is_safe: bool
    warnings: List[WarningAlert]

class SharedIngredient(BaseModel):
    id: str
    name: str
    benefits: Optional[str] = None

class CompareResponse(BaseModel):
    product_a: ProductDetail
    product_b: ProductDetail
    shared_ingredients: List[SharedIngredient]
    similarity_score: float  # Percentage (0 to 100)
    conflicts: List[WarningAlert]

class ChatMessage(BaseModel):
    role: str # 'user' or 'bot'
    text: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage]