import asyncio
import os
import sys
import json

import httpx

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.db.connection import supabase
from app.core.services.catalog_service import upsert_product, upsert_ingredient
from app.core.services.image_service import fetch_and_store_product_image
from app.core.utils import create_slug


async def resolve_catalog_images(products: list[dict]) -> None:
    """Download each product's source image into our own Storage bucket.

    Mutates products in place, setting image_url to the stored public URL.
    source_image_url is where the picture came from; image_url is our copy, so
    the catalog never hotlinks a third party (see 0004_create_product_images_bucket.sql).

    fetch_and_store_product_image never raises and returns None on any failure,
    so a product whose image cannot be fetched simply keeps image_url unset and
    renders the frontend's placeholder.
    """
    pending = [p for p in products if p.get("source_image_url")]
    if not pending:
        return

    print(f"🖼️  Storing product images ({len(pending)} with a source)...")
    async with httpx.AsyncClient() as client:
        for prod in pending:
            slug = create_slug(prod["brand"], prod["name"])
            stored_url = await fetch_and_store_product_image(client, prod["source_image_url"], slug)
            if stored_url:
                prod["image_url"] = stored_url
                print(f"  ✅ {prod['brand']} - {prod['name']}")
            else:
                print(f"  ⚠️  Skipped {prod['brand']} - {prod['name']} (source unavailable or untrusted)")


def run_master_seed():
    print("🚀 Initiating Golden Master Seed...")
    base_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        with open(os.path.join(base_dir, "seeders", "catalog.json"), 'r') as file:
            catalog_data = json.load(file)
        with open(os.path.join(base_dir, "seeders", "seed_conflict.json"), 'r') as file:
            conflict_data = json.load(file)
    except FileNotFoundError as e:
        print(f"❌ Missing Seeder File: {e}")
        return

    # conflict rules are reference data defined entirely by seed_conflict.json,
    # so wiping and rebuilding them wholesale is correct - nothing else creates
    # them and nothing holds a foreign key into them.
    #
    # products/ingredients are upserted below rather than wiped: shelf_items and
    # routine_steps hold real user data and FK-reference products, so deleting
    # products would either fail outright or force deleting those user tables
    # too - which a seed script must never do.
    print("🧹 Rebuilding conflict-rule tables (catalog rows are upserted, not wiped)...")
    supabase.table("conflict_rules").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    supabase.table("category_conflict_rules").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()

    # 2. SEED GOLDEN CATALOG
    # Images are resolved before the upsert loop so each product row is written
    # once, already carrying its stored image_url.
    asyncio.run(resolve_catalog_images(catalog_data["products"]))

    print("📦 Upserting Golden Catalog...")
    ingredient_id_map = {}

    for prod in catalog_data["products"]:
        prod_id = upsert_product(prod)

        # Clear this product's ingredient links only, never the whole table:
        # products created outside this seeder (approved user submissions, the
        # OBF ingest script) own their own links, and wiping globally would
        # silently strip them without ever re-adding.
        supabase.table("product_ingredients").delete().eq("product_id", prod_id).execute()

        for ing_name in prod["ingredients"]:
            if ing_name not in ingredient_id_map:
                ingredient_id_map[ing_name] = upsert_ingredient(ing_name)

            supabase.table("product_ingredients").insert({
                "product_id": prod_id, "ingredient_id": ingredient_id_map[ing_name]
            }).execute()

    print(f"  ✅ Catalog Seeded: {len(catalog_data['products'])} Products & {len(ingredient_id_map)} Unique Ingredients.")

    # 3. SEED CONFLICT RULES
    print("📦 Mapping Chemical Conflict Matrix...")
    linked_conflicts = 0
    for rule in conflict_data.get("ingredient_conflicts", []):
        id_a = ingredient_id_map.get(rule["ingredient_a"])
        id_b = ingredient_id_map.get(rule["ingredient_b"])
        if id_a and id_b:
            supabase.table("conflict_rules").insert({
                "ingredient_a_id": id_a, "ingredient_b_id": id_b,
                "severity": rule["severity"], "warning_message": rule["warning_message"]
            }).execute()
            linked_conflicts += 1
        else:
            missing = rule["ingredient_a"] if not id_a else rule["ingredient_b"]
            print(f"  ⚠️  Skipped conflict rule {rule['ingredient_a']} / {rule['ingredient_b']}: '{missing}' is not in the seeded catalog.")

    for rule in conflict_data.get("category_conflicts", []):
        supabase.table("category_conflict_rules").insert({
            "group_a": rule["category_a"], "group_b": rule["category_b"],
            "severity": rule["severity"], "warning_message": rule["warning_message"]
        }).execute()

    total_rules = len(conflict_data.get('ingredient_conflicts', []))
    print(f"  ✅ Matrix Seeded: {linked_conflicts}/{total_rules} specific interactions and {len(conflict_data.get('category_conflicts', []))} structural group rules.")
    print("🎉 Application backend is fully primed and production-ready!")

if __name__ == "__main__":
    run_master_seed()
