from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from conftest import make_client, responses

from sms_providers.base import (
    ActivationCancelled,
    ActivationNotFound,
    ActivationStatus,
    ActivationTimeout,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    RateLimited,
)
from sms_providers.providers import OnlineSimProvider


def make_provider(handler, **kwargs):
    return OnlineSimProvider(api_key="test-key", client=make_client(handler), **kwargs)


# 24. get_balance -> {"response":"1","balance":"0.000","zbalance":0} -> Decimal("0.000")
async def test_get_balance_parses_decimal_from_string():
    recorder = responses(
        httpx.Response(200, json={"response": "1", "balance": "0.000", "zbalance": 0})
    )
    provider = make_provider(recorder)
    assert await provider.get_balance() == Decimal("0.000")


# 25. ERROR_WRONG_KEY -> InvalidApiKey; ERROR_NO_OPERATIONS -> ActivationNotFound
async def test_wrong_key_raises_invalid_api_key():
    recorder = responses(httpx.Response(200, json={"response": "ERROR_WRONG_KEY"}))
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_balance()


async def test_no_operations_raises_activation_not_found():
    recorder = responses(httpx.Response(200, json={"response": "ERROR_NO_OPERATIONS"}))
    provider = make_provider(recorder)
    with pytest.raises(ActivationNotFound):
        await provider.get_status("1")


# 26. get_number with a number already in the response -> PhoneNumber, id == "10000"
async def test_get_number_with_number_in_response():
    recorder = responses(
        httpx.Response(200, json={"response": 1, "tzid": 10000, "number": "+79001112233"})
    )
    provider = make_provider(recorder)
    number = await provider.get_number(service="tg")
    assert isinstance(number, PhoneNumber)
    assert number.id == "10000"
    assert number.phone == "79001112233"


# 27. get_number without a number: getNum -> TZ_INPOOL -> TZ_NUM_WAIT with a number
async def test_get_number_polls_getstate_until_number_appears():
    recorder = responses(
        httpx.Response(200, json={"response": 1, "tzid": 10000}),
        httpx.Response(200, json=[{"response": "TZ_INPOOL", "tzid": 10000}]),
        httpx.Response(
            200, json=[{"response": "TZ_NUM_WAIT", "tzid": 10000, "number": "+79001112233"}]
        ),
    )
    provider = make_provider(recorder)
    number = await provider.get_number(service="tg")
    assert number.phone == "79001112233"
    assert recorder.call_count == 3


# 28. get_number does not get a number in time -> ActivationTimeout, setOperationOk was called
async def test_get_number_timeout_cancels_and_raises():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "getNum.php" in url:
            return httpx.Response(200, json={"response": 1, "tzid": 10000})
        if "getState.php" in url:
            return httpx.Response(200, json=[{"response": "TZ_INPOOL", "tzid": 10000}])
        if "setOperationOk.php" in url:
            return httpx.Response(200, json={"response": 1, "tzid": 10000})
        raise AssertionError(f"unexpected request: {url}")

    provider = make_provider(handler, number_timeout=0.05, poll_interval=0.01)
    with pytest.raises(ActivationTimeout):
        await provider.get_number(service="tg")
    assert any("setOperationOk.php" in url for url in calls)


# get_number stops waiting immediately on TZ_OVER_EMPTY, instead of looping until
# number_timeout - and raises ActivationCancelled(status=EXPIRED), not ActivationTimeout.
async def test_get_number_stops_immediately_on_tz_over_empty():
    recorder = responses(
        httpx.Response(200, json={"response": 1, "tzid": 10000}),
        httpx.Response(200, json=[{"response": "TZ_OVER_EMPTY", "tzid": 10000}]),
    )
    provider = make_provider(recorder, number_timeout=10, poll_interval=0.01)
    with pytest.raises(ActivationCancelled) as exc_info:
        await provider.get_number(service="tg")
    assert exc_info.value.status == ActivationStatus.EXPIRED
    assert recorder.call_count == 2  # did not keep polling despite number_timeout=10


# An empty getState list while waiting for a number means "not visible yet" - keep
# waiting - not ActivationNotFound.
async def test_get_number_treats_empty_getstate_list_as_still_waiting():
    recorder = responses(
        httpx.Response(200, json={"response": 1, "tzid": 10000}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{"response": "TZ_INPOOL", "tzid": 10000}]),
        httpx.Response(
            200, json=[{"response": "TZ_NUM_WAIT", "tzid": 10000, "number": "+79001112233"}]
        ),
    )
    provider = make_provider(recorder, number_timeout=10, poll_interval=0.001)
    number = await provider.get_number(service="tg")
    assert number.phone == "79001112233"
    assert recorder.call_count == 4


# get_number(..., number=...) must not raise a TypeError from a duplicate 'number' kwarg -
# our own number=1 always wins.
async def test_get_number_ignores_user_supplied_number_option():
    recorder = responses(
        httpx.Response(200, json={"response": 1, "tzid": 1, "number": "79001112233"})
    )
    provider = make_provider(recorder)
    number = await provider.get_number(service="tg", number=0)
    assert number.phone == "79001112233"
    assert recorder.requests[0].url.params["number"] == "1"


# A non-dict first element in a getState list is a parser error, not an AttributeError
async def test_getstate_non_dict_element_raises_provider_api_error():
    recorder = responses(httpx.Response(200, json=["not-a-dict"]))
    provider = make_provider(recorder)
    with pytest.raises(ProviderAPIError):
        await provider.get_status("1")


# 29. NO_NUMBER -> NoNumbersAvailable; EXCEEDED_CONCURRENT_OPERATIONS -> RateLimited
async def test_no_number_raises_no_numbers_available():
    recorder = responses(httpx.Response(200, json={"response": "NO_NUMBER"}))
    provider = make_provider(recorder)
    with pytest.raises(NoNumbersAvailable):
        await provider.get_number(service="tg")


async def test_exceeded_concurrent_operations_raises_rate_limited():
    recorder = responses(httpx.Response(200, json={"response": "EXCEEDED_CONCURRENT_OPERATIONS"}))
    provider = make_provider(recorder)
    with pytest.raises(RateLimited):
        await provider.get_number(service="tg")


# 30. get_status - the whole status table, including TZ_POOL
async def test_get_status_covers_all_statuses():
    recorder = responses(
        httpx.Response(200, json=[{"response": "TZ_INPOOL"}]),
        httpx.Response(200, json=[{"response": "TZ_POOL"}]),
        httpx.Response(200, json=[{"response": "TZ_NUM_WAIT"}]),
        httpx.Response(200, json=[{"response": "TZ_NUM_ANSWER", "msg": "1234"}]),
        httpx.Response(200, json=[{"response": "TZ_OVER_OK"}]),
        httpx.Response(200, json=[{"response": "TZ_OVER_EMPTY"}]),
        httpx.Response(200, json=[{"response": 1}]),
    )
    provider = make_provider(recorder)
    assert await provider.get_status("1") == ActivationStatus.PENDING
    assert await provider.get_status("1") == ActivationStatus.PENDING
    assert await provider.get_status("1") == ActivationStatus.WAITING
    assert await provider.get_status("1") == ActivationStatus.CODE_RECEIVED
    assert await provider.get_status("1") == ActivationStatus.FINISHED
    assert await provider.get_status("1") == ActivationStatus.EXPIRED
    assert await provider.get_status("1") == ActivationStatus.WAITING


# 31. wait_code: TZ_NUM_WAIT (msg: false) -> TZ_NUM_ANSWER (msg: "12345") -> SmsCode("12345")
async def test_wait_code_polls_until_answer():
    recorder = responses(
        httpx.Response(200, json=[{"response": "TZ_NUM_WAIT", "msg": False}]),
        httpx.Response(200, json=[{"response": "TZ_NUM_ANSWER", "msg": "12345"}]),
    )
    provider = make_provider(recorder)
    code = await provider.wait_code("1", timeout=5, poll_interval=0.001)
    assert code.code == "12345"
    assert recorder.call_count == 2


# 32. wait_code -> TZ_OVER_EMPTY -> ActivationCancelled with status == EXPIRED
async def test_wait_code_over_empty_raises_activation_cancelled_expired():
    recorder = responses(httpx.Response(200, json=[{"response": "TZ_OVER_EMPTY"}]))
    provider = make_provider(recorder)
    with pytest.raises(ActivationCancelled) as exc_info:
        await provider.wait_code("1", timeout=5, poll_interval=0.001)
    assert exc_info.value.status == ActivationStatus.EXPIRED


# 33. finish -> {"response":1}; NO_COMPLETE_TZID -> OperationNotAllowed
async def test_finish_success():
    recorder = responses(httpx.Response(200, json={"response": 1, "tzid": 1}))
    provider = make_provider(recorder)
    await provider.finish("1")  # no exception


async def test_finish_no_complete_tzid_raises_operation_not_allowed():
    recorder = responses(httpx.Response(200, json={"response": "NO_COMPLETE_TZID"}))
    provider = make_provider(recorder)
    with pytest.raises(OperationNotAllowed):
        await provider.finish("1")


# 34. cancel calls setOperationOk.php with the right tzid
async def test_cancel_calls_set_operation_ok_with_tzid():
    recorder = responses(httpx.Response(200, json={"response": 1, "tzid": 777}))
    provider = make_provider(recorder)
    await provider.cancel("777")
    request = recorder.requests[0]
    assert "setOperationOk.php" in str(request.url)
    assert request.url.params["tzid"] == "777"


# 35. throttling: 4 concurrent requests at min_request_interval=1.0 must all be
# serialized through the lock - asyncio.sleep is called exactly 3 times (n-1).
# A sequential version would not catch a regression to the pre-lock Throttle.
async def test_throttle_serializes_concurrent_requests(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("sms_providers._http.asyncio.sleep", fake_sleep)
    recorder = responses(
        *(
            httpx.Response(200, json={"response": "1", "balance": "1.0", "zbalance": 0})
            for _ in range(4)
        )
    )
    provider = make_provider(recorder, min_request_interval=1.0)
    await asyncio.gather(*(provider.get_balance() for _ in range(4)))
    assert len(sleeps) == 3
    assert all(0.0 < s <= 1.0 for s in sleeps)


# 36. A getState list is not treated as an error, even if an element has "response": "TZ_NUM_WAIT"
async def test_getstate_list_with_response_field_is_not_treated_as_error():
    recorder = responses(httpx.Response(200, json=[{"response": "TZ_NUM_WAIT", "tzid": 1}]))
    provider = make_provider(recorder)
    assert await provider.get_status("1") == ActivationStatus.WAITING


# proxy= is forwarded to the httpx.AsyncClient this provider builds for itself.
# httpx mounts the proxy transport per URL pattern in Client._mounts, not on
# Client._transport (which stays a plain, non-proxying transport).
async def test_proxy_is_passed_to_httpx_client():
    provider = OnlineSimProvider(api_key="test-key", proxy="http://user:pass@127.0.0.1:8080")
    try:
        mounted = [t for t in provider._client._mounts.values() if t is not None]
        assert mounted, "expected the proxy to be mounted on the client"
        assert type(mounted[0]._pool).__name__ == "AsyncHTTPProxy"
    finally:
        await provider.aclose()


# proxy= and client= are mutually exclusive; the proxy URL (a secret, like api_key)
# must not leak into the error message
async def test_proxy_and_client_together_raises_value_error():
    client = make_client(responses())
    with pytest.raises(ValueError) as exc_info:
        OnlineSimProvider(
            api_key="test-key",
            client=client,
            proxy="http://secret-user:secret-pass@127.0.0.1:8080",
        )
    assert "secret-pass" not in str(exc_info.value)


# 55. get_services on a live getTariffs response -> Service with count/price parsed;
# an element with no "slug" falls back to the dict key without its leading "_"
async def test_get_services_from_live_tariffs_response():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "response": "1",
                "countries": {},
                "services": {
                    "_vkcom": {
                        "id": 1,
                        "count": 9839,
                        "price": "1.00",
                        "service": "ВКонтакте + Mail.ru",
                        "slug": "vkcom",
                    },
                    "_3223": {
                        "id": 2,
                        "count": 5,
                        "price": "2.50",
                        "service": "Other",
                    },
                },
            },
        )
    )
    provider = make_provider(recorder)
    services = await provider.get_services()
    assert len(services) == 2
    first = next(s for s in services if s.code == "vkcom")
    assert first.name == "ВКонтакте + Mail.ru"
    assert first.count == 9839
    assert first.price == Decimal("1.00")
    second = next(s for s in services if s.code == "3223")
    assert second.price == Decimal("2.50")


# 56. get_services(country=49) hits getTariffs.php with country=49
async def test_get_services_puts_country_in_query():
    recorder = responses(
        httpx.Response(200, json={"response": "1", "services": {}, "countries": {}})
    )
    provider = make_provider(recorder)
    await provider.get_services(country=49)
    request = recorder.requests[0]
    assert "getTariffs.php" in str(request.url)
    assert request.url.params["country"] == "49"


# 57. "services": {} -> [] without an exception (real case: country=7)
async def test_get_services_empty_dict_returns_empty_list():
    recorder = responses(
        httpx.Response(200, json={"response": "1", "services": {}, "countries": {}})
    )
    provider = make_provider(recorder)
    assert await provider.get_services() == []


# 58. get_countries -> Country(code="49", name="Германия"), "original" stays in raw
async def test_get_countries_from_live_tariffs_response():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "response": "1",
                "services": {},
                "countries": {
                    "_49": {
                        "name": "Германия",
                        "original": "germany",
                        "code": 49,
                        "pos": 0,
                        "other": False,
                        "new": False,
                        "enable": True,
                    },
                },
            },
        )
    )
    provider = make_provider(recorder)
    countries = await provider.get_countries()
    assert len(countries) == 1
    assert countries[0].code == "49"
    assert countries[0].name == "Германия"
    assert countries[0].raw["original"] == "germany"


# 59. missing "services"/"countries" keys -> []
async def test_get_services_and_get_countries_missing_keys_return_empty_list():
    recorder = responses(httpx.Response(200, json={"response": "1"}))
    provider = make_provider(recorder)
    assert await provider.get_services() == []

    recorder2 = responses(httpx.Response(200, json={"response": "1"}))
    provider2 = make_provider(recorder2)
    assert await provider2.get_countries() == []


# 60. {"response":"ERROR_WRONG_KEY"} on getTariffs -> InvalidApiKey
async def test_get_services_wrong_key_raises_invalid_api_key():
    recorder = responses(httpx.Response(200, json={"response": "ERROR_WRONG_KEY"}))
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_services()
