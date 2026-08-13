from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` whose requests are served by ``handler``, no network."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Recorder:
    """Serves canned responses in order and records every request made."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("Recorder ran out of canned responses")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def responses(*items: Any) -> Recorder:
    """Return a handler serving ``items`` in order.

    Items can be ``httpx.Response`` instances (returned as-is) or exception
    instances (raised instead), letting a single sequence mix successes and
    transport errors.
    """
    return Recorder(*items)


@pytest.fixture
def make_client_fixture() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient]:
    return make_client


async def _noop_sleep(seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from actually sleeping, in the modules that call asyncio.sleep."""
    monkeypatch.setattr("sms_providers.base.asyncio.sleep", _noop_sleep)
    monkeypatch.setattr("sms_providers._http.asyncio.sleep", _noop_sleep)


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def block_network() -> Iterator[None]:
    """Fail any test that tries to open a real socket connection.

    Loopback connections are allowed: on Windows, asyncio's ProactorEventLoop
    opens a local self-pipe socketpair (a loopback connect()) as part of
    creating the per-test event loop - unrelated to a test reaching the
    network, but caught by a blanket socket.connect() block all the same.
    """
    original_connect = socket.socket.connect

    def _blocked_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK_HOSTS:
            return original_connect(self, address, *args, **kwargs)
        raise RuntimeError("network access is not allowed in tests")

    socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
