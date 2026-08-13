"""Generic client for sms-activate-protocol (``handler_api.php``) services."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from sms_providers._http import Throttle, build_client, request_with_retry
from sms_providers.base import (
    AccountBlocked,
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

#: Error codes of the "vanilla" sms-activate protocol. Keyed by the JSON
#: ``title`` field or the first ``:``-delimited token of a plain-text
#: response — both forms share this one table.
SMS_ACTIVATE_ERRORS: Mapping[str, type[SmsProviderError]] = {
    "BAD_KEY": InvalidApiKey,
    "ERROR_WRONG_KEY": InvalidApiKey,
    "NO_KEY": InvalidApiKey,
    "WRONG_API_KEY": InvalidApiKey,
    "NO_BALANCE": InsufficientBalance,
    "LOW_BALANCE": InsufficientBalance,
    "NO_MONEY": InsufficientBalance,
    "NO_NUMBERS": NoNumbersAvailable,
    "NO_NUMBER": NoNumbersAvailable,
    "NOT_FOUND": ActivationNotFound,
    "NO_ACTIVATION": ActivationNotFound,
    "WRONG_ACTIVATION_ID": ActivationNotFound,
    "CHANNELS_LIMIT": RateLimited,
    "BANNED": AccountBlocked,
    "ACCOUNT_INACTIVE": AccountBlocked,
    "SERVICE_NOT_AVAILABLE": AccountBlocked,
    "WRONG_SECURITY": AccountBlocked,
    "OTP_RECEIVED": OperationNotAllowed,
    "NEW_OTP_RECEIVED": OperationNotAllowed,
    "EARLY_CANCEL_DENIED": OperationNotAllowed,
    "FREE_CANCELLATION_EXPIRED": OperationNotAllowed,
    "ACTIVATION_NOT_ACTIVE": OperationNotAllowed,
    "SERVER_ERROR": ProviderUnavailable,
    "BAD_ACTION": ProviderAPIError,
    "BAD_STATUS": ProviderAPIError,
    "BAD_SERVICE": ProviderAPIError,
    "WRONG_SERVICE": ProviderAPIError,
    "WRONG_COUNTRY": ProviderAPIError,
    "WRONG_MAX_PRICE": ProviderAPIError,
    "UNPROCESSABLE_ENTITY": ProviderAPIError,
    "SQL_ERROR": ProviderAPIError,
    "ERROR_SQL": ProviderAPIError,
}

#: ``getStatus`` response prefix -> normalized :class:`ActivationStatus`.
SMS_ACTIVATE_STATUSES: Mapping[str, ActivationStatus] = {
    "STATUS_WAIT_CODE": ActivationStatus.WAITING,
    "STATUS_WAIT_RESEND": ActivationStatus.WAITING,
    "STATUS_WAIT_RETRY": ActivationStatus.WAITING_RETRY,
    "STATUS_OK": ActivationStatus.CODE_RECEIVED,
    "STATUS_CANCEL": ActivationStatus.CANCELLED,
}

_RETRY_BACKOFF = 0.5


@SmsProviderManager.register()
class SmsActivateCompatibleProvider(BaseSmsProvider):
    """Generic client for sms-activate-protocol (``handler_api.php``) services.

    To add a new compatible service, subclass and set ``name`` and
    ``base_url`` — no other code is required::

        @SmsProviderManager.register()
        class MyClone(SmsActivateCompatibleProvider):
            name = "my-clone"
            base_url = "https://my-clone.example/stubs/handler_api.php"

    A clone with error codes or status codes that differ from the vanilla
    protocol can override ``extra_error_map`` / ``extra_status_map``, which
    are merged on top of the base tables.

    ``proxy`` routes every request through an HTTP(S) or SOCKS proxy, e.g.
    ``"http://user:pass@host:port"`` or ``"socks5://host:port"`` (SOCKS
    requires the ``httpx[socks]`` extra). Mutually exclusive with ``client``
    - configure the proxy on your own client instead if you pass one.

    ``use_get_number_v2`` (class attribute, or constructor kwarg which wins
    when both are given) switches ``get_number`` to ``action=getNumberV2``,
    a JSON response that additionally reports ``country_phone_code`` and
    ``cost`` on the returned ``PhoneNumber``. Off by default: ``getNumber``
    is supported by every clone, V2 is not guaranteed everywhere. There is
    no automatic fallback from V2 to ``getNumber`` on error - ``get_number``
    is the one method that spends money, and retrying on a different action
    after an ambiguous V2 response risks buying a second number for an
    activation we couldn't even identify to cancel.
    """

    name: ClassVar[str] = "sms-activate-compatible"
    base_url: ClassVar[str] = ""
    api_key_param: ClassVar[str] = "api_key"
    action_param: ClassVar[str] = "action"
    default_country: ClassVar[str | int | None] = 0
    default_poll_interval: ClassVar[float] = 5.0
    use_get_number_v2: ClassVar[bool] = False

    extra_error_map: ClassVar[Mapping[str, type[SmsProviderError]]] = {}
    extra_status_map: ClassVar[Mapping[str, ActivationStatus]] = {}

    # Defaults for the base class itself; __init_subclass__ recomputes these
    # (merged with extra_error_map/extra_status_map) for every subclass.
    _error_map: ClassVar[Mapping[str, type[SmsProviderError]]] = SMS_ACTIVATE_ERRORS
    _status_map: ClassVar[Mapping[str, ActivationStatus]] = SMS_ACTIVATE_STATUSES

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._error_map = {**SMS_ACTIVATE_ERRORS, **cls.extra_error_map}
        cls._status_map = {**SMS_ACTIVATE_STATUSES, **cls.extra_status_map}

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        name: str | None = None,
        timeout: float = 15.0,
        poll_interval: float | None = None,
        max_retries: int = 2,
        min_request_interval: float = 0.0,
        extra_params: Mapping[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
        use_get_number_v2: bool | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        resolved_base_url = base_url or type(self).base_url
        if not resolved_base_url:
            raise ValueError("base_url is required")
        if client is not None and proxy is not None:
            raise ValueError(
                "pass proxy inside your custom client, not both client= and proxy="
            )
        if name is not None:
            self.name = name  # type: ignore[misc] # deliberate per-instance override of the ClassVar default
        self.base_url = resolved_base_url  # type: ignore[misc]
        self._api_key = api_key
        self.timeout = timeout
        self.poll_interval = (
            poll_interval if poll_interval is not None else self.default_poll_interval
        )
        self.max_retries = max_retries
        self.extra_params = dict(extra_params) if extra_params else {}
        self.use_get_number_v2 = (  # type: ignore[misc] # deliberate per-instance override, like self.name above
            use_get_number_v2 if use_get_number_v2 is not None else type(self).use_get_number_v2
        )
        self._throttle = Throttle(min_request_interval) if min_request_interval > 0 else None
        self._client, self._owns_client = build_client(timeout=timeout, client=client, proxy=proxy)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ---

    async def _request(self, action: str, **params: Any) -> str | dict[str, Any] | list[Any]:
        query = {
            self.api_key_param: self._api_key,
            self.action_param: action,
            **self.extra_params,
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
        body = response.text.strip()
        if body == "":
            return ""
        if body[0] in "{[":
            try:
                data: dict[str, Any] | list[Any] = json.loads(body)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(data, dict) and "title" in data:
                    title = str(data["title"])
                    self._raise_error(title, message=data.get("details"), raw=data)
                return data
        head = body.split(":", 1)[0].strip().upper()
        if head in self._error_map:
            self._raise_error(head, message=body, raw=body)
        if response.status_code >= 400:
            raise ProviderAPIError(
                f"HTTP {response.status_code}: {body[:200]}", provider=self.name, raw=body
            )
        return body

    def _raise_error(self, code: str, *, message: str | None = None, raw: Any = None) -> None:
        exc_cls = self._error_map.get(code, ProviderAPIError)
        raise exc_cls(message if message else code, provider=self.name, code=code, raw=raw)

    def _expect_prefix(self, body: Any, prefix: str) -> list[str]:
        if not isinstance(body, str):
            raise ProviderAPIError(
                f"unexpected response: {body!r}", provider=self.name, raw=body
            )
        parts = body.split(":")
        if parts[0] != prefix:
            raise ProviderAPIError(
                f"unexpected response: {body[:200]}", provider=self.name, raw=body
            )
        return parts[1:]

    # --- public API ---

    async def get_balance(self) -> Decimal:
        body = await self._request("getBalance")
        parts = self._expect_prefix(body, "ACCESS_BALANCE")
        if not parts:
            raise ProviderAPIError(
                f"unexpected response: {body!r}", provider=self.name, raw=body
            )
        try:
            return Decimal(parts[0])
        except InvalidOperation as exc:
            raise ProviderAPIError(
                f"unexpected balance value: {parts[0]!r}", provider=self.name, raw=body
            ) from exc

    async def get_number(
        self,
        service: str,
        country: str | int | None = None,
        *,
        operator: str | None = None,
        max_price: str | None = None,
        **extra: Any,
    ) -> PhoneNumber:
        country_value = country if country is not None else self.default_country
        # "maxPrice" passed via **extra (the API's native spelling) must not
        # collide with the max_price kwarg; the explicit kwarg wins.
        extra_max_price = extra.pop("maxPrice", None)
        if max_price is None:
            max_price = extra_max_price
        if self.use_get_number_v2:
            return await self._get_number_v2(
                service, country_value, operator=operator, max_price=max_price, **extra
            )
        body = await self._request(
            "getNumber",
            service=service,
            country=country_value,
            operator=operator,
            maxPrice=max_price,
            **extra,
        )
        parts = self._expect_prefix(body, "ACCESS_NUMBER")
        if len(parts) < 2:
            raise ProviderAPIError(
                f"unexpected response: {body!r}", provider=self.name, raw=body
            )
        activation_id, phone = parts[0], parts[1]
        return PhoneNumber(
            id=str(activation_id),
            phone=phone,
            provider=self.name,
            service=service,
            country=str(country_value) if country_value is not None else None,
            raw={"body": body},
        )

    async def _get_number_v2(
        self,
        service: str,
        country_value: str | int | None,
        *,
        operator: str | None,
        max_price: str | None,
        **extra: Any,
    ) -> PhoneNumber:
        """``action=getNumberV2`` - JSON response, reports ``country_phone_code``/``cost``.

        No fallback to ``getNumber`` on any error here - see the class
        docstring. ``NO_NUMBERS`` still arrives as plain text with HTTP 200
        and is already handled by :meth:`_request`'s own parser.
        """
        data = await self._request(
            "getNumberV2",
            service=service,
            country=country_value,
            operator=operator,
            maxPrice=max_price,
            **extra,
        )
        if not isinstance(data, dict) or "activationId" not in data or "phoneNumber" not in data:
            raise ProviderAPIError(
                f"unexpected getNumberV2 response: {data!r}", provider=self.name, raw=data
            )
        country_code = data.get("countryCode")
        country_phone_code = data.get("countryPhoneCode")
        raw_cost = data.get("activationCost")
        cost: Decimal | None
        if raw_cost is None:
            cost = None
        else:
            try:
                cost = Decimal(str(raw_cost))
            except InvalidOperation:
                cost = None
        return PhoneNumber(
            id=str(data["activationId"]),
            phone=str(data["phoneNumber"]),
            provider=self.name,
            service=service,
            country=(
                str(country_code)
                if country_code is not None
                else (str(country_value) if country_value is not None else None)
            ),
            country_phone_code=str(country_phone_code) if country_phone_code is not None else None,
            cost=cost,
            raw=data,
        )

    async def get_status(self, activation_id: str) -> ActivationStatus:
        response = await self._request("getStatus", id=str(activation_id))
        status, _code = self._parse_status(response)
        return status

    async def get_code(self, activation_id: str) -> SmsCode | None:
        response = await self._request("getStatus", id=str(activation_id))
        status, code = self._parse_status(response)
        if status is ActivationStatus.CANCELLED:
            raise ActivationCancelled(
                "activation was cancelled",
                status=ActivationStatus.CANCELLED,
                provider=self.name,
            )
        return code

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
        body = await self._request("setStatus", id=str(activation_id), status=3)
        if body == "":  # HTTP 204 empty body - success
            return
        self._expect_prefix(body, "ACCESS_RETRY_GET")

    async def finish(self, activation_id: str) -> None:
        body = await self._request("setStatus", id=str(activation_id), status=6)
        if body == "":  # HTTP 204 empty body - success
            return
        self._expect_prefix(body, "ACCESS_ACTIVATION")

    async def cancel(self, activation_id: str) -> None:
        body = await self._request("setStatus", id=str(activation_id), status=8)
        if body == "":  # HTTP 204 empty body - success
            return
        self._expect_prefix(body, "ACCESS_CANCEL")

    # --- discovery ---

    #: getServicesList/getCountries exist on vanilla sms-activate and HeroSMS, but an
    #: arbitrary clone may not implement them - these are the codes that mean "no such
    #: action here", as opposed to a real error that should propagate unchanged.
    _UNSUPPORTED_ACTION_CODES = frozenset({"BAD_ACTION", "ACTION_NOT_AVAILABLE"})

    async def get_services(self, country: str | int | None = None) -> list[Service]:
        try:
            data = await self._request("getServicesList", country=country)
        except ProviderAPIError as exc:
            if exc.code in self._UNSUPPORTED_ACTION_CODES:
                raise NotImplementedError(
                    f"{self.name} does not support getServicesList"
                ) from exc
            raise
        if not isinstance(data, dict) or data.get("status") != "success":
            raise ProviderAPIError(
                f"unexpected getServicesList response: {data!r}", provider=self.name, raw=data
            )
        services = data.get("services") or []
        result = []
        for item in services:
            if not isinstance(item, dict) or "code" not in item:
                raise ProviderAPIError(
                    f"unexpected service entry: {item!r}", provider=self.name, raw=data
                )
            result.append(Service(code=str(item["code"]), name=item.get("name"), raw=item))
        return result

    async def get_countries(self) -> list[Country]:
        try:
            data = await self._request("getCountries")
        except ProviderAPIError as exc:
            if exc.code in self._UNSUPPORTED_ACTION_CODES:
                raise NotImplementedError(f"{self.name} does not support getCountries") from exc
            raise
        # Live HeroSMS responds with a dict keyed by id; the official OpenAPI example
        # shows a list of the same objects instead - accept both.
        if isinstance(data, dict):
            pairs: list[tuple[str | None, Any]] = list(data.items())
        elif isinstance(data, list):
            pairs = [(None, item) for item in data]
        else:
            raise ProviderAPIError(
                f"unexpected getCountries response: {data!r}", provider=self.name, raw=data
            )
        result = []
        for key, item in pairs:
            if not isinstance(item, dict):
                raise ProviderAPIError(
                    f"unexpected country entry: {item!r}", provider=self.name, raw=data
                )
            item_id = item.get("id")
            code = str(item_id) if item_id is not None else (key or "")
            name = item.get("eng") or item.get("rus")
            result.append(Country(code=code, name=name, raw=item))
        return result

    # --- status/code parsing ---

    def _parse_status(
        self, response: str | dict[str, Any] | list[Any]
    ) -> tuple[ActivationStatus, SmsCode | None]:
        if isinstance(response, dict) and "verificationType" in response:
            return self._parse_verification_type_response(response)
        if not isinstance(response, str):
            raise ProviderAPIError(
                f"unexpected getStatus response: {response!r}", provider=self.name, raw=response
            )
        body = response
        head = body.split(":", 1)[0].strip().upper()
        status = self._status_map.get(head)
        if status is None:
            raise ProviderAPIError(
                f"unexpected getStatus response: {body[:200]}", provider=self.name, raw=body
            )
        if status is ActivationStatus.CODE_RECEIVED:
            parts = body.split(":", 1)
            code = parts[1] if len(parts) > 1 else ""
            if not code:
                # Malformed STATUS_OK with no code attached - nothing to hand back yet,
                # keep polling instead of returning an empty SmsCode.
                return status, None
            return status, SmsCode(code=code, raw={"body": body})
        # STATUS_WAIT_RETRY:{code} carries the PREVIOUS, already-rejected code.
        return status, None

    def _parse_verification_type_response(
        self, response: dict[str, Any]
    ) -> tuple[ActivationStatus, SmsCode | None]:
        verification_type = response.get("verificationType")
        if verification_type != 0:
            raise NotImplementedError(
                f"call/voice verification (verificationType={verification_type}) is not "
                "supported in v0.1.0; only SMS (verificationType=0)"
            )
        sms = response.get("sms") or {}
        code = sms.get("code")
        if not code:
            return ActivationStatus.WAITING, None
        return ActivationStatus.CODE_RECEIVED, SmsCode(
            code=str(code), text=sms.get("text"), raw=response
        )
