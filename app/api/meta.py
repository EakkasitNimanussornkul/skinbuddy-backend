"""Fixed lists the submission and admin pages are built from. Public.

The categories and concern tags are the same tuples the request models in
app/schemas.py validate against, so the page can never offer a value the API
would refuse.
"""

from fastapi import APIRouter, HTTPException

from app.core import consent
from app.core.services import ingredient_lookup
from app.schemas import CATEGORIES, CONCERN_TAGS

router = APIRouter()


@router.get("/categories")
async def get_categories():
    return {"categories": list(CATEGORIES)}


@router.get("/concern-tags")
async def get_concern_tags():
    return {"concern_tags": list(CONCERN_TAGS)}


# Public, like the two lists above. Only the admin's approve screen needs it
# (a with_details ingredient's functional_group must be one of these, exactly),
# but every value is already public through GET /ingredients/search and every
# product page, so an admin gate would protect nothing.
@router.get("/functional-groups")
async def get_functional_groups():
    try:
        return {"functional_groups": ingredient_lookup.functional_groups()}
    except Exception as e:
        print("GET /meta/functional-groups error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch functional groups.")


# Public, so the /privacy and /terms pages, which anyone can open without
# signing in (LINE's user data policy 2.4), can show the version the backend is
# asking people to agree to without a copy that could drift. These are the same
# constants GET /auth/me puts in `consent.current_terms_version` and
# `current_health_version`; bumping one in app/core/consent.py changes both.
@router.get("/policy-versions")
async def get_policy_versions():
    return {"terms_version": consent.TERMS_VERSION, "health_version": consent.HEALTH_CONSENT_VERSION}
