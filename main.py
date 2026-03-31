#main.py
from fastapi import FastAPI
from app.auth import router as auth_router


app = FastAPI(title="Corporate Shuttle Backend")

@app.get("/")
def root():
    return {"message": "Backend Running 🚀"}
app.include_router(auth_router)