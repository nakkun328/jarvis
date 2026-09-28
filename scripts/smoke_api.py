"""Check the actual ASGI app startup and health route."""

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings


def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        settings = Settings("test", "WARNING", root, root / "jarvis.sqlite3")
        with TestClient(create_app(settings)) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert settings.database_path.is_file()
    print("API smoke check passed")


if __name__ == "__main__":
    main()
