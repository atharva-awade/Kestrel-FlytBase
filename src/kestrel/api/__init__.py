"""FastAPI surface: REST for state, SSE for streaming, a gated route for actions."""

from kestrel.api.main import app

__all__ = ["app"]
