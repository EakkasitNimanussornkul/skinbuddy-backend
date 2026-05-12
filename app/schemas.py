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

# --- NEW: Schema for Product Search ---
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
    status: str
    opened_date: Optional[str] = None
    expiration_date: Optional[str] = None

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
    # Inherits id, brand, name, image_url from your existing ProductResponse!
    product_ingredients: list[ProductIngredient] = []