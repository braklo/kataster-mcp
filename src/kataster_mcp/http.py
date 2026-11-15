"""Sequential HTTP client with per-host pacing.

All requests of a process go through one lock, so the server never sends two requests at once.
Pacing per host comes from Policy (ESKN portal: >= 3 s + jitter). With a Pacer (the server always sets one)
the pause also holds across processes, with one lock per host.
"""

import asyncio
import contextlib
import random
import time
from urllib.parse import urlsplit

import httpx

from . import __version__
from .cache import ts_iso
from .envelope import KatasterError
from .policy import ESKN_HOST, Policy

USER_AGENT = f"kataster-mcp/{__version__} (+local MCP server; human-paced)"

# Markers of the F5 WAF / bot challenge and of reCAPTCHA pages.
_BLOCK_MARKERS = ("Request Rejected", "The requested URL was rejected")
_CHALLENGE_MARKERS = ("TSPD", "bobcmn")
_CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/api.js")
BLOCK_PAUSE_S = 24 * 3600  # after a WAF block, bot challenge or CAPTCHA the portal is left alone this long
BLOCK_SOURCE = "portal_blokacia"


class Http:
    def __init__(self, policy: Policy, *, client: httpx.AsyncClient | None = None,
                 sleep=asyncio.sleep, clock=time.monotonic):
        self.policy = policy
        self._client = client
        self._lock = asyncio.Lock()
        self._last: dict[str, float] = {}
        self._sleep = sleep
        self._clock = clock
        self.blocks = None  # Cache where a portal block is remembered across restarts (set by the server)
        self.pacer = None  # Pacer: the same pause across processes (set by the server)

    def blocked_until(self, host: str = ESKN_HOST) -> float | None:
        """Epoch time until which requests to host are not sent (after a block or CAPTCHA), else None."""
        if host != ESKN_HOST or self.blocks is None:
            return None
        hit = self.blocks.get(BLOCK_SOURCE, "1", host, BLOCK_PAUSE_S)
        return hit[1] + BLOCK_PAUSE_S if hit else None

    def guard(self, host: str = ESKN_HOST) -> None:
        until = self.blocked_until(host)
        if until is not None:
            raise KatasterError("blocked", f"{host} blocked or asked for CAPTCHA earlier; the server does not "
                                f"call it again until {ts_iso(until)}.", details={"skusi_znova": ts_iso(until)})

    def _check(self, resp: httpx.Response, host: str) -> None:
        try:
            check_response(resp)
        except KatasterError as e:
            if e.kind in ("blocked", "captcha_required") and host == ESKN_HOST and self.blocks is not None:
                self.blocks.put(BLOCK_SOURCE, "1", host, {"typ": e.kind})
            raise

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=True,
            )
        return self._client

    async def _wait_turn(self, host: str, since_other: float | None) -> None:
        """Wait out the pause after the last request to host: own (monotonic) or another process's (wall time)."""
        pause, jitter = self.policy.pause_for(host)
        last = self._last.get(host)
        elapsed = [self._clock() - last] if last is not None else []
        if since_other is not None:
            elapsed.append(since_other)
        if elapsed:
            wait = pause + random.uniform(0, jitter) - min(elapsed)
            if wait > 0:
                await self._sleep(wait)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        self.guard(host)
        async with self._lock:
            hold = self.pacer.hold(host) if self.pacer is not None else contextlib.nullcontext()
            async with hold as since_other:
                await self._wait_turn(host, since_other)
                try:
                    resp = await self.client.request(method, url, **kwargs)
                except httpx.HTTPError as e:
                    raise KatasterError("unavailable", f"{host}: {type(e).__name__}: {e}") from e
                finally:
                    self._last[host] = self._clock()
        self._check(resp, host)
        return resp

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        return await self._request("GET", url, params=params)

    async def post(self, url: str, data: dict, headers: dict | None = None) -> httpx.Response:
        """Form POST under the same lock, pacing and block detection as get(); redirects are followed."""
        return await self._request("POST", url, data=data, headers=headers)

    async def get_json(self, url: str, params: dict | None = None):
        resp = await self.get(url, params)
        try:
            return resp.json()
        except ValueError as e:
            snippet = resp.text[:300]
            raise KatasterError("parser_drift", f"{resp.url.host}: expected JSON", details=snippet) from e

    async def aclose(self) -> None:
        if self.pacer is not None:
            self.pacer.close()
        if self._client is not None:
            await self._client.aclose()


def check_response(resp: httpx.Response) -> None:
    host = resp.url.host
    text = resp.text[:20000] if resp.headers.get("content-type", "").startswith(("text/", "application/xhtml")) else ""
    if any(m in text for m in _BLOCK_MARKERS):
        raise KatasterError("blocked", f"{host} rejected the request (WAF).")
    if any(m in text for m in _CAPTCHA_MARKERS):
        raise KatasterError("captcha_required", f"{host} requires CAPTCHA; open it in a browser yourself.")
    if any(m in text for m in _CHALLENGE_MARKERS) and len(text) < 5000:
        raise KatasterError("blocked", f"{host} returned a bot challenge.")
    if resp.status_code in (403, 429):
        raise KatasterError("blocked", f"{host} HTTP {resp.status_code}")
    if resp.status_code >= 500:
        raise KatasterError("unavailable", f"{host} HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise KatasterError("unavailable", f"{host} HTTP {resp.status_code}", details=resp.text[:300])
