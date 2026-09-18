"""The shared HTTP session and the SatNOGS cursor paginator.

Two things here are specific to SatNOGS and worth keeping:

* Pagination is cursor-based through the ``Link: <...>; rel="next"`` header.
  There is no ``?page=``, and ``?page_size=`` is silently ignored - every page
  is 25 items whatever you ask for.
* The observation endpoints are genuinely slow. ``?norad_cat_id=`` takes about
  17 seconds; the station feed about 6 seconds a page. Timeouts have to be
  generous or perfectly healthy requests start failing.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Iterator

import requests

from . import USER_AGENT

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 90          # the observation feed really is this slow
MAX_RETRIES = 3
BACKOFF_BASE_S = 2.0

_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class SatnogsHTTPError(RuntimeError):
    """An API call failed in a way the caller should report, not retry."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def make_session(token: str = "", token_scheme: str = "Token") -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if token:
        session.headers["Authorization"] = f"{token_scheme} {token}"
    return session


def request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> requests.Response:
    """One request, retried on transport errors and 5xx but never on 4xx.

    A 4xx is the server telling us the request itself is wrong; repeating it
    just wastes everybody's time.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.request(
                method, url, params=params, json=json_body, timeout=timeout
            )
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("%s %s failed (%s), attempt %d/%d",
                        method, url, exc, attempt + 1, MAX_RETRIES)
        else:
            if resp.status_code < 400:
                return resp
            if 400 <= resp.status_code < 500:
                raise SatnogsHTTPError(
                    f"{method} {url} -> HTTP {resp.status_code}",
                    status=resp.status_code,
                    body=resp.text[:2000],
                )
            log.warning("%s %s -> HTTP %d, attempt %d/%d",
                        method, url, resp.status_code, attempt + 1, MAX_RETRIES)
            last_exc = SatnogsHTTPError(
                f"{method} {url} -> HTTP {resp.status_code}",
                status=resp.status_code,
                body=resp.text[:2000],
            )
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_BASE_S * (attempt + 1))

    raise SatnogsHTTPError(
        f"{method} {url} failed after {MAX_RETRIES} attempts: {last_exc}"
    )


def next_link(resp: requests.Response) -> str | None:
    """Pull the next-page URL out of a SatNOGS Link header."""
    match = _NEXT_LINK.search(resp.headers.get("Link", ""))
    return match.group(1) if match else None


def paginate(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    max_pages: int | None = None,
    stop: Callable[[dict], bool] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Iterator[dict]:
    """Walk a cursor-paginated SatNOGS list endpoint.

    ``stop`` is an optional predicate taking one item. The first item for which
    it returns True ends the walk, without yielding that item. It is how the
    observation-history crawl stops at the watermark it already has instead of
    paging through years of history at six seconds a page.
    """
    page = 0
    next_url: str | None = url
    next_params = params
    while next_url:
        resp = request(session, "GET", next_url, params=next_params, timeout=timeout)
        # The Link header already carries the query string for later pages.
        next_params = None
        items = resp.json()
        if not isinstance(items, list):
            raise SatnogsHTTPError(
                f"expected a list from {next_url}, got {type(items).__name__}"
            )
        for item in items:
            if stop is not None and stop(item):
                return
            yield item
        page += 1
        if max_pages is not None and page >= max_pages:
            log.debug("stopping at the %d-page limit for %s", max_pages, url)
            return
        next_url = next_link(resp)
