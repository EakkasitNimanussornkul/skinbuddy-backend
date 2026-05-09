from app.db.connection import supabase

class UserRepository:
    @staticmethod
    def get_or_create_line_user(line_id: str, name: str, picture: str):
        # 1. Check if the user is already in our database
        existing_user = supabase.table("users").select("*").eq("line_id", line_id).execute()
        
        if existing_user.data:
            return existing_user.data[0]
            
        # 2. If not, insert them
        new_user = {
            "line_id": line_id,
            "display_name": name,
            "picture_url": picture
        }
        result = supabase.table("users").insert(new_user).execute()
        
        if result.data:
            return result.data[0]
        return None

user_repo = UserRepository()