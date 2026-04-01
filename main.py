from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.admin.endpoints.router import router as admin_router
from app.auth import router as auth_router
from sqlalchemy import text   # ✅ FIX ADDED




from app.driver.driverprofile import router as driverprofile_router
from app.db.database import engine   # 👈 import engine
from app.driver.driverprofileshow import router as driverprofileshow_router


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
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(driverprofile_router)

app.include_router(driverprofileshow_router)

# ---------------------------
# Healthcheck endpoint
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}
# ---------------------------
# STARTUP EVENT (🔥 ADD THIS)
# ---------------------------
@app.on_event("startup")
async def startup_db_check():
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        print("✅ Database connected successfully")
    except Exception as e:
        print("❌ Database connection failed:", e)

@app.get("/")
def root():
    return {"message": "Backend Running 🚀"}


app.include_router(auth_router)
