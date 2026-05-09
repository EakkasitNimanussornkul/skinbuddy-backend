from pydantic import BaseModel
from typing import Optional

# This tells FastAPI to expect a JSON body with a 'code' string from the frontend
class LineAuthRequest(BaseModel):
    code: str

# You will also need these for the return response in auth.py!
class UserBase(BaseModel):
    id: str
    name: str
    avatar: Optional[str] = None

class TokenResponse(BaseModel):
    message: Optional[str] = None
    access_token: str
    token_type: str = "bearer"
    user: UserBase