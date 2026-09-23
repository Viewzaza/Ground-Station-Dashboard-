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


class SatnogsOutcomeUnknown(SatnogsHTTPError):
    """A write was sent and may or may not have been applied.

    Raised instead of retrying. The request reached the server - it was a read
    timeout, a dropped connection mid-response, or a 5xx from a gateway sitting
    in front of an application that may already have committed - so repeating
    it can create the same rows twice. The only safe next step is to read the
    real state back, never to send the write again.
    """


# Only these are retried when the request may have reached the server. Every
# other method creates or changes something, and "try again" is only safe for
# those when the first attempt provably never arrived.
_SAFE_TO_REPEAT = frozenset({"GET", "HEAD", "OPTIONS"})


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
    """One request. Reads are retried on transport errors and 5xx; writes are
    retried only when they provably never reached the server; nothing is ever
    retried on 4xx.

    A 4xx is the server telling us the request itself is wrong; repeating it
    just wastes everybody's time.

    WRITES ARE DIFFERENT, and this used to treat them as reads. The booking
    POST went through the same loop, so a read timeout after SatNOGS had
    already created the observations - or a 504 from a gateway in front of an
    application that had - sent the whole batch again, twice more. The caller
    then fell back to re-posting every item on its own, three tries each. A
    probe against a server that persists every POST and then drops the
    response turned 3 intended bookings into 12 POSTs and 18 created rows, and
    the run reported accepted 0, so none of it could be found or cancelled from
    the dashboard. On a 150-item campaign that is up to 900 create attempts on
    other people's stations.

    So for a write, only ConnectTimeout is retried: the TCP connection never
    completed, so nothing can have arrived. Everything else that can happen
    after the request is on the wire raises SatnogsOutcomeUnknown immediately.
    """
    repeatable = method.upper() in _SAFE_TO_REPEAT
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.request(
                method, url, params=params, json=json_body, timeout=timeout
            )
        except requests.exceptions.ConnectTimeout as exc:
            # Never connected, so never sent: safe to repeat for any method.
            last_exc = exc
            log.warning("%s %s could not connect (%s), attempt %d/%d",
                        method, url, exc, attempt + 1, MAX_RETRIES)
        except requests.RequestException as exc:
            if not repeatable:
                raise SatnogsOutcomeUnknown(
                    f"{method} {url} was sent but no answer arrived ({exc}); "
                    "it may or may not have been applied",
                ) from exc
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
            if not repeatable:
                raise SatnogsOutcomeUnknown(
                    f"{method} {url} -> HTTP {resp.status_code}; the server may "
                    "have applied it before failing",
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
