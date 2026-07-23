from pydantic import BaseModel
from typing import Optional, Dict, List

class LineAuthRequest(BaseModel):
    code: str

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
    pao: Optional[int] = None

class ProductCreate(BaseModel):
    brand: str
    name: str
    category: str
    ingredients: Optional[str] = None
    image_url: Optional[str] = None 
    pao: Optional[int] = None

class ShelfItemCreate(BaseModel):
    product_id: str  
    usage_state: str  
    opened_date: Optional[str] = None
    expiration_date: Optional[str] = None
    pao: Optional[int] = None

class IngredientConcern(BaseModel):
    id: Optional[str] = None
    concern_title: Optional[str] = None
    concern_name: Optional[str] = None
    name: Optional[str] = None
    concern_description: Optional[str] = None
    target_profile: Optional[str] = None
    severity: Optional[str] = 'Moderate'

    class Config:
        from_attributes = True

class Ingredient(BaseModel):
    id: str
    name: str
    benefits: Optional[str] = None
    good_for: Optional[str] = None
    bad_for: Optional[str] = None
    functional_group: Optional[str] = None
    safety_warning: Optional[str] = None
    is_irritant: Optional[bool] = False
    ingredient_concerns: List[IngredientConcern] = []

    class Config:
        from_attributes = True

class ProductIngredient(BaseModel):
    # This represents the bridge table. It holds the nested ingredient object.
    ingredients: Ingredient

    class Config:
        from_attributes = True

class ProductConcern(BaseModel):
    id: Optional[str] = None
    concern_name: Optional[str] = None
    name: Optional[str] = None

    class Config:
        from_attributes = True

class ProductDetail(ProductResponse): 
    product_ingredients: List[ProductIngredient] = []
    product_concerns: List[ProductConcern] = []
    concerns: Optional[List[str]] = []

    class Config:
        from_attributes = True

class ShelfItemResponse(BaseModel):
    id: str
    user_id: str
    product_id: str
    usage_state: str
    opened_date: Optional[str] = None
    expiration_date: Optional[str] = None
    pao: Optional[int] = None
    archive_outcome: Optional[str] = None
    archive_notes: Optional[str] = None
    archived_at: Optional[str] = None
    products: Optional[ProductDetail] = None

    class Config:
        from_attributes = True

class WarningAlert(BaseModel):
    alert_type: str  # "Biological" or "Chemical" or "Skin Type Conflict"
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