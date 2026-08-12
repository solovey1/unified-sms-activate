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
    InsufficientBalance,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    ProviderUnavailable,
    RateLimited,
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
    """

    name: ClassVar[str] = "sms-activate-compatible"
    base_url: ClassVar[str] = ""
    api_key_param: ClassVar[str] = "api_key"
    action_param: ClassVar[str] = "action"
    default_country: ClassVar[str | int | None] = 0
    default_poll_interval: ClassVar[float] = 5.0

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
        client: httpx.Client | None = None,
        proxy: str | None = None,
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
        self._throttle = Throttle(min_request_interval) if min_request_interval > 0 else None
        self._client, self._owns_client = build_client(timeout=timeout, client=client, proxy=proxy)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r})"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- transport ---

    def _request(self, action: str, **params: Any) -> str | dict[str, Any] | list[Any]:
        query = {
            self.api_key_param: self._api_key,
            self.action_param: action,
            **self.extra_params,
            **{k: v for k, v in params.items() if v is not None},
        }
        response = request_with_retry(
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

    def get_balance(self) -> Decimal:
        body = self._request("getBalance")
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

    def get_number(
        self,
        service: str,
        country: str | int | None = None,
        *,
        operator: str | None = None,
        max_price: str | None = None,
        **extra: Any,
    ) -> PhoneNumber:
        country_value = country if country is not None else self.default_country
        body = self._request(
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

    def get_status(self, activation_id: str) -> ActivationStatus:
        response = self._request("getStatus", id=str(activation_id))
        status, _code = self._parse_status(response)
        return status

    def get_code(self, activation_id: str) -> SmsCode | None:
        response = self._request("getStatus", id=str(activation_id))
        status, code = self._parse_status(response)
        if status is ActivationStatus.CANCELLED:
            raise ActivationCancelled(
                "activation was cancelled",
                status=ActivationStatus.CANCELLED,
                provider=self.name,
            )
        return code

    def wait_code(
        self,
        activation_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> SmsCode:
        activation_id = str(activation_id)
        resolved_timeout = timeout if timeout is not None else self.default_timeout
        resolved_interval = poll_interval if poll_interval is not None else self.poll_interval
        return self._poll(
            lambda: self.get_code(activation_id),
            timeout=resolved_timeout,
            interval=resolved_interval,
        )

    def request_retry(self, activation_id: str) -> None:
        body = self._request("setStatus", id=str(activation_id), status=3)
        if body == "":  # HTTP 204 empty body - success
            return
        self._expect_prefix(body, "ACCESS_RETRY_GET")

    def finish(self, activation_id: str) -> None:
        body = self._request("setStatus", id=str(activation_id), status=6)
        if body == "":  # HTTP 204 empty body - success
            return
        self._expect_prefix(body, "ACCESS_ACTIVATION")

    def cancel(self, activation_id: str) -> None:
        body = self._request("setStatus", id=str(activation_id), status=8)
        if body == "":  # HTTP 204 empty body - success
            return
        self._expect_prefix(body, "ACCESS_CANCEL")

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
