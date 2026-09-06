import re
from pydantic import BaseModel, field_validator
from typing import Optional, Dict, List, Any, Literal

# Baumann 16-Type code: one letter from each axis pair (O/D, S/R, P/N, W/T).
# Public rather than underscore-prefixed: app/api/products.py imports it so the
# scoring function rejects exactly the codes the write paths refuse to store.
BAUMANN_PATTERN = re.compile(r"^[OD][SR][PN][WT]$")


def validate_baumann_skin_type(v: str) -> str:
    if not BAUMANN_PATTERN.match(v):
        raise ValueError("skin_type must be a 4-letter Baumann code, e.g. 'DSPT'")
    return v

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

    _validate_skin_type = field_validator("skinType")(validate_baumann_skin_type)

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

# Mirrors the shelf_item_state database enum, whose members are confirmed by the
# verification step of 0001_security_and_fk_cleanup.sql. Declared once and shared
# so the two request models cannot drift apart from each other or from the
# column. Without it any string was accepted, and Postgres rejected the write as
# a 500 for what was really a malformed request. BE-DEF-08.
UsageState = Literal["unopened", "active", "archived"]


class ShelfItemCreate(BaseModel):
    product_id: str
    usage_state: UsageState
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

class DuplicateMatch(BaseModel):
    product_id: str
    name: str
    brand: Optional[str] = None
    slug: Optional[str] = None
    similarity: float
    shared_actives: List[str] = []

class AnalysisResponse(BaseModel):
    is_safe: bool
    warnings: List[WarningAlert]
    # Advisory only - never derived from or folded into is_safe/warnings above.
    # A dupe is not a hazard; if it leaked into warnings it would flip is_safe
    # to false and trip the frontend's blocking "unsafe, are you sure?" gate on
    # AddProductModal.handleSave for the sole reason that the user owns
    # something similar. Defaults to [] so every existing response_model=
    # AnalysisResponse caller keeps working unchanged.
    duplicates: List[DuplicateMatch] = []

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
    # UC-19: the session a step belongs to is edited alongside its cadence.
    # Optional so existing callers that only send a frequency keep working.
    time_of_day: Optional[str] = None

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