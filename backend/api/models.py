"""GET /api/models: the owner-selectable chat models. Names and availability only, no secrets."""

from fastapi import APIRouter

from backend.providers.choices import ModelRegistry


def create_models_router(registry: ModelRegistry | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models")
    def list_models() -> list[dict[str, str | bool]]:
        # No registry (JARVIS_MODEL_CHOICES unset): an empty list, which hides the selector.
        return [option.as_dict() for option in registry.options()] if registry else []

    return router
