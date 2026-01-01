from fastapi import FastAPI
from app.api.v1.query import router as query_router
from app.api.v1.analyze import router as analyze_router
from app.api.v1.retrieve_tables import router as retrieve_router




app = FastAPI(title="dbops-enterprise-copilot")

app.include_router(query_router)
app.include_router(analyze_router)
app.include_router(retrieve_router)

@app.get("/health")
def health():
    return {"ok": True}
