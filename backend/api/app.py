"""FastAPI application factory and health endpoints."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from backend.core.config import Settings
from backend.core.database import Database
from backend.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging(settings.log_level)
        database.initialize()
        logging.getLogger(__name__).info("JARVIS backend started")
        yield

    app = FastAPI(
        title="JARVIS",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> dict[str, str]:
        if not database.is_ready():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ok"}

    return app


app = create_app()
