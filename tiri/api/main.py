"""FastAPI application factory.

`create_app()` wires the provider container into `app.state` and registers
the routers. `app = create_app()` at module load is the default entry point
for uvicorn (`uvicorn tiri.api.main:app`).

RoomEngine and RoomManager are constructed per request from
`app.state.container` — they are lightweight and hold no state beyond their
provider references.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tiri.config import Config, ConfigurationError
from tiri.container import build_container
from tiri.engine.room_engine import RoomNotFoundError
from tiri.api.mcp import server as mcp_server
from tiri.api.routes import conversations, feedback, management


_log = logging.getLogger("tiri.api")


def create_app(
    cfg: Config | None = None,
    container: dict[str, Any] | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Both `cfg` and `container` can be injected (test path). In production
    the default is `Config.load()` + `build_container(cfg)`.
    """
    if cfg is None:
        cfg = Config.load()
    if container is None:
        container = build_container(cfg)

    app = FastAPI(title="Tiri API", version="0.0.1")
    app.state.cfg = cfg
    app.state.container = container

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_split_origins(cfg.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(management.router, prefix="/rooms", tags=["management"])
    app.include_router(
        conversations.router, prefix="/rooms", tags=["conversations"]
    )
    app.include_router(feedback.router, prefix="/rooms", tags=["feedback"])
    app.include_router(mcp_server.router, prefix="/mcp", tags=["mcp"])

    _register_exception_handlers(app)
    return app


def _split_origins(spec: str) -> list[str]:
    if not spec or spec == "*":
        return ["*"]
    return [o.strip() for o in spec.split(",") if o.strip()]


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RoomNotFoundError)
    async def room_not_found_handler(
        request: Request, exc: RoomNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": "room_not_found", "message": str(exc)},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(
        request: Request, exc: ValueError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "message": str(exc)},
        )

    @app.exception_handler(ConfigurationError)
    async def config_error_handler(
        request: Request, exc: ConfigurationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"error": "configuration_error", "message": str(exc)},
        )


# Module-level app for uvicorn. Created lazily on first access so importing
# the module doesn't trigger Config.load() in test environments.
_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
