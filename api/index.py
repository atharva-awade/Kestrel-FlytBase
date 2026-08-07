"""Vercel serverless FastAPI entrypoint."""

from kestrel.api.main import app

__all__ = ["app"]
