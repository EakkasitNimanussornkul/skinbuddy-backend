import re
from uuid import UUID
from functools import partial
from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints,
                      field_validator, model_validator)
from typing import Annotated, Optional, Dict, List, Any, Literal

from app.core.input_safety import check_public_url, clean_text

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

# A published source a claim rests on (migration 0009). Declared here so every
# response model that nests ingredients, concerns or conflict pairs carries it:
# a model that does not declare a field drops it silently on the way out.
class SourceRef(BaseModel):
    id: str
    title: str
    publisher: Optional[str] = None
    url: Optional[str] = None
    source_type: str        # regulatory_register | safety_review | chemical_database | peer_reviewed | reference_book | product_database
    accessed_on: Optional[str] = None
    notes: Optional[str] = None


class IngredientSourceLink(BaseModel):
    claim: str              # which stored claim the source backs: function | benefits | good_for | bad_for
    sources: Optional[SourceRef] = None


class ConcernSourceLink(BaseModel):
    sources: Optional[SourceRef] = None


class ProductSourceLink(BaseModel):
    claim: str              # which product fact the source backs: listing | price | image | description
    sources: Optional[SourceRef] = None


class IngredientConcern(BaseModel):
    id: Optional[str] = None
    concern_title: Optional[str] = None
    concern_description: Optional[str] = None
    target_profile: Optional[str] = None
    severity: Optional[str] = 'Moderate'
    concern_sources: List[ConcernSourceLink] = []

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
    ingredient_sources: List[IngredientSourceLink] = []

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

class MatchBreakdown(BaseModel):
    """The working behind skin_match_score, so the page can show how it was made."""
    helpful: int              # ingredients that suit a trait of the user's code
    concerns: int             # ingredients flagged for a trait of the code
    concern_weight: float     # those concerns summed by grade: High 1.0, Medium 0.6, Low 0.3
    considered: int           # ingredients that said anything about the code (helpful, concern, or both)
    total_ingredients: int    # every ingredient in the product
    limited: bool             # fewer than 3 considered: the score rests on too little to lean on
    # Of `considered`, how many are backed by a source on every side they count:
    # a good_for source for a helpful one; a bad_for source, or a source on the
    # concern that graded it, for a concern; both when counted on both sides.
    verified_considered: int = 0


class ProductDetail(ProductResponse):
    description: Optional[str] = None
    source_url: Optional[str] = None   # the product's page in Open Beauty Facts, when that is where it came from
    product_sources: List[ProductSourceLink] = []   # migration 0010
    price_thb: Optional[float] = None
    price_usd: Optional[float] = None
    product_ingredients: List[ProductIngredient] = []
    concerns: Optional[List[str]] = []
    
    safety_flags: Optional[SafetyFlags] = None
    skin_match_score: Optional[float] = None
    # None exactly when skin_match_score is None for want of a skin type.
    match_breakdown: Optional[MatchBreakdown] = None
    match_reasons: Optional[List[str]] = []
    caution_reasons: Optional[List[str]] = []

    # Migration 0013. Declared so GET /products/compare, which serialises
    # products through this model, does not drop them: a field a response model
    # does not declare is silently removed on the way out.
    benefits: Optional[List[str]] = None      # the benefits an admin published; None when unset
    good_for: Optional[List[str]] = None      # concern tags (CONCERN_TAGS); None when unset
    pao_months: Optional[int] = None          # period after opening: 6, 12 or 24
    updated_at: Optional[str] = None          # PATCH /products/{id} needs it back exactly as sent

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
class ConflictDetail(BaseModel):
    """One ingredient pair behind a conflict with another product."""
    alert_type: str
    severity: str
    ingredient: str                               # the checked product's ingredient(s)
    conflicting_ingredient: Optional[str] = None  # the other product's ingredient
    message: str
    sources: List[SourceRef] = []                 # what the rule behind this pair rests on


class SkinTypeReason(BaseModel):
    """Why one ingredient suits the caller's skin type poorly, for one trait."""
    trait: str                          # the bad_for entry that matched, e.g. "Extremely Dry Skin (D)"
    title: Optional[str] = None         # from ingredient_concerns, when one covers this trait
    description: Optional[str] = None
    severity: str                       # the concern's, on the High/Medium/Low scale; "High" when none
    sources: List[SourceRef] = []       # what that concern rests on; [] when none, or no concern


class WarningAlert(BaseModel):
    alert_type: str  # "Skin Type Conflict", "Chemical Interaction Warning", "Active Routine Clash"
    severity: str    # "High", "Medium", "Low"
    message: str
    # Set on a conflict with another product; None on a skin-type alert, which
    # is about the product itself. Every pair that product clashes on is listed
    # in details, most severe first. Both are additive: a caller that reads only
    # the three fields above sees one warning per product, with a summary message.
    conflicting_product: Optional[str] = None
    details: List[ConflictDetail] = []
    # Set on a Skin Type Conflict only: one entry per trait of the caller's
    # Baumann code the ingredient is flagged for, with the explanation from
    # ingredient_concerns when one exists. The warning's severity is the most
    # severe entry's.
    reasons: List[SkinTypeReason] = []

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
    # UC-22: which session ("AM"/"PM") is being ticked. A product set to "both"
    # is one step but two daily tasks, so the session must be recorded to tell
    # "did the morning" from "did the evening". Optional for back-compat: an
    # AM- or PM-only step infers it; a "both" step without one marks the day.
    time_of_day: Optional[str] = None

# 10. WEEKLY SKIN ANALYSIS LOG SCHEMAS
class SymptomEntry(BaseModel):
    symptom: str
    severity: int

class SkinLogCreate(BaseModel):
    symptoms: List[SymptomEntry]
    affected_areas: List[str] = []
    notes: Optional[str] = None
    week_start: Optional[str] = None


# 11. PRODUCT SUBMISSIONS, ADMIN REVIEW AND PRODUCT EDITING
#
# The lists below are the contract's (contracts-submissions-admin.md, approved
# 2026-10-04). Each is defined once: GET /meta/* serves it and the request
# models below validate against it, so what the page offers and what the API
# accepts cannot drift apart.

CATEGORIES = ("Cleansers", "Toners", "Serums", "Treatments", "Moisturizers",
              "Exfoliators", "Sun Care", "Masks", "Eye Care")
CONCERN_TAGS = ("Dry skin", "Dehydrated", "Sensitive", "Oily", "Acne-prone",
                "Dark spots", "Dullness", "Fine lines", "Redness")
INGREDIENT_ROLES = ("Moisturising", "Soothing", "Barrier support", "Exfoliating",
                    "Brightening", "Preservative", "Not sure")
# product_sources.claim's check constraint (migration 0010).
PRODUCT_SOURCE_CLAIMS = ("listing", "description", "price", "image")
# sources.source_type's check constraint (migration 0009).
SOURCE_TYPES = ("regulatory_register", "safety_review", "chemical_database",
                "peer_reviewed", "reference_book", "product_database")
PAO_MONTHS = (6, 12, 24)

Category = Literal[CATEGORIES]
ConcernTag = Literal[CONCERN_TAGS]
IngredientRole = Literal[INGREDIENT_ROLES]
ProductSourceClaim = Literal[PRODUCT_SOURCE_CLAIMS]
SourceType = Literal[SOURCE_TYPES]
PaoMonths = Literal[PAO_MONTHS]

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Paths the upload routes create: <folder>/<uuid>.<ext>. Anything else is not
# one of our uploads and is refused, so a payload cannot point a product page at
# an arbitrary object in the bucket.
SUBMISSION_IMAGE_PATH = re.compile(rf"^submissions/{_UUID}\.(jpg|png|webp)$")
PRODUCT_IMAGE_PATH = re.compile(rf"^(submissions|products)/{_UUID}\.(jpg|png|webp)$")


def _http_url(value: str) -> str:
    """An http(s) link fit to publish: see check_public_url in
    app/core/input_safety.py. The backend never fetches these URLs; the rules
    keep junk and internal links (localhost, private IPs, user:pass@) off
    product pages. The value is stored as sent, after the whitespace trim."""
    return check_public_url(value)


# fullmatch, not match: "$" also matches before a trailing newline, so
# match() let "submissions/<uuid>.jpg" plus a newline through.
def _submission_image_path(value: str) -> str:
    if not SUBMISSION_IMAGE_PATH.fullmatch(value):
        raise ValueError("must be an image_path returned by POST /submissions/images")
    return value


def _product_image_path(value: str) -> str:
    if not PRODUCT_IMAGE_PATH.fullmatch(value):
        raise ValueError("must be an image_path returned by an upload route")
    return value


def _text(max_length: int, min_length: int = 1, multiline: bool = False):
    """Submitted text: invisible and control characters cleaned out first
    (clean_text in app/core/input_safety.py), then trimmed and length-checked,
    so a name made only of zero-width spaces is refused as empty. multiline
    keeps line breaks and tabs (notes, descriptions)."""
    return Annotated[str,
                     StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length),
                     BeforeValidator(partial(clean_text, multiline=multiline))]


HttpUrlText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
                        AfterValidator(_http_url)]
SubmissionImagePath = Annotated[str, AfterValidator(_submission_image_path)]
ProductImagePath = Annotated[str, AfterValidator(_product_image_path)]
Price = Annotated[float, Field(ge=0)]


def _refuse_explicit_nulls(model: BaseModel, fields) -> None:
    """A field that may be left out but, when sent, may not be null."""
    for name in fields:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null")


class NewIngredientDetails(BaseModel):
    """What a user knows about an ingredient that is not in our list. All optional."""
    model_config = ConfigDict(extra="forbid")

    roles: Annotated[List[IngredientRole], Field(max_length=len(INGREDIENT_ROLES))] = []
    known_for: Optional[_text(200, 0)] = None
    source_url: Optional[HttpUrlText] = None


class SubmissionIngredient(BaseModel):
    """{"ingredient_id": uuid} for one picked from our list, or {"new_name": str,
    "details"?: {...}} for one typed in. Exactly one of the two."""
    model_config = ConfigDict(extra="forbid")

    ingredient_id: Optional[UUID] = None
    new_name: Optional[_text(120)] = None
    details: Optional[NewIngredientDetails] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.ingredient_id is None) == (self.new_name is None):
            raise ValueError("each ingredient needs exactly one of ingredient_id or new_name")
        if self.ingredient_id is not None and self.details is not None:
            raise ValueError("details go with new_name, not with ingredient_id")
        return self

    def stored(self) -> Dict[str, Any]:
        """The item as it is stored in the payload: only the keys that were set."""
        return self.model_dump(mode="json", exclude_none=True)


class SubmissionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrlText
    title: _text(120)
    claims: Annotated[List[ProductSourceClaim], Field(min_length=1, max_length=len(PRODUCT_SOURCE_CLAIMS))]


SubmissionIngredients = Annotated[List[SubmissionIngredient], Field(min_length=1, max_length=100)]
Benefits = Annotated[List[_text(80)], Field(max_length=8)]
GoodFor = Annotated[List[ConcernTag], Field(max_length=len(CONCERN_TAGS))]
SubmissionSources = Annotated[List[SubmissionSource], Field(max_length=5)]


class SubmissionCreate(BaseModel):
    """POST /submissions. The whole body is stored as the submission's payload."""
    model_config = ConfigDict(extra="forbid")

    name: _text(200)
    brand: _text(200)
    category: Category
    image_path: Optional[SubmissionImagePath] = None
    ingredients: SubmissionIngredients            # in the order printed on the pack
    price_thb: Optional[Price] = None
    price_usd: Optional[Price] = None
    pao_months: Optional[PaoMonths] = None
    benefits: Benefits = []
    good_for: GoodFor = []
    sources: SubmissionSources = []
    note: Optional[_text(1000, 0, multiline=True)] = None

    def stored_payload(self) -> Dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["ingredients"] = [item.stored() for item in self.ingredients]
        return payload


class SubmissionEdit(BaseModel):
    """PATCH /submissions/admin/{id}: the POST body with every field optional.
    Only the fields sent are saved to edited_payload."""
    model_config = ConfigDict(extra="forbid")

    name: Optional[_text(200)] = None
    brand: Optional[_text(200)] = None
    category: Optional[Category] = None
    image_path: Optional[SubmissionImagePath] = None
    ingredients: Optional[SubmissionIngredients] = None
    price_thb: Optional[Price] = None
    price_usd: Optional[Price] = None
    pao_months: Optional[PaoMonths] = None
    benefits: Optional[Benefits] = None
    good_for: Optional[GoodFor] = None
    sources: Optional[SubmissionSources] = None
    note: Optional[_text(1000, 0, multiline=True)] = None

    @model_validator(mode="after")
    def _no_nulls_where_a_value_is_needed(self):
        _refuse_explicit_nulls(self, ("name", "brand", "category", "ingredients",
                                      "benefits", "good_for", "sources"))
        return self

    def stored_edits(self) -> Dict[str, Any]:
        edits = self.model_dump(mode="json", exclude_unset=True)
        if self.ingredients is not None:
            edits["ingredients"] = [item.stored() for item in self.ingredients]
        return edits


class NewIngredientDecision(BaseModel):
    """One admin decision on a new ingredient. Its rules (one decision per new
    name, a decision from the three, a real position) are checked inside
    approve_submission(), so the API and the SQL cannot disagree about them."""
    model_config = ConfigDict(extra="allow")

    position: Optional[int] = None
    decision: Optional[str] = None
    functional_group: Optional[str] = None
    benefits: Optional[str] = None


class ApproveRequest(BaseModel):
    """POST /submissions/admin/{id}/approve. Passed to approve_submission()
    as sent: only the keys the admin sent, nothing added."""
    model_config = ConfigDict(extra="allow")

    publish_benefits: List[str] = []
    publish_good_for: List[str] = []
    # Checked by the same link rules as the submission's own sources (422 with
    # loc ["body", "publish_source_urls", i]), and passed on unchanged.
    publish_source_urls: List[Annotated[str, AfterValidator(check_public_url)]] = []
    new_ingredients: List[NewIngredientDecision] = []

    def as_sent(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_unset=True)


class RejectRequest(BaseModel):
    review_notes: Optional[_text(1000, 0, multiline=True)] = None


class IngredientMatchRequest(BaseModel):
    names: Annotated[List[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=100)]


class ProductIngredientRef(BaseModel):
    """{"ingredient_id": uuid} or {"new_name": str}: exactly one."""
    model_config = ConfigDict(extra="forbid")

    ingredient_id: Optional[UUID] = None
    new_name: Optional[_text(120)] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.ingredient_id is None) == (self.new_name is None):
            raise ValueError("each ingredient needs exactly one of ingredient_id or new_name")
        return self


class ProductSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrlText
    title: _text(120)
    publisher: Optional[_text(200)] = None
    source_type: Optional[SourceType] = None      # product_database when left out
    claims: Annotated[List[ProductSourceClaim], Field(min_length=1, max_length=len(PRODUCT_SOURCE_CLAIMS))]


class ProductPatch(BaseModel):
    """PATCH /products/{id}. Every field optional except updated_at, the value
    the admin loaded, which is passed to the database exactly as received: it
    carries microseconds, and any reformatting would read as a stale edit."""
    model_config = ConfigDict(extra="forbid")

    updated_at: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    name: Optional[_text(200)] = None
    brand: Optional[_text(200)] = None
    category: Optional[Category] = None
    description: Optional[_text(2000, 0, multiline=True)] = None
    price_thb: Optional[Price] = None
    price_usd: Optional[Price] = None
    pao_months: Optional[PaoMonths] = None
    image_path: Optional[ProductImagePath] = None
    benefits: Optional[Benefits] = None
    good_for: Optional[GoodFor] = None
    ingredients: Optional[Annotated[List[ProductIngredientRef], Field(min_length=1, max_length=100)]] = None
    sources: Optional[Annotated[List[ProductSourceIn], Field(max_length=10)]] = None

    @model_validator(mode="after")
    def _no_nulls_where_a_value_is_needed(self):
        _refuse_explicit_nulls(self, ("name", "brand", "category", "ingredients", "sources"))
        return self

    def patch_fields(self) -> Dict[str, Any]:
        """The fields sent, minus updated_at, with list items reduced to the keys
        that were set. image_path is left for the route to turn into a URL."""
        patch = self.model_dump(mode="json", exclude_unset=True)
        patch.pop("updated_at", None)
        if self.ingredients is not None:
            patch["ingredients"] = [i.model_dump(mode="json", exclude_none=True) for i in self.ingredients]
        if self.sources is not None:
            patch["sources"] = [s.model_dump(mode="json", exclude_none=True) for s in self.sources]
        return patch
