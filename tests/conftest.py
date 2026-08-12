from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an ``httpx.Client`` whose requests are served by ``handler``, no network."""
    return httpx.Client(transport=httpx.MockTransport(handler))


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
def make_client_fixture() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]:
    return make_client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from actually sleeping, in the modules that call time.sleep."""
    monkeypatch.setattr("sms_providers.base.time.sleep", lambda seconds: None)
    monkeypatch.setattr("sms_providers._http.time.sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def block_network() -> Iterator[None]:
    """Fail any test that tries to open a real socket connection."""

    def _blocked_connect(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network access is not allowed in tests")

    original_connect = socket.socket.connect
    socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
