"""Radio / transmitter endpoints.

The interesting value here is `tuned_hz`: the published downlink corrected for
Doppler at this instant. That is the number an operator types into a radio, and
computing it on the server means the dashboard and the antenna are working from
the same range rate rather than two independently propagated ones.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

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
