"""FastAPI application.

Everything long-lived (TLE refresh, rotator polling, SatNOGS polling) is owned
by the scheduler and started here. Routes stay thin: they read state that the
background tasks maintain.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .routes import (
    cameras, control, health, passes, radio, rotator, satellites, satnogs, ws,
)
from .scheduler import Scheduler
from .services.predictor import Predictor
from .services.tle_store import TleStore
from .services.transmitters import TransmitterStore

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("ground station backend starting (mock=%s)", settings.mock)

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    tles = TleStore(settings)
    transmitters = TransmitterStore(settings)
    predictor = Predictor(settings, tles, transmitters)
    # The scheduler reaches the store through the predictor it already owns,
    # rather than being handed a second reference to the same thing.
    scheduler = Scheduler(settings, tles, predictor)

    app.state.settings = settings
    app.state.tles = tles
    app.state.transmitters = transmitters
    app.state.predictor = predictor
    app.state.scheduler = scheduler
    app.state.rotator = scheduler.rotator
    app.state.satnogs = scheduler.satnogs
    app.state.control = scheduler.control

    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        log.info("ground station backend stopped")


app = FastAPI(
    title="KNACKSAT-2 Ground Station",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# No CORS middleware on purpose: Caddy serves the frontend and the API from one
# origin, so a cross-origin request should fail rather than be quietly allowed.

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(satellites.router, prefix="/api", tags=["satellites"])
app.include_router(passes.router, prefix="/api", tags=["passes"])
app.include_router(cameras.router, prefix="/api", tags=["cameras"])
app.include_router(rotator.router, prefix="/api", tags=["rotator"])
app.include_router(control.router, prefix="/api", tags=["control"])
app.include_router(satnogs.router, prefix="/api", tags=["satnogs"])
app.include_router(radio.router, prefix="/api", tags=["radio"])
app.include_router(ws.router, tags=["ws"])
