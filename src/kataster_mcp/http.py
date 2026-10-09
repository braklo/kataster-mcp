"""Sequential HTTP client with per-host pacing.

All requests go through one lock, so the server never sends two requests at once.
Pacing per host comes from Policy (ESKN portal: >= 3 s + jitter).
"""

import asyncio
import random
import time
from urllib.parse import urlsplit

import httpx

from . import __version__
from .envelope import KatasterError
from .policy import Policy

USER_AGENT = f"kataster-mcp/{__version__} (+local MCP server; human-paced)"

# Markers of the F5 WAF / bot challenge and of reCAPTCHA pages.
_BLOCK_MARKERS = ("Request Rejected", "The requested URL was rejected")
_CHALLENGE_MARKERS = ("TSPD", "bobcmn")
_CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/api.js")


class Http:
    def __init__(self, policy: Policy, *, client: httpx.AsyncClient | None = None,
                 sleep=asyncio.sleep, clock=time.monotonic):
        self.policy = policy
        self._client = client
        self._lock = asyncio.Lock()
        self._last: dict[str, float] = {}
        self._sleep = sleep
        self._clock = clock

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=True,
            )
        return self._client

    async def _wait_turn(self, host: str) -> None:
        pause, jitter = self.policy.pause_for(host)
        last = self._last.get(host)
        if last is not None:
            wait = pause + random.uniform(0, jitter) - (self._clock() - last)
            if wait > 0:
                await self._sleep(wait)

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        async with self._lock:
            await self._wait_turn(host)
            try:
                resp = await self.client.get(url, params=params)
            except httpx.HTTPError as e:
                raise KatasterError("unavailable", f"{host}: {type(e).__name__}: {e}") from e
            finally:
                self._last[host] = self._clock()
        check_response(resp)
        return resp

    async def post(self, url: str, data: dict, headers: dict | None = None) -> httpx.Response:
        """Form POST under the same lock, pacing and block detection as get(); redirects are followed."""
        host = urlsplit(url).hostname or ""
        async with self._lock:
            await self._wait_turn(host)
            try:
                resp = await self.client.post(url, data=data, headers=headers)
            except httpx.HTTPError as e:
                raise KatasterError("unavailable", f"{host}: {type(e).__name__}: {e}") from e
            finally:
                self._last[host] = self._clock()
        check_response(resp)
        return resp

    async def get_json(self, url: str, params: dict | None = None):
        resp = await self.get(url, params)
        try:
            return resp.json()
        except ValueError as e:
            snippet = resp.text[:300]
            raise KatasterError("parser_drift", f"{resp.url.host}: expected JSON", details=snippet) from e

    async def aclose(self) -> None:
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
