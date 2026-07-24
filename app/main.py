import os
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from app.api import health
from app.api import auth
from app.api import quiz
from app.api import shelf
from app.api import products
from app.api import chat
from app.api import routine
from app.api import notifications
from fastapi.middleware.cors import CORSMiddleware


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
app.include_router(quiz.router, prefix="/quiz", tags=["Quiz"])
app.include_router(shelf.router, prefix="/shelf", tags=["Shelf"])
app.include_router(products.router, prefix="/products", tags=["Products"])
app.include_router(chat.router, prefix="/chat", tags=["Chat"])
app.include_router(routine.router, prefix="/routine", tags=["Routine"])
app.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])