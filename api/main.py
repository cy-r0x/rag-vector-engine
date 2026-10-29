from fastapi import FastAPI

app = FastAPI(title="rag-vector-engine")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "rag-vector-engine", "status": "ok"}
