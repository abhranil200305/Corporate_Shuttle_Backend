
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.auth import router as auth_router

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

# ---------------------------
# Healthcheck endpoint
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}
@app.get("/")
def root():
    return {"message": "Backend Running 🚀"}
app.include_router(auth_router)
