from pydantic import BaseModel
from typing import Optional, Dict, List, Any

# 1. AUTHENTICATION SCHEMAS

class LineAuthRequest(BaseModel):
    code: str

class UserBase(BaseModel):
    id: str
    name: str
    picture: Optional[str] = None  # User profile avatar URL
    skin_type: Optional[str] = None 

class TokenResponse(BaseModel):
    message: Optional[str] = None
    access_token: str
    token_type: str = "bearer"
    user: UserBase

# 2. QUIZ & SKIN PROFILE SCHEMAS
class QuizResultCreate(BaseModel):
    skinType: str
    scores: Dict[str, float] 

# 3. INGREDIENT & CONCERN SUB-MODELS
class IngredientConcern(BaseModel):
    id: Optional[str] = None
    concern_title: Optional[str] = None
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
    ingredient_concerns: List[IngredientConcern] = []

    class Config:
        from_attributes = True

class ProductIngredient(BaseModel):
    ingredients: Ingredient

    class Config:
        from_attributes = True

# 4. SAFETY & COMPOSITION FLAGS
class SafetyFlags(BaseModel):
    """Dynamic backend flags computed by ingredientcheck_service"""
    alcohol_free: bool = True
    fragrance_free: bool = True
    paraben_free: bool = True
    silicone_free: bool = True
    sulfate_free: bool = True
    vegan: bool = True
    fungal_safe: bool = True

    class Config:
        from_attributes = True

# 5. PRODUCT & CATALOG SCHEMAS

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

class ProductDetail(ProductResponse): 
    description: Optional[str] = None
    price_thb: Optional[float] = None
    price_usd: Optional[float] = None
    product_ingredients: List[ProductIngredient] = []
    concerns: Optional[List[str]] = []
    
    safety_flags: Optional[SafetyFlags] = None
    skin_match_score: Optional[float] = None
    match_reasons: Optional[List[str]] = []
    caution_reasons: Optional[List[str]] = []

    class Config:
        from_attributes = True


# 6. DIGITAL SHELF / INVENTORY SCHEMAS
class ShelfItemCreate(BaseModel):
    product_id: str  
    usage_state: str  
    opened_date: Optional[str] = None
    expiration_date: Optional[str] = None
    pao: Optional[int] = None

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

# 7. SAFETY ANALYSIS & COMPARISON SCHEMAS
class WarningAlert(BaseModel):
    alert_type: str  # "Biological", "Chemical", "Category Clash", etc.
    severity: str    # "High", "Moderate", "Low"
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
    similarity_score: float  # Percentage (0.0 to 100.0)
    conflicts: List[WarningAlert]

# 8. AI CHATBOT SCHEMAS
class ChatMessage(BaseModel):
    role: str  # 'user' or 'bot'
    text: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage]

# 9. ROUTINE TRACKER SCHEMAS
class RoutineGenerateRequest(BaseModel):
    followup_answers: str = ""

class ProposedStep(BaseModel):
    product_id: str
    step_order: int
    time_of_day: str = "both"  # "AM" | "PM" | "both"
    frequency: str = "daily"    # "daily" | "3x_week" | "2x_week" | "weekly"

class ApplyRoutineRequest(BaseModel):
    steps: List[ProposedStep]

class RoutineStepCreate(BaseModel):
    product_id: str
    frequency: str = "daily"
    time_of_day: str = "both"
    shelf_item_id: Optional[str] = None

class FrequencyUpdateRequest(BaseModel):
    frequency: str

class ReorderRequest(BaseModel):
    step_ids: List[str]

class CompleteStepRequest(BaseModel):
    period_key: Optional[str] = None

# 10. WEEKLY SKIN ANALYSIS LOG SCHEMAS
class SymptomEntry(BaseModel):
    symptom: str
    severity: int

class SkinLogCreate(BaseModel):
    symptoms: List[SymptomEntry]
    affected_areas: List[str] = []
    notes: Optional[str] = None
    week_start: Optional[str] = None