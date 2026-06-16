"""ASGI entry point: `uvicorn app.main:app`."""

from app.module import app

__all__ = ["app"]
