"""Public contract of the ``sms_providers`` package.

Defines the shared DTOs (:class:`PhoneNumber`, :class:`SmsCode`), the
:class:`ActivationStatus` enum, the package's exception hierarchy, and the
:class:`BaseSmsProvider` abstract contract that every provider implements.
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, TypeVar

T = TypeVar("T")

__all__ = [
    "AccountBlocked",
    "ActivationCancelled",
    "ActivationNotFound",
    "ActivationStatus",
    "ActivationTimeout",
    "BaseSmsProvider",
    "Country",
    "CountryPrice",
    "InsufficientBalance",
    "InvalidApiKey",
    "NoNumbersAvailable",
    "OperationNotAllowed",
    "PhoneNumber",
    "ProviderAPIError",
    "ProviderNotRegistered",
    "ProviderUnavailable",
    "RateLimited",
    "Service",
    "SmsCode",
    "SmsProviderError",
]


class ActivationStatus(str, Enum):
    """Lifecycle status of an activation, normalized across providers.

    The sms-activate protocol does not distinguish a user-cancelled
    activation from one that expired without a code: both are reported as
    ``STATUS_CANCEL``. Providers built on that protocol therefore always
    report :attr:`CANCELLED`, never :attr:`EXPIRED`.
    """

    PENDING = "pending"
    """Operation created, no phone number allocated yet (OnlineSim TZ_INPOOL)."""
    WAITING = "waiting"
    """Number allocated, waiting for an SMS."""
    WAITING_RETRY = "waiting_retry"
    """A new SMS was requested, waiting for it to arrive."""
    CODE_RECEIVED = "code_received"
    """A code has arrived and is available."""
    FINISHED = "finished"
    """Activation closed successfully."""
    CANCELLED = "cancelled"
    """Activation was cancelled."""
    EXPIRED = "expired"
    """Time ran out and no SMS arrived."""


@dataclass(frozen=True, slots=True)
class PhoneNumber:
    """A phone number allocated for an activation."""

    id: str
    """Activation id (a.k.a. tzid), always a string."""
    phone: str
    """Phone number exactly as returned by the provider, digits only, no "+"."""
    provider: str
    service: str | None = None
    country: str | None = None
    """Country code in the provider's own numbering (sms-activate: 6 = Indonesia)."""
    country_phone_code: str | None = None
    """E.164 phone prefix without "+" (62 = Indonesia). None if the provider didn't report it."""
    cost: Decimal | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Escape hatch: the provider's raw response for this call."""

    def __str__(self) -> str:
        return self.phone


@dataclass(frozen=True, slots=True)
class SmsCode:
    """A code extracted from a received SMS."""

    code: str
    text: str | None = None
    """Full SMS text, if the provider makes it available."""
    received_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Escape hatch: the provider's raw response for this call."""

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class Service:
    """A service offered by a provider, as returned by discovery.

    ``code`` is exactly the value :meth:`BaseSmsProvider.get_number` accepts
    as its ``service`` argument for this same provider - discovery unifies
    the SHAPE of the listing, not the codes themselves; there is still no
    normalization between providers.
    """

    code: str
    name: str | None = None
    count: int | None = None
    """Numbers currently available for this service, if the provider reports it."""
    price: Decimal | None = None
    """Activation price, if the provider reports it."""
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Escape hatch: the provider's raw response for this entry."""

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class Country:
    """A country offered by a provider, as returned by discovery.

    ``code`` is exactly the value :meth:`BaseSmsProvider.get_number` accepts
    as its ``country`` argument for this same provider.
    """

    code: str
    name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Escape hatch: the provider's raw response for this entry."""

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class CountryPrice:
    """Availability/price of one service in one country, as returned by discovery.

    Where :meth:`BaseSmsProvider.get_services`/:meth:`get_countries` answer
    "what services/countries exist", :meth:`BaseSmsProvider.get_prices`
    answers "where and for how much is a service available" in one call.
    ``country``/``service`` are exactly the values :meth:`get_number` accepts
    as ``country=``/``service=`` for this same provider.
    """

    country: str
    service: str
    cost: Decimal | None = None
    """Activation price, if the provider reports it for this country/service pair."""
    count: int | None = None
    """Numbers currently available, if the provider reports it. ``0`` is a
    real, meaningful value (out of stock) and is never filtered out."""
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Escape hatch: the provider's raw response for this entry."""

    def __str__(self) -> str:
        return f"{self.service}@{self.country}"


class SmsProviderError(Exception):
    """Base exception of the package. Everything raised by providers subclasses it."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        code: str | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.code = code
        self.raw = raw

    def __str__(self) -> str:
        if self.provider and self.code:
            return f"[{self.provider}] {self.code}: {self.message}"
        if self.provider:
            return f"[{self.provider}] {self.message}"
        if self.code:
            return f"{self.code}: {self.message}"
        return self.message


class InvalidApiKey(SmsProviderError):
    """The API key is missing or invalid."""


class AccountBlocked(SmsProviderError):
    """Account is blocked, not activated, banned, or IP access is denied."""


class InsufficientBalance(SmsProviderError):
    """Not enough funds to perform the operation."""


class NoNumbersAvailable(SmsProviderError):
    """No numbers available for the requested service/country."""


class RateLimited(SmsProviderError):
    """Concurrent channel limit or request rate limit exceeded."""


class ActivationNotFound(SmsProviderError):
    """Unknown activation_id / tzid."""


class ActivationCancelled(SmsProviderError):
    """The activation ended without a code, on the provider's side."""

    def __init__(
        self,
        message: str,
        *,
        status: ActivationStatus,
        provider: str | None = None,
        code: str | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message, provider=provider, code=code, raw=raw)
        self.status = status


class ActivationTimeout(SmsProviderError):
    """Local ``wait_code`` deadline was reached before a code arrived.

    Not a subclass of :class:`ActivationCancelled`: this is a local deadline,
    unrelated to the activation's state on the provider's side.
    """


class OperationNotAllowed(SmsProviderError):
    """The service refused cancel/finish (protective interval, OTP already received)."""


class ProviderUnavailable(SmsProviderError):
    """Network error, transport timeout, or HTTP 5xx after retries."""


class ProviderAPIError(SmsProviderError):
    """Any other or unrecognized API error."""


class ProviderNotRegistered(SmsProviderError, KeyError):
    """Requested provider name is not registered with the manager."""


class BaseSmsProvider(ABC):
    """Contract for an SMS-receiving provider. Users subclass this directly.

    Invariants that every implementation must uphold:

    1. ``activation_id`` is always a ``str`` — exactly what is stored in
       ``PhoneNumber.id``.
    2. ``wait_code`` does NOT call ``finish()`` automatically — closing the
       activation is the caller's responsibility.
    3. ``wait_code`` raises :class:`ActivationCancelled` if the activation
       died on the service's side, and :class:`ActivationTimeout` on the
       local deadline.
    4. Calling ``cancel()``/``finish()`` again on an already-closed
       activation raises :class:`ActivationNotFound` or
       :class:`OperationNotAllowed` — never a bare transport exception.
       Documented exception: providers with no completion endpoint of their
       own (``SpanchSmsProvider`` — an order closes itself once the code
       arrives) implement ``finish()`` as a no-op, so repeated calls there
       never raise anything.
    5. No API error ever leaks out as ``httpx.*``, ``ValueError``,
       ``KeyError``, or ``json.JSONDecodeError``.
    6. ``asyncio.CancelledError`` is never swallowed and never turned into a
       :class:`SmsProviderError`. Cancelling a task awaiting ``wait_code()``
       does NOT cancel the activation on the service's side — closing it is
       the caller's responsibility (``try/finally`` with
       ``await provider.cancel(...)``).
    """

    name: ClassVar[str] = ""
    default_poll_interval: ClassVar[float] = 5.0
    default_timeout: ClassVar[float] = 300.0

    @abstractmethod
    async def get_balance(self) -> Decimal: ...

    @abstractmethod
    async def get_number(
        self, service: str, country: str | int | None = None, **options: Any
    ) -> PhoneNumber: ...

    @abstractmethod
    async def get_status(self, activation_id: str) -> ActivationStatus: ...

    @abstractmethod
    async def wait_code(
        self,
        activation_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> SmsCode: ...

    @abstractmethod
    async def cancel(self, activation_id: str) -> None: ...

    @abstractmethod
    async def finish(self, activation_id: str) -> None: ...

    async def request_retry(self, activation_id: str) -> None:
        """Request the next SMS on the same number."""
        raise NotImplementedError(f"{type(self).__name__} does not support request_retry()")

    async def get_code(self, activation_id: str) -> SmsCode | None:
        """Non-blocking check for a code. ``None`` means no code yet."""
        raise NotImplementedError

    async def get_services(
        self, country: str | int | None = None, search: str | None = None
    ) -> list[Service]:
        """Services available from this provider; optionally narrow by country.

        ``search`` narrows the results to services whose code or name
        contains it, case-insensitively; support and exact matching
        semantics (server-side filter vs. client-side substring) are up to
        each provider. An empty list is a valid result, not an error.
        Element order follows the API's own order; results are never sorted
        or de-duplicated.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_services()")

    async def get_countries(self) -> list[Country]:
        """Countries available from this provider.

        An empty list is a valid result, not an error. Element order follows
        the API's own order; results are never sorted or de-duplicated.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_countries()")

    async def get_prices(
        self, service: str | None = None, country: str | int | None = None
    ) -> list[CountryPrice]:
        """Where and for how much numbers are available, in one call.

        An empty list is a valid result, not an error. Element order follows
        the API's own order; results are never sorted or de-duplicated.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_prices()")

    _DOMAIN_SUFFIX_RE = re.compile(r"\.[a-z]{2,4}$")

    @classmethod
    def _normalize_service_name(cls, value: str) -> str:
        return cls._DOMAIN_SUFFIX_RE.sub("", value.strip().lower())

    async def find_service(
        self, name: str, country: str | int | None = None
    ) -> Service | None:
        """Find a service by code or name in one call, tolerating naming drift.

        Different providers describe the same service differently - e.g.
        hero-sms lists "Bybit", vak-sms lists "bybit.com" - so an exact,
        case-sensitive lookup on either code or name alone is not enough.
        Built on :meth:`get_services` (called once, with ``search=name``),
        then matched in priority order, all case-insensitive: (1) exact
        ``code``, (2) exact ``name``, (3) ``name`` with a trailing domain
        suffix (``.com``, ``.io``, ...) stripped from both sides. Returns
        the first match found at the highest matching priority, or ``None``
        if nothing matches at any of the three.
        """
        services = await self.get_services(country=country, search=name)
        name_lower = name.strip().lower()
        name_normalized = self._normalize_service_name(name)
        for service in services:
            if service.code.lower() == name_lower:
                return service
        for service in services:
            if service.name is not None and service.name.strip().lower() == name_lower:
                return service
        for service in services:
            if (
                service.name is not None
                and self._normalize_service_name(service.name) == name_normalized
            ):
                return service
        return None

    async def aclose(self) -> None: ...

    async def __aenter__(self) -> BaseSmsProvider:  # noqa: PYI034 - typing.Self needs py3.11+
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"

    async def _poll(
        self, fetch: Callable[[], Awaitable[T | None]], *, timeout: float, interval: float
    ) -> T:
        """Call ``fetch()`` until it returns a non-``None`` result.

        The first ``fetch()`` happens immediately, without sleeping. After
        that, the deadline (``time.monotonic()``) is checked before each
        ``asyncio.sleep(interval)`` so the last attempt never sleeps needlessly.
        Raises :class:`ActivationTimeout` once the deadline passes.
        """
        deadline = time.monotonic() + timeout
        result = await fetch()
        if result is not None:
            return result
        while True:
            if time.monotonic() >= deadline:
                raise ActivationTimeout(f"{type(self).__name__}: wait_code timed out")
            await asyncio.sleep(interval)
            result = await fetch()
            if result is not None:
                return result
