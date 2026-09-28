"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from backend.core.config import Settings
from backend.core.database import Database
from backend.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        database.initialize()
        app.state.database = database
        yield

    app = FastAPI(title="JARVIS", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            healthy = database.check()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        if not healthy:
            raise HTTPException(status_code=503, detail="Database unavailable")
        return {"status": "ok"}

    return app


app = create_app()
