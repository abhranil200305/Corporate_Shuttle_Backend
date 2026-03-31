# app/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import the driver signup router
from app.controllers.driverauth.signup import router as driverauth_router

# Create FastAPI app
app = FastAPI(title="Kolkata Corporate Shuttle - Driver API")

# ---------------------------
# Allow all origins (everyone can access)
# ---------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Include driver signup router
# ---------------------------
app.include_router(driverauth_router)

# ---------------------------
# Healthcheck endpoint
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}