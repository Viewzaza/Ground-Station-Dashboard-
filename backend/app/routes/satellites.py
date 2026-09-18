from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


@router.get("/satellites")
async def satellites(request: Request, q: str = Query("", max_length=64)) -> dict:
    """The selector's catalogue. Pinned satellites sort first."""
    items = request.app.state.tles.catalog()
    if q:
        needle = q.lower()
        items = [
            it for it in items
            if needle in it["name"].lower() or needle in str(it["norad"])
        ]
    return {"count": len(items), "items": items[:500]}


@router.get("/tle")
async def tle(request: Request, norad: int) -> dict:
    info = request.app.state.tles.get(norad)
    if info is None:
        raise HTTPException(404, f"no elements for NORAD {norad}")
    return info.model_dump(mode="json")


@router.post("/tle/refresh")
async def tle_refresh(request: Request) -> dict:
    """Force a refresh. The 2 h floor still applies — Celestrak enforces it
    with HTTP 403 and firewalls repeat offenders, so this is not a bypass."""
    store = request.app.state.tles
    if store.is_fresh:
        raise HTTPException(
            429,
            f"cache is {store.cache_age_s:.0f}s old; the minimum interval is "
            f"{request.app.state.settings.tle_ttl_s}s",
        )
    fetched = await store.refresh()
    return {"fetched": fetched, "count": len(store)}
