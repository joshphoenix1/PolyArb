"""FastAPI dashboard application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from dashboard.routes import create_router

if TYPE_CHECKING:
    from main import BotState

logger = logging.getLogger(__name__)


def create_app(bot_state: "BotState") -> FastAPI:
    """Create the FastAPI dashboard app with a reference to bot state."""

    app = FastAPI(title="PolyArb Dashboard", version="1.0.0")
    router = create_router(bot_state)
    app.include_router(router)

    return app
