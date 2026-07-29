from fastapi import FastAPI

app = FastAPI(title="创懿量化交易平台", version="1.0.0")


@app.get("/health")
async def health():
    return {"ok": True}
