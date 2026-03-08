"""Uvicorn entry point for RichIris NVR."""

import uvicorn

from app.config import get_config


def main() -> None:
    config = get_config()
    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
