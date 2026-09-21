"""Radio / transmitter endpoints.

The interesting value here is `tuned_hz`: the published downlink corrected for
Doppler at this instant. That is the number an operator types into a radio, and
computing it on the server means the dashboard and the antenna are working from
the same range rate rather than two independently propagated ones.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from ..services.transmitters import doppler_shift_hz

router = APIRouter()


@router.get("/radio")
async def radio(request: Request, norad: int | None = None) -> dict:
    store = getattr(request.app.state, "transmitters", None)
    if store is None:
        raise HTTPException(503, "transmitter store not running")

    settings = request.app.state.settings
    predictor = request.app.state.predictor
    norad = norad or settings.default_norad

    # Lazily populated: a satellite the operator has only just selected has
    # never been fetched, and waiting for a scheduled refresh would leave the
    # panel empty through the pass they are watching.
    await store.refresh(norad)

    transmitters = store.get(norad)
    primary = store.primary_downlink(norad)
    pos = predictor.position(norad)

    items = []
    for tx in transmitters:
        entry = dict(tx)
        entry["primary"] = bool(primary and tx.get("uuid") == primary.get("uuid"))
        if tx.get("downlink_hz") and pos is not None:
            shift = doppler_shift_hz(tx["downlink_hz"], pos.range_rate_km_s)
            entry["doppler_hz"] = round(shift, 1)
            entry["tuned_hz"] = round(tx["downlink_hz"] + shift, 1)
        else:
            entry["doppler_hz"] = None
            entry["tuned_hz"] = None
        items.append(entry)

    return {
        "norad": norad,
        "age_s": None if store.age_s(norad) == float("inf") else round(store.age_s(norad), 1),
        "source": "satnogs-db",
        # Only meaningful while the satellite is up; the panel greys out below
        # the horizon rather than showing a shift for a satellite that is not
        # there.
        "visible": bool(pos and pos.el > 0),
        "el": round(pos.el, 2) if pos else None,
        "range_rate_km_s": round(pos.range_rate_km_s, 4) if pos else None,
        "count": len(items),
        "transmitters": items,
    }


@router.get("/radio/waterfall")
async def waterfall_meta(request: Request, norad: int | None = None) -> dict:
    """Which observation the waterfall image currently shows."""
    store = getattr(request.app.state, "waterfall", None)
    if store is None:
        raise HTTPException(503, "waterfall store not running")
    norad = norad or request.app.state.settings.default_norad

    meta = await store.refresh(norad)
    if meta is None:
        return {"norad": norad, "available": False}
    return {
        "norad": norad,
        "available": store.png(norad) is not None,
        "image_url": f"/api/radio/waterfall.png?norad={norad}",
        **meta,
    }


@router.get("/radio/waterfall.png")
async def waterfall_png(request: Request, norad: int | None = None) -> Response:
    """The cropped, contrast-lifted signal region as a PNG.

    Proxied rather than linked. The source lives on a Wasabi S3 bucket, so a
    direct link would make every wall display fetch 1.6 MB of matplotlib
    furniture across the internet to show a 760x150 strip — and the cropping
    has to happen somewhere with an image library anyway.
    """
    store = getattr(request.app.state, "waterfall", None)
    if store is None:
        raise HTTPException(503, "waterfall store not running")
    norad = norad or request.app.state.settings.default_norad

    await store.refresh(norad)
    png = store.png(norad)
    if png is None:
        raise HTTPException(404, f"no waterfall available for {norad}")

    meta = store.meta(norad) or {}
    return Response(
        content=png,
        media_type="image/png",
        headers={
            # Keyed to the observation, so a new pass busts it and a wall
            # display that reloads every minute does not refetch 100 KB.
            "Cache-Control": "public, max-age=120",
            "X-Observation-Id": str(meta.get("id", "")),
        },
    )


@router.get("/telemetry")
async def telemetry(request: Request, norad: int | None = None) -> dict:
    """Recent decoded frames for a satellite, newest first.

    Always 200 with a `status`, never an error for a station that is simply not
    configured: `/telemetry/` is the one SatNOGS endpoint here that needs a
    token, and an empty `GS_SATNOGS_DB_TOKEN` comes back as
    `status: "no_token"` with a `detail` the panel can print. A panel that says
    what to set is worth more than one that is blank or red.
    """
    store = getattr(request.app.state, "telemetry", None)
    if store is None:
        raise HTTPException(503, "telemetry store not running")
    norad = norad or request.app.state.settings.default_norad

    # Lazily, like the transmitters above: a satellite the operator has only
    # just selected has never been fetched. TTL-gated, so a wall display
    # polling this does not turn into polling SatNOGS.
    await store.refresh(norad)
    return store.snapshot(norad)
