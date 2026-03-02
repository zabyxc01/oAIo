"""
Example extension backend — FastAPI router mounted at /extensions/example
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
def ping():
    return {"extension": "example", "status": "ok"}


@router.get("/info")
def info():
    return {
        "name": "example",
        "description": "Demonstrates the oAIo extension API",
        "endpoints": ["/extensions/example/ping", "/extensions/example/info"],
    }
