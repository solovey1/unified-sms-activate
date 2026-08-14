"""Client for the OnlineSim API (https://onlinesim.io) - not sms-activate-protocol."""

from __future__ import annotations

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
    ActivationTimeout,
    BaseSmsProvider,
    Country,
    CountryPrice,
    InsufficientBalance,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    RateLimited,
    Service,
    SmsCode,
    SmsProviderError,
)
from sms_providers.manager import SmsProviderManager

#: Error codes reported in the ``response`` field of a JSON object response.
ONLINESIM_ERRORS: Mapping[str, type[SmsProviderError]] = {
    "ERROR_WRONG_KEY": InvalidApiKey,
    "ERROR_NO_KEY": InvalidApiKey,
    "ACCOUNT_BLOCKED": AccountBlocked,
    "API_ACCESS_DISABLED": AccountBlocked,
    "API_ACCESS_IP": AccountBlocked,
    "ACCOUNT_IDENTIFICATION_REQUIRED": AccountBlocked,
    "WARNING_LOW_BALANCE": InsufficientBalance,
    "NO_NUMBER": NoNumbersAvailable,
    "WARNING_NO_NUMS": NoNumbersAvailable,
    "SERVICE_TO_NUMBER_EMPTY": NoNumbersAvailable,
    "ERROR_NO_SERVICE_REPEAT": NoNumbersAvailable,
    "EXCEEDED_CONCURRENT_OPERATIONS": RateLimited,
    "INTERVAL_CONCURRENT_REQUESTS_ERROR": RateLimited,
    "TRY_AGAIN_LATER": RateLimited,
    "ERROR_NO_TZID": ActivationNotFound,
    "ERROR_WRONG_TZID": ActivationNotFound,
    "ERROR_NO_OPERATIONS": ActivationNotFound,
    "NO_COMPLETE_TZID": OperationNotAllowed,
    "NEED_EXTENSION_NUMBER": OperationNotAllowed,
    "NO_CONFIRM_FORWARD": OperationNotAllowed,
    "LIFICYCLE_NUM_EXPIRED": ActivationCancelled,
    "ERROR_NO_SERVICE": ProviderAPIError,
    "REQUEST_NOT_FOUND": ProviderAPIError,
    "ERROR_PARAMS": ProviderAPIError,
    "ERROR_NO_NUMBER": ProviderAPIError,
    "ERROR_NUMBERS_PARAMS": ProviderAPIError,
    "DUPLICATE_OPERATION": ProviderAPIError,
    "TIME_INTERVAL_ERROR": ProviderAPIError,
    "NO_FORWARD_FOR_DEFFER": ProviderAPIError,
    "NO_NUMBER_FOR_FORWARD": ProviderAPIError,
    "ERROR_LENGTH_NUMBER_FOR_FORWARD": ProviderAPIError,
}

#: Friendlier messages for a handful of error codes; falls back to the code itself.
_ONLINESIM_MESSAGES: Mapping[str, str] = {
    "NO_COMPLETE_TZID": (
        "OnlineSim forbids closing an operation during the protective interval "
        "(~2 minutes). Retry later or leave the operation to expire on its own "
        "(TZ_OVER_EMPTY); reserved funds are released either way."
    ),
}

#: ``response`` field of a ``getState`` element -> normalized :class:`ActivationStatus`.
ONLINESIM_STATUSES: Mapping[str, ActivationStatus] = {
    "TZ_INPOOL": ActivationStatus.PENDING,
    "TZ_POOL": ActivationStatus.PENDING,
    "TZ_NUM_WAIT": ActivationStatus.WAITING,
    "TZ_NUM_ANSWER": ActivationStatus.CODE_RECEIVED,
    "TZ_OVER_OK": ActivationStatus.FINISHED,
    "TZ_OVER_EMPTY": ActivationStatus.EXPIRED,
    "1": ActivationStatus.WAITING,  # defensive: older docs show "response": 1
}

_RETRY_BACKOFF = 1.0


@SmsProviderManager.register()
class OnlineSimProvider(BaseSmsProvider):
    """OnlineSim (https://onlinesim.io) - own protocol, not sms-activate-compatible.

    Rate limiting (confirmed by live testing: after ~6 fast requests the
    service starts dropping TCP connections):
    * ``min_request_interval`` throttles every request, including retries.
      Throttling is concurrency-safe: concurrent calls on the same provider
      instance are serialized through an internal ``asyncio.Lock``, so the
      minimum interval holds even when several coroutines call the provider
      at once.
    * ``poll_interval`` defaults to 8s (vs. 5s for sms-activate).
    * ``max_retries`` defaults to 3 with 1/2/4s backoff, retrying
      ``httpx.TransportError`` (including ``ReadError``/``ConnectError``/
      ``RemoteProtocolError``) — a dropped connection is expected behavior
      here.

    Known limitation - ``cancel()``: the single-service-activation API has
    no dedicated cancel endpoint, only ``setOperationOk`` (close/finish).
    ``cancel()`` calls it as a best-effort close; see its docstring.

    ``proxy`` routes every request through an HTTP(S) or SOCKS proxy, e.g.
    ``"http://user:pass@host:port"`` or ``"socks5://host:port"`` (SOCKS
    requires the ``httpx[socks]`` extra). Mutually exclusive with ``client``
    - configure the proxy on your own client instead if you pass one.
    """

    name: ClassVar[str] = "online-sim"
    base_url: ClassVar[str] = "https://onlinesim.io/api"
    default_country: ClassVar[int] = 7
    default_poll_interval: ClassVar[float] = 8.0

    _error_map: ClassVar[Mapping[str, type[SmsProviderError]]] = ONLINESIM_ERRORS
    _status_map: ClassVar[Mapping[str, ActivationStatus]] = ONLINESIM_STATUSES

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        name: str | None = None,
        lang: str = "en",
        timeout: float = 15.0,
        poll_interval: float | None = None,
        max_retries: int = 3,
        min_request_interval: float = 1.0,
        number_timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
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
        self.lang = lang
        self.timeout = timeout
        self.poll_interval = (
            poll_interval if poll_interval is not None else self.default_poll_interval
        )
        self.max_retries = max_retries
        self.number_timeout = number_timeout
        self._throttle = Throttle(min_request_interval)
        self._client, self._owns_client = build_client(timeout=timeout, client=client, proxy=proxy)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ---

    async def _request(self, method: str, **params: Any) -> dict[str, Any] | list[Any]:
        query = {
            "apikey": self._api_key,
            "lang": self.lang,
            **{k: v for k, v in params.items() if v is not None},
        }
        response = await request_with_retry(
            client=self._client,
            url=f"{self.base_url}/{method}.php",
            params=query,
            max_retries=self.max_retries,
            retry_backoff=_RETRY_BACKOFF,
            provider_name=self.name,
            throttle=self._throttle,
        )
        try:
            data: dict[str, Any] | list[Any] = response.json()
        except ValueError as exc:
            raise ProviderAPIError(
                f"invalid JSON response: {response.text[:200]}",
                provider=self.name,
                raw=response.text,
            ) from exc
        if isinstance(data, dict) and "response" in data and str(data["response"]) != "1":
            self._raise_error(str(data["response"]), raw=data)
        return data

    def _raise_error(self, code: str, *, raw: Any = None) -> None:
        exc_cls = self._error_map.get(code, ProviderAPIError)
        message = _ONLINESIM_MESSAGES.get(code, code)
        if exc_cls is ActivationCancelled:
            raise ActivationCancelled(
                message, status=ActivationStatus.EXPIRED, provider=self.name, code=code, raw=raw
            )
        raise exc_cls(message, provider=self.name, code=code, raw=raw)

    async def _getstate_elements(self, tzid: str) -> list[Any]:
        data = await self._request("getState", tzid=tzid, message_to_code=1)
        if not isinstance(data, list):
            raise ProviderAPIError(
                f"unexpected getState response: {data!r}", provider=self.name, raw=data
            )
        return data

    async def _state(self, tzid: str) -> dict[str, Any]:
        """Fetch the operation state (``getState``) and return its first element."""
        elements = await self._getstate_elements(tzid)
        if not elements:
            raise ActivationNotFound(
                f"no operation found for tzid={tzid}", provider=self.name, code=str(tzid)
            )
        element = elements[0]
        if not isinstance(element, dict):
            raise ProviderAPIError(
                f"unexpected getState element: {element!r}", provider=self.name, raw=elements
            )
        return element

    @staticmethod
    def _msg_text(item: Any) -> str | None:
        """One ``msg`` entry -> its string, or ``None``.

        With ``msg_list=1`` the entries are objects - ``{"service": "bybit",
        "msg": "468683"}`` - verified live; without it, ``msg`` is a plain
        string. Both forms are accepted here.
        """
        if isinstance(item, dict):
            item = item.get("msg")
        return item if isinstance(item, str) and item else None

    @classmethod
    def _extract_code(cls, msg: Any) -> str | None:
        """``msg`` can be ``false``, ``""``, ``None``, a string, or a list.

        The list form only appears with ``msg_list=1``, which neither
        :meth:`get_code` nor :meth:`wait_code` sends. It is handled anyway,
        and takes the FIRST entry: on a live activation holding several
        messages, the plain (non-list) ``msg`` the API returns for the same
        operation was exactly the first entry of that list, so the first is
        what the service itself calls the active message. ``orderby`` had no
        effect on that order.
        """
        if isinstance(msg, list):
            for item in msg:
                code = cls._msg_text(item)
                if code:
                    return code
            return None
        return cls._msg_text(msg)

    def _parse_state_element(
        self, element: Mapping[str, Any]
    ) -> tuple[ActivationStatus, SmsCode | None]:
        response = str(element.get("response"))
        status = self._status_map.get(response)
        if status is None:
            raise ProviderAPIError(
                f"unexpected getState status: {response!r}", provider=self.name, raw=element
            )
        if status is ActivationStatus.CODE_RECEIVED:
            code = self._extract_code(element.get("msg"))
            if code is not None:
                return status, SmsCode(code=code, raw=dict(element))
            return ActivationStatus.WAITING, None
        return status, None

    # --- public API ---

    async def get_balance(self) -> Decimal:
        data = await self._request("getBalance")
        if not isinstance(data, dict) or "balance" not in data:
            raise ProviderAPIError(
                f"unexpected getBalance response: {data!r}", provider=self.name, raw=data
            )
        try:
            return Decimal(str(data["balance"]))
        except InvalidOperation as exc:
            raise ProviderAPIError(
                f"unexpected balance value: {data['balance']!r}", provider=self.name, raw=data
            ) from exc

    async def get_number(
        self, service: str, country: str | int | None = None, **options: Any
    ) -> PhoneNumber:
        country_value = country if country is not None else self.default_country
        # number=1 (return the number in the getNum response) is not user-overridable.
        options = dict(options)
        options.pop("number", None)
        data = await self._request(
            "getNum", service=service, country=country_value, number=1, **options
        )
        if not isinstance(data, dict) or "tzid" not in data:
            raise ProviderAPIError(
                f"unexpected getNum response: {data!r}", provider=self.name, raw=data
            )
        tzid = str(data["tzid"])
        phone: str | None = data.get("number") or None
        # OnlineSim's country IS the E.164 phone prefix (no "+"): "7" for Russia,
        # "86" for China. Both PhoneNumber fields carry the same value unless the
        # getState poll below reports a more authoritative one.
        resolved_country = str(country_value) if country_value is not None else None
        if phone is None:
            try:
                phone, polled_country = await self._poll(
                    lambda: self._peek_number(tzid),
                    timeout=self.number_timeout,
                    interval=self.poll_interval,
                )
            except ActivationTimeout:
                try:
                    await self.cancel(tzid)
                except SmsProviderError:
                    pass
                raise
            if polled_country is not None:
                resolved_country = polled_country
        assert phone is not None
        return PhoneNumber(
            id=tzid,
            # OnlineSim returns numbers as "+79001234567"; the PhoneNumber
            # contract is digits only, no "+". The original value stays in raw.
            phone=phone.lstrip("+"),
            provider=self.name,
            service=service,
            country=resolved_country,
            country_phone_code=resolved_country,
            raw={"getNum": data},
        )

    async def _peek_number(self, tzid: str) -> tuple[str, str | None] | None:
        """Poll step used while waiting for ``getNum`` to allocate a number.

        Unlike :meth:`_state`, an empty element list here means the operation
        is not visible yet right after ``getNum`` - keep waiting instead of
        raising ``ActivationNotFound``. ``TZ_OVER_EMPTY`` means the operation
        already expired, so stop waiting immediately instead of looping until
        ``number_timeout``. Returns ``(phone, country)`` once a number is
        assigned - ``country`` is whatever ``getState`` reported for this
        element, ``None`` if it didn't include one.
        """
        elements = await self._getstate_elements(tzid)
        if not elements:
            return None
        element = elements[0]
        if not isinstance(element, dict):
            raise ProviderAPIError(
                f"unexpected getState element: {element!r}", provider=self.name, raw=elements
            )
        if str(element.get("response")) == "TZ_OVER_EMPTY":
            raise ActivationCancelled(
                "activation expired while waiting for a number",
                status=ActivationStatus.EXPIRED,
                provider=self.name,
            )
        number = element.get("number") or None
        if number is None:
            return None
        element_country = element.get("country")
        return number, (str(element_country) if element_country is not None else None)

    async def get_status(self, activation_id: str) -> ActivationStatus:
        element = await self._state(str(activation_id))
        status, _code = self._parse_state_element(element)
        return status

    async def get_code(self, activation_id: str) -> SmsCode | None:
        element = await self._state(str(activation_id))
        status, code = self._parse_state_element(element)
        if status is ActivationStatus.EXPIRED:
            raise ActivationCancelled(
                "activation expired without a code",
                status=ActivationStatus.EXPIRED,
                provider=self.name,
            )
        if status is ActivationStatus.FINISHED and code is None:
            raise ActivationCancelled(
                "activation finished without a code",
                status=ActivationStatus.FINISHED,
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
        await self._request("setOperationRevise", tzid=str(activation_id))

    async def finish(self, activation_id: str) -> None:
        await self._request("setOperationOk", tzid=str(activation_id))

    async def cancel(self, activation_id: str) -> None:
        """Best-effort cancel: OnlineSim has no dedicated cancel endpoint.

        The single-service-activation API only exposes ``getTariffs``,
        ``getNum``, ``getState``, ``setOperationRevise`` and
        ``setOperationOk`` - there is no separate "cancel" action. This
        calls ``setOperationOk`` (the same request as :meth:`finish`) as a
        best-effort close; ``zbalance`` (frozen funds) is released either
        way, whether the operation is closed successfully or left to expire
        on its own (``TZ_OVER_EMPTY``). If called during the protective
        interval (~2 minutes by default), the service raises
        :class:`OperationNotAllowed`.

        # TODO(v0.2): verify cancel() refund semantics on a live operation
        """
        await self._request("setOperationOk", tzid=str(activation_id))

    async def get_messages(self, activation_id: str) -> list[SmsCode]:
        """Every code received on this activation (``msg_list=1&message_to_code=1``).

        ``received_at`` is always ``None``: OnlineSim reports no per-message
        timestamp - ``getState``'s ``time`` is the operation's REMAINING
        seconds, not a receipt time (checked against the official OpenAPI
        spec AND a live response). ``text`` is ``None`` too, because
        ``message_to_code=1`` hands back only the extracted code; for the
        full text use :meth:`get_raw_messages`.

        The list is NOT de-duplicated: a live activation answered with the
        same code twice in a row (four entries, two distinct codes), and
        which of those repeats is a real second SMS is the caller's call.
        """
        items, _element = await self._msg_list(activation_id, message_to_code=1)
        return [
            SmsCode(code=code, raw=item if isinstance(item, dict) else {"msg": item})
            for item, code in ((item, self._msg_text(item)) for item in items)
            if code
        ]

    async def get_raw_messages(self, activation_id: str) -> list[Any]:
        """Return the raw list of SMS messages for the activation (full text included).

        Unlike :meth:`get_code`/:meth:`wait_code`/:meth:`get_messages`, which
        use ``message_to_code=1`` and therefore only see the extracted code,
        this uses ``msg_list=1&message_to_code=0`` to retrieve the message(s)
        exactly as the API returns them. The element shape is undocumented;
        live it is ``{"service": "bybit", "msg": "<full SMS text>"}`` - no
        timestamp anywhere - which is why the result stays raw.
        """
        items, _element = await self._msg_list(activation_id, message_to_code=0)
        return items

    async def _msg_list(
        self, activation_id: str, *, message_to_code: int
    ) -> tuple[list[Any], dict[str, Any]]:
        """``getState`` with ``msg_list=1`` -> (messages, the state element)."""
        data = await self._request(
            "getState", tzid=str(activation_id), msg_list=1, message_to_code=message_to_code
        )
        if not isinstance(data, list) or not data:
            raise ActivationNotFound(
                f"no operation found for tzid={activation_id}",
                provider=self.name,
                code=str(activation_id),
            )
        element = data[0]
        if not isinstance(element, dict):
            raise ProviderAPIError(
                f"unexpected getState element: {element!r}", provider=self.name, raw=data
            )
        msg = element.get("msg")
        if isinstance(msg, list):
            return msg, element
        return ([msg] if msg else []), element

    # --- discovery ---

    async def get_services(
        self, country: str | int | None = None, search: str | None = None
    ) -> list[Service]:
        """Services available from OnlineSim; optionally narrow by country.

        There is no dedicated services endpoint - this reads the ``services``
        key of ``getTariffs.php``, the same call used by :meth:`get_countries`.
        ``name`` is localized by the constructor's ``lang`` (default ``"en"``).
        Without ``search``, the API only returns its top ~30 services -
        ``search`` is forwarded as ``getTariffs.php``'s own ``filter=``
        parameter to reach anything beyond that.
        """
        data = await self._request("getTariffs", country=country, filter=search)
        services = self._tariffs_section(data, "services")
        if search is not None and country is None and not services:
            # getTariffs&filter=X without country= answers with the countries
            # where the service exists but an EMPTY services section (verified
            # live). Re-ask with the first of those countries - one extra
            # request, only on this branch.
            countries = self._tariffs_section(data, "countries")
            for item in countries.values():
                if isinstance(item, dict) and item.get("code") is not None:
                    data = await self._request(
                        "getTariffs", country=item["code"], filter=search
                    )
                    services = self._tariffs_section(data, "services")
                    break
        result = []
        for key, item in services.items():
            if not isinstance(item, dict):
                raise ProviderAPIError(
                    f"unexpected service entry: {item!r}", provider=self.name, raw=data
                )
            slug = item.get("slug")
            code = str(slug) if slug else key.lstrip("_")
            result.append(
                Service(
                    code=code,
                    name=item.get("service"),
                    count=item.get("count"),
                    price=self._parse_price(item.get("price")),
                    raw=item,
                )
            )
        return result

    async def get_countries(self) -> list[Country]:
        """Countries available from OnlineSim.

        There is no dedicated countries endpoint - this reads the
        ``countries`` key of ``getTariffs.php``, the same call used by
        :meth:`get_services`. ``name`` is localized by the constructor's
        ``lang`` (default ``"en"``).
        """
        data = await self._request("getTariffs")
        countries = self._tariffs_section(data, "countries")
        result = []
        for key, item in countries.items():
            if not isinstance(item, dict):
                raise ProviderAPIError(
                    f"unexpected country entry: {item!r}", provider=self.name, raw=data
                )
            country_code = item.get("code")
            code = str(country_code) if country_code is not None else key.lstrip("_")
            result.append(Country(code=code, name=item.get("name"), raw=item))
        return result

    async def get_prices(
        self, service: str | None = None, country: str | int | None = None
    ) -> list[CountryPrice]:
        """Where and for how much ``service`` is available, via ``getTariffs.php``.

        ``service`` is required: without a ``filter=``, ``getTariffs`` has no
        meaningful "all services" listing (see :meth:`get_services`'s
        docstring). Without ``country``, the API reports only which
        countries have the service available, with an empty ``services``
        section - so ``cost``/``count`` come back ``None`` (presence is
        known, volume/price is not). With ``country``, the ``services``
        section carries the real count/price for that one country.
        """
        if service is None:
            raise ValueError(
                "OnlineSim requires service= for get_prices() - getTariffs.php "
                "without a filter= has no meaningful 'all services' listing"
            )
        if country is None:
            data = await self._request("getTariffs", filter=service)
            countries = self._tariffs_section(data, "countries")
            result = []
            for key, item in countries.items():
                if not isinstance(item, dict):
                    raise ProviderAPIError(
                        f"unexpected country entry: {item!r}", provider=self.name, raw=data
                    )
                country_code = item.get("code")
                code = str(country_code) if country_code is not None else key.lstrip("_")
                result.append(CountryPrice(country=code, service=service, raw=item))
            return result
        data = await self._request("getTariffs", filter=service, country=country)
        services = self._tariffs_section(data, "services")
        result = []
        for key, item in services.items():
            if not isinstance(item, dict):
                raise ProviderAPIError(
                    f"unexpected service entry: {item!r}", provider=self.name, raw=data
                )
            slug = item.get("slug")
            code = str(slug) if slug else key.lstrip("_")
            result.append(
                CountryPrice(
                    country=str(country),
                    service=code,
                    cost=self._parse_price(item.get("price")),
                    count=item.get("count"),
                    raw=item,
                )
            )
        return result

    def _tariffs_section(self, data: Any, key: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ProviderAPIError(
                f"unexpected getTariffs response: {data!r}", provider=self.name, raw=data
            )
        section = data.get(key) or {}
        if not isinstance(section, dict):
            raise ProviderAPIError(
                f"unexpected {key!r} value: {section!r}", provider=self.name, raw=data
            )
        return section

    @staticmethod
    def _parse_price(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
