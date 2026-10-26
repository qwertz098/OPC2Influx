"""FastAPI dependency injection helpers."""
from __future__ import annotations

from fastapi import Request

from app.state import RuntimeState


def get_state(request: Request) -> RuntimeState:
    return request.app.state.runtime
