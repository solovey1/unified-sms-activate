"""Private HTTP helpers shared by the built-in providers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

from sms_providers import __version__
from sms_providers.base import ProviderUnavailable

DEFAULT_USER_AGENT = f"sms-providers/{__version__} (+https://github.com/solovey1/unified-sms-activate)"


def build_client(
    *, timeout: float, client: httpx.AsyncClient | None = None, proxy: str | None = None
) -> tuple[httpx.AsyncClient, bool]:
    """Return ``(client, owns_client)``.

    If a client was passed in, it is reused as-is and ``owns_client`` is
    ``False`` — the caller must not close it in its own ``aclose()``. ``proxy``
    is only used when this function builds the client itself.
    """
    if client is not None:
        return client, False
    return (
        httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=True,
            proxy=proxy,
        ),
        True,
    )


class Throttle:
    """Enforces a minimum interval between requests. Used by OnlineSim."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last_call: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._last_call is not None:
                remaining = self._min_interval - (now - self._last_call)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_call = time.monotonic()


async def request_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    params: Mapping[str, Any],
    max_retries: int,
    retry_backoff: float,
    provider_name: str,
    throttle: Throttle | None = None,
) -> httpx.Response:
    """GET ``url`` with retries on transport errors and HTTP 5xx.

    Retries ``max_retries`` times with backoff ``retry_backoff * 2**attempt``.
    If ``throttle`` is given, it waits before every attempt, including
    retries. Raises :class:`ProviderUnavailable` once retries are exhausted.
    """
    last_error: BaseException | httpx.Response | None = None
    for attempt in range(max_retries + 1):
        if throttle is not None:
            await throttle.wait()
        try:
            response = await client.get(url, params=params)
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if response.status_code >= 500:
                last_error = response
            else:
                return response
        if attempt < max_retries:
            await asyncio.sleep(retry_backoff * (2**attempt))
    raise ProviderUnavailable(
        f"request failed after {max_retries + 1} attempt(s)",
        provider=provider_name,
        # repr(), not the exception/response object itself: both httpx.TransportError
        # and httpx.Response carry a `.request` whose repr includes the full query
        # string (api_key included). repr(last_error) itself does not.
        raw=repr(last_error),
    )
