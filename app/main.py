import os
from fastapi import Depends, FastAPI
from fastapi.concurrency import asynccontextmanager
from app.api import health
from app.api import auth
from app.api import quiz
from app.api import shelf
from app.api import products
from app.api import chat
from app.api import routine
from app.api import analysis
from app.api import notifications
from app.api import meta
from app.api import ingredients
from app.api import submissions
from app.api import consent
from app.api import account_deletion
from app.core.services.consent_gate import require_health_consent_for_checkin
from app.core.services.rpc_errors import BodyHTTPException, body_http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    print("starting up...")
    
    yield
    
    # --- SHUTDOWN ---
    print("Shutting down...")

# Create the FastAPI app and run with lifespan
app = FastAPI(title="Flood Backend API", lifespan=lifespan)

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
]

# Compress JSON bodies for clients that send Accept-Encoding: gzip (/products/search is
# 485 kB for 34 products, 66 kB gzipped). Level 6, not Starlette's default 9: 5.8 ms
# against 9.2 ms for the same body. Added BEFORE CORS so CORS is the outer layer and
# its headers sit on every response, compressed or not.
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,    # Allows your Vue app to connect
    allow_credentials=True,   # Allows cookies/tokens
    allow_methods=["*"],      # ALLOWS ALL METHODS
    allow_headers=["*"],      # Allows all headers
)

# Include the router
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(account_deletion.router, prefix="/auth", tags=["auth"])
app.include_router(consent.router, prefix="/consent", tags=["Consent"])
app.include_router(quiz.router, prefix="/quiz", tags=["Quiz"])
app.include_router(shelf.router, prefix="/shelf", tags=["Shelf"])
app.include_router(products.router, prefix="/products", tags=["Products"])
app.include_router(chat.router, prefix="/chat", tags=["Chat"])
app.include_router(routine.router, prefix="/routine", tags=["Routine"])
# POST /analysis/log needs a current health consent. The gate is attached here, before the
# route runs, so analysis.py stays untouched; it gates POST only (app/core/services/consent_gate.py).
app.include_router(analysis.router, prefix="/analysis", tags=["Analysis"],
                   dependencies=[Depends(require_health_consent_for_checkin)])
app.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
app.include_router(meta.router, prefix="/meta", tags=["Meta"])
app.include_router(ingredients.router, prefix="/ingredients", tags=["Ingredients"])
app.include_router(submissions.router, prefix="/submissions", tags=["Submissions"])

# The 409 duplicate answer carries "candidates" beside "detail"; this handler
# sends such a body as it is rather than wrapping it in {"detail": ...}.
app.add_exception_handler(BodyHTTPException, body_http_exception_handler)
