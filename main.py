from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager


@asynccontextmanager
async def lifespan(app:FastAPI):
    print("Starting the apps..............")
    yield
    print("ending the app.................")

#app = FastAPI(title="Corporate Shuttle Backend")
app = FastAPI(title="KBSA Project", lifespan=lifespan)
@app.get("/")
def root():
    return {"message": "Backend Running 🚀"}
