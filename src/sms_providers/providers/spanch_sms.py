"""Client for Spanch SMS (https://spanch-projects.com) - own protocol, not sms-activate-compatible."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from sms_providers._http import Throttle, build_client, request_with_retry
from sms_providers.base import (
    ActivationCancelled,
    ActivationNotFound,
    ActivationStatus,
    BaseSmsProvider,
    Country,
    InsufficientBalance,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    ProviderUnavailable,
    RateLimited,
    Service,
    SmsCode,
    SmsProviderError,
)
from sms_providers.manager import SmsProviderManager

#: Ordered (needle, exception) pairs, matched by substring against the
#: normalized (stripped + lowercased) error message - Spanch has no stable
#: error codes, only free text, and that text drifts from the docs (see the
#: needle for #11 below).
SPANCH_ERRORS: tuple[tuple[str, type[SmsProviderError]], ...] = (
    ("the key is wrong", InvalidApiKey),
    ("insufficient funds", InsufficientBalance),
    ("no numbers", NoNumbersAvailable),
    ("no prices available", NoNumbersAvailable),
    ("rent is not available", NoNumbersAvailable),
    ("too many active activations", RateLimited),
    ("order not found", ActivationNotFound),
    ("no longer active", ActivationCancelled),
    # No apostrophe/quote: the live response mis-escapes the apostrophe in
    # "can't" as a literal double-quote after json.loads (see _raise_error).
    ("cancel an order so quickly", OperationNotAllowed),
    ("the gateway did not respond", ProviderUnavailable),
    # "Unknown error from gateway" - not in the documented list of 29,
    # observed live. Provisional ProviderUnavailable, see _raise_error.
    ("from gateway", ProviderUnavailable),
)

_RETRY_BACKOFF = 0.5


@SmsProviderManager.register()
class SpanchSmsProvider(BaseSmsProvider):
    """Spanch SMS (https://spanch-projects.com) - own protocol, single GET endpoint.

    Two things are unusual compared to the other built-in providers:

    * ``country`` is a two-letter ISO code (``"RU"``), not a numeric code.
    * ``gateway`` is required by ``get_number()`` and has no hardcoded
      default - it's a choice of upstream supplier with its own price and
      availability, not something to silently pick for the caller. Pass
      ``gateway=`` per call or set ``default_gateway=`` in the constructor;
      see :meth:`get_gateways` for the current list. Neither the constructor
      nor :meth:`get_number` validates ``gateway`` against a hardcoded
      allowlist - the live set already differs from the docs (14 gateways
      seen live vs. 12 documented) - an unsupported value is rejected by the
      API itself.

    Errors carry no stable code, only free text that drifts from the
    documentation - see :data:`SPANCH_ERRORS` and :meth:`_raise_error`.
    Responses arrive with HTTP 400 on error, parsed from the body; decisions
    are made on the ``status`` field only, never on the shape of ``message``
    (which is polymorphic: an error string, a number for ``getBalance``, a
    list or JSON-encoded string for ``getCountries``/``getServices``, a
    comma-separated string for ``getGateways``, a state string for
    ``getCode``).

    Rental (``getRentNumber``/``getRentStatus``/``setRentStatus``) and email
    methods are out of scope for v1.

    Spanch has no completion endpoint - :meth:`finish` is a documented
    no-op (see its docstring). ``cancel()`` is idempotent on the API's own
    side: cancelling an already-cancelled order succeeds again rather than
    raising.
    """

    name: ClassVar[str] = "spanch-sms"
    base_url: ClassVar[str] = "https://spanch-projects.com/api"
    default_poll_interval: ClassVar[float] = 5.0

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        name: str | None = None,
        default_gateway: str | None = None,
        timeout: float = 15.0,
        poll_interval: float | None = None,
        max_retries: int = 2,
        min_request_interval: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        resolved_base_url = base_url or type(self).base_url
        if not resolved_base_url:
            raise ValueError("base_url is required")
        if name is not None:
            self.name = name  # type: ignore[misc] # deliberate per-instance override of the ClassVar default
        self.base_url = resolved_base_url  # type: ignore[misc]
        self._api_key = api_key
        self.default_gateway = default_gateway
        self.timeout = timeout
        self.poll_interval = (
            poll_interval if poll_interval is not None else self.default_poll_interval
        )
        self.max_retries = max_retries
        # No evidence of request-frequency rate limiting (error #20 limits
        # concurrent active activations, not request rate) - off by default.
        self._throttle = Throttle(min_request_interval)
        self._client, self._owns_client = build_client(timeout=timeout, client=client)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ---

    async def _request(self, action: str, **params: Any) -> dict[str, Any]:
        query = {
            "api_key": self._api_key,
            "action": action,
            **{k: v for k, v in params.items() if v is not None},
        }
        response = await request_with_retry(
            client=self._client,
            url=self.base_url,
            params=query,
            max_retries=self.max_retries,
            retry_backoff=_RETRY_BACKOFF,
            provider_name=self.name,
            throttle=self._throttle,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAPIError(
                f"invalid JSON response: {response.text[:200]}",
                provider=self.name,
                raw=response.text,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderAPIError(f"unexpected response: {data!r}", provider=self.name, raw=data)
        status = data.get("status")
        if status == "error":
            self._raise_error(str(data.get("message", "")), raw=data)
        if status != "success":
            raise ProviderAPIError(
                f"unexpected status: {status!r}", provider=self.name, raw=data
            )
        return data

    def _raise_error(self, message: str, *, raw: Any = None) -> None:
        """Match ``message`` by substring, first hit wins - see :data:`SPANCH_ERRORS`.

        Extra fields useful for a couple of errors (``price`` on #7,
        ``seconds`` on #11) are not surfaced as dedicated attributes - they
        stay in ``raw``, which every caller already has access to.
        """
        normalized = message.strip().lower()
        for needle, exc_cls in SPANCH_ERRORS:
            if needle in normalized:
                if exc_cls is ActivationCancelled:
                    raise ActivationCancelled(
                        message, status=ActivationStatus.EXPIRED, provider=self.name, raw=raw
                    )
                raise exc_cls(message, provider=self.name, raw=raw)
        raise ProviderAPIError(message, provider=self.name, raw=raw)

    def _to_int_str(self, value: Any, *, raw: Any = None) -> str:
        """``id``/``phone`` arrive as JSON numbers - str(int(...)), not str(...), to
        avoid a ".0" tail if the API ever returns a float."""
        try:
            return str(int(value))
        except (TypeError, ValueError) as exc:
            raise ProviderAPIError(
                f"unexpected numeric value: {value!r}", provider=self.name, raw=raw
            ) from exc

    @staticmethod
    def _parse_decimal(value: Any) -> Decimal | None:
        """``price``/``message`` (getBalance) can arrive as a string or a number."""
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def _message_as_items(self, message: Any, *, raw: Any = None) -> list[str]:
        """``message`` for getCountries/getServices: a JSON list, or (getServices,
        live-confirmed) a string double-encoding one. Never falls back to
        ``split(",")`` - service codes themselves contain commas and slashes
        (``"google,youtube,gmail"``, ``"alipay/alibaba/1688"``), so a comma
        fallback would silently shred a single valid code into several bogus
        ones.
        """
        if isinstance(message, list):
            return [str(item) for item in message]
        if isinstance(message, str) and message.strip().startswith("["):
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError as exc:
                raise ProviderAPIError(
                    f"unexpected message value: {message!r}", provider=self.name, raw=raw
                ) from exc
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        raise ProviderAPIError(f"unexpected message value: {message!r}", provider=self.name, raw=raw)

    def _message_as_tokens(self, message: Any) -> list[str]:
        """``message`` for getGateways only: a comma-separated string, or a list."""
        if isinstance(message, list):
            return [str(item) for item in message]
        if isinstance(message, str):
            return [item.strip() for item in message.split(",") if item.strip()]
        return []

    # --- public API ---

    async def get_balance(self) -> Decimal:
        data = await self._request("getBalance")
        balance = self._parse_decimal(data.get("message"))
        if balance is None:
            raise ProviderAPIError(
                f"unexpected balance value: {data.get('message')!r}", provider=self.name, raw=data
            )
        return balance

    async def get_number(
        self,
        service: str,
        country: str | int | None = None,
        *,
        gateway: str | None = None,
        operator: str | None = None,
        route: str | None = None,
        max_price: str | None = None,
        phone_exception: str | None = None,
        **extra: Any,
    ) -> PhoneNumber:
        """Buy a number. ``country`` is a two-letter ISO code (``"RU"``), not numeric.

        ``gateway`` resolves to ``gateway or self.default_gateway``; if
        still empty, raises :class:`ValueError` before any request - a
        config error, not an API one, so no money is spent and no number is
        reserved. ``max_price`` is in USD. ``route`` is only required by a
        subset of gateways (larry/plankton) - not guessed here; the API
        rejects a missing one with a message listing the valid values.
        """
        resolved_gateway = gateway or self.default_gateway
        if not resolved_gateway:
            raise ValueError(
                "gateway is required for spanch-sms; pass gateway= or set "
                "default_gateway=; see get_gateways()"
            )
        data = await self._request(
            "getNumber",
            service=service,
            country=country,
            gateway=resolved_gateway,
            operator=operator,
            route=route,
            maxPrice=max_price,
            phoneException=phone_exception,
            **extra,
        )
        if "id" not in data or "phone" not in data:
            raise ProviderAPIError(
                f"unexpected getNumber response: {data!r}", provider=self.name, raw=data
            )
        return PhoneNumber(
            id=self._to_int_str(data["id"], raw=data),
            phone=self._to_int_str(data["phone"], raw=data),
            provider=self.name,
            service=service,
            country=str(country) if country is not None else None,
            country_phone_code=None,  # Spanch doesn't report a phone prefix, see §13.
            cost=self._parse_decimal(data.get("price")),
            raw=data,
        )

    async def get_code(self, activation_id: str) -> SmsCode | None:
        """Non-blocking check for a code.

        Tolerant by design: any ``status == "success"`` response other than
        the exact "code received" shape means "no code yet" and returns
        ``None`` rather than raising - an unrecognized intermediate state
        must not abandon an already-paid-for activation. Confirmed live
        waiting form is ``{"status":"success","message":"Wait code"}``, but
        this does not match it literally for that reason.
        """
        data = await self._request("getCode", id=str(activation_id))
        if data.get("message") == "received":
            code = data.get("code")
            if code:
                return SmsCode(code=str(code), text=data.get("full"), raw=data)
        return None

    async def get_status(self, activation_id: str) -> ActivationStatus:
        try:
            data = await self._request("getCode", id=str(activation_id))
        except ActivationCancelled as exc:
            return exc.status
        if data.get("message") == "received" and data.get("code"):
            return ActivationStatus.CODE_RECEIVED
        return ActivationStatus.WAITING

    async def wait_code(
        self,
        activation_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> SmsCode:
        activation_id = str(activation_id)
        resolved_timeout = timeout if timeout is not None else self.default_timeout
        resolved_interval = poll_interval if poll_interval is not None else self.poll_interval
        return await self._poll(
            lambda: self.get_code(activation_id),
            timeout=resolved_timeout,
            interval=resolved_interval,
        )

    async def request_retry(self, activation_id: str) -> None:
        await self._request("getNewCode", id=str(activation_id))

    async def cancel(self, activation_id: str) -> None:
        """Cancel the activation. Idempotent on the API's own side - cancelling an
        already-cancelled order succeeds again instead of raising; a too-early
        cancel (within the protective interval) raises :class:`OperationNotAllowed`
        with ``seconds`` (remaining wait) in ``exc.raw``.
        """
        await self._request("getCancel", id=str(activation_id))

    async def finish(self, activation_id: str) -> None:
        """No-op: Spanch has no finish endpoint - an order closes itself once the code arrives.

        Kept for contract compatibility so provider-agnostic code can call
        finish() unconditionally. Never raises, never makes a request. This
        is a documented exception to invariant 4 of ``BaseSmsProvider``:
        calling finish() again here never raises ActivationNotFound.
        """
        return

    # --- gateways ---

    async def get_gateways(self) -> list[str]:
        """List of gateways usable as ``get_number(gateway=...)``/``default_gateway=``.

        ``message`` arrives as a comma-separated string, not a JSON list.
        Not validated or cached against a hardcoded allowlist - the live set
        already differs from the docs (14 gateways seen live, incl.
        ``jellyfish``/``whale``, vs. 12 documented); an unsupported value is
        rejected by the API itself.
        """
        data = await self._request("getGateways")
        return self._message_as_tokens(data.get("message"))

    # --- discovery ---

    async def get_countries(self) -> list[Country]:
        """Countries available from Spanch. No country names are returned; ``name`` is
        always ``None``."""
        data = await self._request("getCountries")
        codes = self._message_as_items(data.get("message"), raw=data)
        return [Country(code=code, name=None, raw={"code": code}) for code in codes]

    async def get_services(
        self, country: str | int | None = None, search: str | None = None
    ) -> list[Service]:
        """Services available from Spanch.

        ``country`` is accepted for signature consistency with the other
        providers but IGNORED - Spanch's ``getServices`` has no country
        filter. No service names are returned; ``name`` is always ``None``.
        There is no server-side search, so ``search`` filters client-side,
        case-insensitively, by substring on ``code``.
        """
        data = await self._request("getServices")
        codes = self._message_as_items(data.get("message"), raw=data)
        result = [Service(code=code, name=None, raw={"code": code}) for code in codes]
        if search is not None:
            needle = search.strip().lower()
            result = [s for s in result if needle in s.code.lower()]
        return result

    async def get_prices(  # type: ignore[override]
        self, service: str, country: str, *, gateway: str | None = None
    ) -> Any:
        """Raw prices for ``service``/``country`` from ``getPrices``.

        Provider-specific, NOT part of ``BaseSmsProvider``'s contract -
        ``service``/``country`` are required here (the API itself requires
        them), unlike the base class's ``get_prices()``. Returns
        ``data["prices"]`` exactly as received, no DTO: a list of
        per-route entries when ``gateway`` is given, or a dict keyed by
        gateway name when it's omitted (observed live:
        ``{"crabbs": [...], "bob": [...]}``) - both shapes are valid and
        returned as-is. No unification yet: HeroSMS's ``getPrices`` has
        a completely different shape; do it when a second real data point
        exists, not the first.

        Live quirk (2026-08-13): with ``gateway=`` given, the route
        identifier of each entry arrives in the ``"gateway"`` key, not
        ``"route"`` as the docs show - ``[{"price": 0.46, "gateway":
        "DQV"}, ...]``. Read ``entry.get("route") or entry.get("gateway")``
        to be safe. Gateways such as ``plankton``/``larry`` require picking
        one of these routes and passing it to
        ``get_number(..., route=...)`` - a missing route is rejected by the
        API with a message listing the valid values.
        """
        data = await self._request(
            "getPrices", service=service, country=country, gateway=gateway
        )
        return data.get("prices")
