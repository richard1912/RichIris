"""Uvicorn entry point for RichIris NVR."""

import uvicorn

from app.config import get_bootstrap


def main() -> None:
    bootstrap = get_bootstrap()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=bootstrap.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
