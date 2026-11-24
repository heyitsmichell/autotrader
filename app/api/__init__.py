"""HTTP control plane: FastAPI app factory plus one APIRouter per area."""

from .app import create_app  # noqa: F401

__all__ = ["create_app"]
