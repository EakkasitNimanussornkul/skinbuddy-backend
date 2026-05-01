# Functions to save new users or find existing ones
from sqlalchemy.orm import Session
from app.db.models import User

def create_user(db: Session, user_id: str, email: str, username: str):
    # Package the data
    new_user = User(
        id=user_id,
        email=email,
        username=username
    )
    
    # Save to the database
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    return new_user