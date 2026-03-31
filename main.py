#main.py
from fastapi import FastAPI

app = FastAPI(title="Corporate Shuttle Backend")

@app.get("/")
def root():
    return {"message": "Backend Running 🚀"}