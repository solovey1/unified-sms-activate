from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from conftest import make_client, responses

from sms_providers.base import (
    AccountBlocked,
    ActivationCancelled,
    ActivationNotFound,
    ActivationStatus,
    ActivationTimeout,
    InsufficientBalance,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    ProviderUnavailable,
    RateLimited,
)
from sms_providers.manager import SmsProviderManager
from sms_providers.providers.sms_activate_compatible import SmsActivateCompatibleProvider


def make_provider(handler, **kwargs):
    kwargs.setdefault("base_url", "https://sms-activate.example/stubs/handler_api.php")
    return SmsActivateCompatibleProvider(api_key="test-key", client=make_client(handler), **kwargs)


# 1. get_balance -> ACCESS_BALANCE:2.225 -> Decimal("2.225")
async def test_get_balance_parses_decimal():
    recorder = responses(httpx.Response(200, text="ACCESS_BALANCE:2.225"))
    provider = make_provider(recorder)
    assert await provider.get_balance() == Decimal("2.225")


# 2. get_balance -> JSON BAD_KEY + 401 -> InvalidApiKey
async def test_get_balance_bad_key_json_401_raises_invalid_api_key():
    recorder = responses(httpx.Response(401, json={"title": "BAD_KEY", "details": "Unauthorized"}))
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_balance()


# 3. get_balance -> plain BAD_KEY -> InvalidApiKey (vanilla clone)
async def test_get_balance_bad_key_plain_text_raises_invalid_api_key():
    recorder = responses(httpx.Response(200, text="BAD_KEY"))
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_balance()


# 4. get_number -> ACCESS_NUMBER:123:79001112233 -> PhoneNumber fields, id is str
async def test_get_number_returns_phone_number_with_string_id():
    recorder = responses(httpx.Response(200, text="ACCESS_NUMBER:123:79001112233"))
    provider = make_provider(recorder)
    number = await provider.get_number(service="tg", country=7)
    assert number == PhoneNumber(
        id="123",
        phone="79001112233",
        provider=provider.name,
        service="tg",
        country="7",
        raw={"body": "ACCESS_NUMBER:123:79001112233"},
    )
    assert isinstance(number.id, str)


# 5. get_number -> NO_NUMBERS (HTTP 200) -> NoNumbersAvailable
async def test_get_number_no_numbers_http_200_raises_no_numbers_available():
    recorder = responses(httpx.Response(200, text="NO_NUMBERS"))
    provider = make_provider(recorder)
    with pytest.raises(NoNumbersAvailable):
        await provider.get_number(service="tg")


# 6. get_number -> JSON NO_BALANCE + 402 -> InsufficientBalance
async def test_get_number_no_balance_raises_insufficient_balance():
    recorder = responses(
        httpx.Response(402, json={"title": "NO_BALANCE", "details": "Not enough funds"})
    )
    provider = make_provider(recorder)
    with pytest.raises(InsufficientBalance):
        await provider.get_number(service="tg")


# 7. get_number -> JSON CHANNELS_LIMIT + 403 -> RateLimited; BANNED -> AccountBlocked
async def test_get_number_channels_limit_raises_rate_limited():
    recorder = responses(
        httpx.Response(403, json={"title": "CHANNELS_LIMIT", "details": "too many channels"})
    )
    provider = make_provider(recorder)
    with pytest.raises(RateLimited):
        await provider.get_number(service="tg")


async def test_get_number_banned_raises_account_blocked():
    recorder = responses(httpx.Response(403, json={"title": "BANNED", "details": "banned"}))
    provider = make_provider(recorder)
    with pytest.raises(AccountBlocked):
        await provider.get_number(service="tg")


# 8. get_number puts service/country/maxPrice in query; country=None takes default_country
async def test_get_number_query_params_and_default_country():
    recorder = responses(httpx.Response(200, text="ACCESS_NUMBER:1:79000000000"))
    provider = make_provider(recorder)
    await provider.get_number(service="tg", country=7, operator="mts", max_price="10.5")
    params = recorder.requests[0].url.params
    assert params["service"] == "tg"
    assert params["country"] == "7"
    assert params["operator"] == "mts"
    assert params["maxPrice"] == "10.5"

    recorder2 = responses(httpx.Response(200, text="ACCESS_NUMBER:2:79000000001"))
    provider2 = make_provider(recorder2)
    await provider2.get_number(service="tg")
    assert recorder2.requests[0].url.params["country"] == "0"


# 9. get_status - all five variants from the status table
async def test_get_status_covers_all_statuses():
    recorder = responses(
        httpx.Response(200, text="STATUS_WAIT_CODE"),
        httpx.Response(200, text="STATUS_WAIT_RESEND"),
        httpx.Response(200, text="STATUS_WAIT_RETRY:111"),
        httpx.Response(200, text="STATUS_OK:12345"),
        httpx.Response(200, text="STATUS_CANCEL"),
    )
    provider = make_provider(recorder)
    assert await provider.get_status("1") == ActivationStatus.WAITING
    assert await provider.get_status("1") == ActivationStatus.WAITING
    assert await provider.get_status("1") == ActivationStatus.WAITING_RETRY
    assert await provider.get_status("1") == ActivationStatus.CODE_RECEIVED
    assert await provider.get_status("1") == ActivationStatus.CANCELLED


# 10. get_code on STATUS_WAIT_RETRY:111 -> None
async def test_get_code_on_wait_retry_returns_none():
    recorder = responses(httpx.Response(200, text="STATUS_WAIT_RETRY:111"))
    provider = make_provider(recorder)
    assert await provider.get_code("1") is None


# STATUS_OK with no code attached is malformed - treat as "no code yet", not SmsCode("")
async def test_get_code_status_ok_without_code_returns_none():
    recorder = responses(httpx.Response(200, text="STATUS_OK:"))
    provider = make_provider(recorder)
    assert await provider.get_code("1") is None


# 11. wait_code: WAIT_CODE, WAIT_CODE, OK:12345 -> SmsCode("12345"), exactly 3 requests
async def test_wait_code_polls_until_code_received():
    recorder = responses(
        httpx.Response(200, text="STATUS_WAIT_CODE"),
        httpx.Response(200, text="STATUS_WAIT_CODE"),
        httpx.Response(200, text="STATUS_OK:12345"),
    )
    provider = make_provider(recorder)
    code = await provider.wait_code("1", timeout=5, poll_interval=0.001)
    assert code.code == "12345"
    assert recorder.call_count == 3


# 12. wait_code -> STATUS_CANCEL -> ActivationCancelled, status == CANCELLED
async def test_wait_code_status_cancel_raises_activation_cancelled():
    recorder = responses(httpx.Response(200, text="STATUS_CANCEL"))
    provider = make_provider(recorder)
    with pytest.raises(ActivationCancelled) as exc_info:
        await provider.wait_code("1", timeout=5, poll_interval=0.001)
    assert exc_info.value.status == ActivationStatus.CANCELLED


# 13. wait_code with timeout=0.1 and endless WAIT_CODE -> ActivationTimeout
async def test_wait_code_local_timeout():
    def endless_wait_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="STATUS_WAIT_CODE")

    provider = make_provider(endless_wait_handler)
    with pytest.raises(ActivationTimeout):
        await provider.wait_code("1", timeout=0.1, poll_interval=0.001)


# 14. cancel sends status=8; finish sends status=6; request_retry sends status=3
async def test_cancel_finish_request_retry_send_correct_status():
    recorder = responses(httpx.Response(200, text="ACCESS_CANCEL"))
    provider = make_provider(recorder)
    await provider.cancel("42")
    assert recorder.requests[0].url.params["status"] == "8"

    recorder2 = responses(httpx.Response(200, text="ACCESS_ACTIVATION"))
    provider2 = make_provider(recorder2)
    await provider2.finish("42")
    assert recorder2.requests[0].url.params["status"] == "6"

    recorder3 = responses(httpx.Response(200, text="ACCESS_RETRY_GET"))
    provider3 = make_provider(recorder3)
    await provider3.request_retry("42")
    assert recorder3.requests[0].url.params["status"] == "3"


# An empty body (HTTP 204) on setStatus is success, not a parse error
async def test_cancel_finish_request_retry_accept_empty_body_as_success():
    recorder = responses(httpx.Response(204, text=""))
    provider = make_provider(recorder)
    await provider.cancel("1")  # no exception

    recorder2 = responses(httpx.Response(204, text=""))
    provider2 = make_provider(recorder2)
    await provider2.finish("1")  # no exception

    recorder3 = responses(httpx.Response(204, text=""))
    provider3 = make_provider(recorder3)
    await provider3.request_retry("1")  # no exception


# 15. cancel -> JSON OTP_RECEIVED + 409 -> OperationNotAllowed; NOT_FOUND + 404 -> ActivationNotFound
async def test_cancel_operation_not_allowed():
    recorder = responses(
        httpx.Response(409, json={"title": "OTP_RECEIVED", "details": "code already delivered"})
    )
    provider = make_provider(recorder)
    with pytest.raises(OperationNotAllowed):
        await provider.cancel("42")


async def test_cancel_activation_not_found():
    recorder = responses(
        httpx.Response(404, json={"title": "NOT_FOUND", "details": "Activation Not Found"})
    )
    provider = make_provider(recorder)
    with pytest.raises(ActivationNotFound):
        await provider.cancel("42")


# 16. 500 SERVER_ERROR, then success -> result returned, 2 requests
async def test_retries_on_5xx_then_succeeds():
    recorder = responses(
        httpx.Response(500, text="SERVER_ERROR"),
        httpx.Response(200, text="ACCESS_BALANCE:1.000"),
    )
    provider = make_provider(recorder)
    assert await provider.get_balance() == Decimal("1.000")
    assert recorder.call_count == 2


# 17. Three consecutive httpx.ConnectError -> ProviderUnavailable
async def test_three_connect_errors_raise_provider_unavailable():
    recorder = responses(
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderUnavailable):
        await provider.get_balance()
    assert recorder.call_count == 3


# api_key must not leak into ProviderUnavailable after exhausting retries, via either
# a chain of transport errors or a chain of 5xx responses.
async def test_provider_unavailable_after_connect_errors_does_not_leak_api_key():
    recorder = responses(
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderUnavailable) as exc_info:
        await provider.get_balance()
    exc = exc_info.value
    assert "test-key" not in str(exc)
    assert "test-key" not in repr(exc)
    assert "test-key" not in str(exc.raw)


async def test_provider_unavailable_after_5xx_does_not_leak_api_key():
    recorder = responses(
        httpx.Response(500, text="SERVER_ERROR"),
        httpx.Response(500, text="SERVER_ERROR"),
        httpx.Response(500, text="SERVER_ERROR"),
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderUnavailable) as exc_info:
        await provider.get_balance()
    exc = exc_info.value
    assert "test-key" not in str(exc)
    assert "test-key" not in repr(exc)
    assert "test-key" not in str(exc.raw)


# 18. verificationType 1 -> NotImplementedError; verificationType 0 -> SmsCode from sms.code
async def test_verification_type_not_zero_raises_not_implemented_error():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "verificationType": 1,
                "call": {"from": "1", "text": "x", "code": "x", "dateTime": "x", "url": "x",
                         "parsingCount": 1},
            },
        )
    )
    provider = make_provider(recorder)
    with pytest.raises(NotImplementedError):
        await provider.get_status("1")


async def test_verification_type_zero_returns_sms_code():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "verificationType": 0,
                "sms": {"dateTime": "now", "code": "5551", "text": "Your code is 5551"},
            },
        )
    )
    provider = make_provider(recorder)
    code = await provider.get_code("1")
    assert code.code == "5551"
    assert code.text == "Your code is 5551"


# 19. api_key present in query of every request, absent from repr(provider)
async def test_api_key_in_query_not_in_repr():
    recorder = responses(httpx.Response(200, text="ACCESS_BALANCE:1.0"))
    provider = make_provider(recorder)
    await provider.get_balance()
    assert recorder.requests[0].url.params["api_key"] == "test-key"
    assert "test-key" not in repr(provider)


# 20. A 3-line clone works across the whole contract
@SmsProviderManager.register()
class _ThreeLineClone(SmsActivateCompatibleProvider):
    """A minimal sms-activate-compatible clone, per the README/spec example."""

    name = "test-three-line-clone"
    base_url = "https://three-line-clone.example/stubs/handler_api.php"


async def test_three_line_clone_covers_full_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        status_param = request.url.params.get("status")
        if action == "getBalance":
            return httpx.Response(200, text="ACCESS_BALANCE:9.500")
        if action == "getNumber":
            return httpx.Response(200, text="ACCESS_NUMBER:555:79995551122")
        if action == "getStatus":
            return httpx.Response(200, text="STATUS_OK:9999")
        if action == "setStatus" and status_param == "3":
            return httpx.Response(200, text="ACCESS_RETRY_GET")
        if action == "setStatus" and status_param == "6":
            return httpx.Response(200, text="ACCESS_ACTIVATION")
        if action == "setStatus" and status_param == "8":
            return httpx.Response(200, text="ACCESS_CANCEL")
        raise AssertionError(f"unexpected action {action!r}")

    provider = _ThreeLineClone(api_key="test-key", client=make_client(handler))
    assert await provider.get_balance() == Decimal("9.500")
    number = await provider.get_number(service="tg")
    assert number.id == "555"
    assert number.phone == "79995551122"
    assert await provider.get_status(number.id) == ActivationStatus.CODE_RECEIVED
    code = await provider.wait_code(number.id, timeout=1, poll_interval=0.01)
    assert code.code == "9999"
    await provider.request_retry(number.id)
    await provider.finish(number.id)
    await provider.cancel(number.id)


# 21. A user-supplied httpx.AsyncClient is not closed by provider.aclose()
async def test_custom_client_not_closed_by_provider_close():
    client = make_client(responses(httpx.Response(200, text="ACCESS_BALANCE:1.0")))
    provider = SmsActivateCompatibleProvider(
        api_key="test-key",
        base_url="https://sms-activate.example/stubs/handler_api.php",
        client=client,
    )
    await provider.aclose()
    assert client.is_closed is False


# proxy= is forwarded to the httpx.AsyncClient this provider builds for itself.
# httpx mounts the proxy transport per URL pattern in Client._mounts, not on
# Client._transport (which stays a plain, non-proxying transport).
async def test_proxy_is_passed_to_httpx_client():
    provider = SmsActivateCompatibleProvider(
        api_key="test-key",
        base_url="https://sms-activate.example/stubs/handler_api.php",
        proxy="http://user:pass@127.0.0.1:8080",
    )
    try:
        mounted = [t for t in provider._client._mounts.values() if t is not None]
        assert mounted, "expected the proxy to be mounted on the client"
        assert type(mounted[0]._pool).__name__ == "AsyncHTTPProxy"
    finally:
        await provider.aclose()


# proxy= and client= are mutually exclusive; the proxy URL (a secret, like api_key)
# must not leak into the error message
def test_proxy_and_client_together_raises_value_error():
    client = make_client(responses())
    with pytest.raises(ValueError) as exc_info:
        SmsActivateCompatibleProvider(
            api_key="test-key",
            base_url="https://sms-activate.example/stubs/handler_api.php",
            client=client,
            proxy="http://secret-user:secret-pass@127.0.0.1:8080",
        )
    assert "secret-pass" not in str(exc_info.value)


# 49. get_services on {"status":"success","services":[...]} -> two Service, order preserved
async def test_get_services_returns_services_in_order():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "status": "success",
                "services": [
                    {"code": "tg", "name": "Telegram"},
                    {"code": "wa", "name": "Whatsapp"},
                ],
            },
        )
    )
    provider = make_provider(recorder)
    services = await provider.get_services()
    assert [s.code for s in services] == ["tg", "wa"]
    assert services[0].name == "Telegram"
    assert services[1].name == "Whatsapp"


# 50. get_services(country=0) puts country in query, action is getServicesList
async def test_get_services_puts_country_in_query():
    recorder = responses(httpx.Response(200, json={"status": "success", "services": []}))
    provider = make_provider(recorder)
    await provider.get_services(country=0)
    params = recorder.requests[0].url.params
    assert params["action"] == "getServicesList"
    assert params["country"] == "0"


# 51. get_services at {"status":"false"} -> ProviderAPIError
async def test_get_services_status_not_success_raises_provider_api_error():
    recorder = responses(httpx.Response(200, json={"status": "false"}))
    provider = make_provider(recorder)
    with pytest.raises(ProviderAPIError):
        await provider.get_services()


# 52. get_countries on the live dict-keyed form -> Country(code="1", name="Ukraine")
async def test_get_countries_dict_form():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "1": {
                    "id": 1,
                    "rus": "Украина",
                    "eng": "Ukraine",
                    "chn": "...",
                    "visible": 1,
                    "retry": 1,
                    "rent": 1,
                    "multiService": 0,
                },
            },
        )
    )
    provider = make_provider(recorder)
    countries = await provider.get_countries()
    assert len(countries) == 1
    assert countries[0].code == "1"
    assert countries[0].name == "Ukraine"
    assert countries[0].raw["visible"] == 1
    assert countries[0].raw["retry"] == 1


# 53. get_countries on the OpenAPI list form -> Country(code="2", name="Kazakhstan")
async def test_get_countries_list_form():
    recorder = responses(
        httpx.Response(
            200,
            json=[
                {"id": 2, "rus": "Казахстан", "eng": "Kazakhstan", "chn": "...", "visible": 1,
                 "retry": 1},
            ],
        )
    )
    provider = make_provider(recorder)
    countries = await provider.get_countries()
    assert len(countries) == 1
    assert countries[0].code == "2"
    assert countries[0].name == "Kazakhstan"


# 54. BAD_ACTION (plain text or JSON) on either method -> NotImplementedError;
# plain BAD_KEY stays InvalidApiKey, not NotImplementedError
async def test_get_services_bad_action_plain_text_raises_not_implemented():
    recorder = responses(httpx.Response(200, text="BAD_ACTION"))
    provider = make_provider(recorder)
    with pytest.raises(NotImplementedError):
        await provider.get_services()


async def test_get_countries_bad_action_json_raises_not_implemented():
    recorder = responses(httpx.Response(400, json={"title": "BAD_ACTION", "details": "no"}))
    provider = make_provider(recorder)
    with pytest.raises(NotImplementedError):
        await provider.get_countries()


async def test_get_services_bad_key_raises_invalid_api_key_not_not_implemented():
    recorder = responses(httpx.Response(200, text="BAD_KEY"))
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_services()


async def test_get_countries_bad_key_raises_invalid_api_key_not_not_implemented():
    recorder = responses(httpx.Response(200, text="BAD_KEY"))
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_countries()


# 62. use_get_number_v2=False (default): action=getNumber, country_phone_code is
# None, cost is None - old behavior unbroken.
async def test_get_number_v2_disabled_by_default_uses_get_number():
    recorder = responses(httpx.Response(200, text="ACCESS_NUMBER:123:79001112233"))
    provider = make_provider(recorder)
    number = await provider.get_number(service="tg", country=7)
    assert recorder.requests[0].url.params["action"] == "getNumber"
    assert number.country_phone_code is None
    assert number.cost is None


# 63. use_get_number_v2=True on a live-shaped V2 example.
async def test_get_number_v2_parses_live_example():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "activationId": 635468024,
                "phoneNumber": "79584******",
                "countryCode": 6,
                "countryPhoneCode": 62,
                "activationCost": "12.5",
                "currency": 840,
                "activationTime": "2026-08-13 10:00:00",
                "activationEndTime": "2026-08-13 10:20:00",
                "canGetAnotherSms": True,
                "activationOperator": "beeline",
            },
        )
    )
    provider = make_provider(recorder, use_get_number_v2=True)
    number = await provider.get_number(service="tg", country=6)
    assert recorder.requests[0].url.params["action"] == "getNumberV2"
    assert number.id == "635468024"
    assert number.phone == "79584******"
    assert number.country == "6"
    assert number.country_phone_code == "62"
    assert number.cost == Decimal("12.5")
    assert number.raw["currency"] == 840
    assert number.raw["activationEndTime"] == "2026-08-13 10:20:00"


# 64. V2 branch + plain text NO_NUMBERS with HTTP 200 -> NoNumbersAvailable.
async def test_get_number_v2_no_numbers_plain_text_raises_no_numbers_available():
    recorder = responses(httpx.Response(200, text="NO_NUMBERS"))
    provider = make_provider(recorder, use_get_number_v2=True)
    with pytest.raises(NoNumbersAvailable):
        await provider.get_number(service="tg")


# 65. V2 branch + JSON {"title":"NO_BALANCE"} with HTTP 402 -> InsufficientBalance,
# and recorder.call_count == 1 - no fallback to getNumber (double-purchase regression).
async def test_get_number_v2_no_balance_does_not_fall_back_to_get_number():
    recorder = responses(
        httpx.Response(402, json={"title": "NO_BALANCE", "details": "Not enough funds"})
    )
    provider = make_provider(recorder, use_get_number_v2=True)
    with pytest.raises(InsufficientBalance):
        await provider.get_number(service="tg")
    assert recorder.call_count == 1


# 66. V2 branch without countryPhoneCode in the response -> country_phone_code is
# None, the rest of the fields are still filled.
async def test_get_number_v2_missing_country_phone_code_is_none():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "activationId": 1,
                "phoneNumber": "79001112233",
                "countryCode": 7,
            },
        )
    )
    provider = make_provider(recorder, use_get_number_v2=True)
    number = await provider.get_number(service="tg")
    assert number.id == "1"
    assert number.phone == "79001112233"
    assert number.country == "7"
    assert number.country_phone_code is None
    assert number.cost is None


# 67. V2 branch without activationId -> ProviderAPIError.
async def test_get_number_v2_missing_activation_id_raises_provider_api_error():
    recorder = responses(httpx.Response(200, json={"phoneNumber": "79001112233"}))
    provider = make_provider(recorder, use_get_number_v2=True)
    with pytest.raises(ProviderAPIError):
        await provider.get_number(service="tg")


# 68. use_get_number_v2=True via the constructor (not a subclass) enables V2 -
# the from_config path for a bare SmsActivateCompatibleProvider.
async def test_get_number_v2_enabled_via_constructor_kwarg():
    recorder = responses(
        httpx.Response(200, json={"activationId": 1, "phoneNumber": "79001112233"})
    )
    provider = SmsActivateCompatibleProvider(
        api_key="test-key",
        base_url="https://sms-activate.example/stubs/handler_api.php",
        client=make_client(recorder),
        use_get_number_v2=True,
    )
    assert provider.use_get_number_v2 is True
    number = await provider.get_number(service="tg")
    assert recorder.requests[0].url.params["action"] == "getNumberV2"
    assert number.id == "1"


# 69. A 4-line clone (name, base_url, use_get_number_v2 = True) goes through
# get_number and reports country_phone_code.
async def test_four_line_clone_with_v2_reports_country_phone_code():
    @SmsProviderManager.register()
    class _FourLineV2Clone(SmsActivateCompatibleProvider):
        name = "test-four-line-v2-clone"
        base_url = "https://four-line-v2-clone.example/stubs/handler_api.php"
        use_get_number_v2 = True

    recorder = responses(
        httpx.Response(
            200,
            json={"activationId": 42, "phoneNumber": "79001112233", "countryPhoneCode": 7},
        )
    )
    provider = _FourLineV2Clone(api_key="test-key", client=make_client(recorder))
    number = await provider.get_number(service="tg")
    assert number.id == "42"
    assert number.country_phone_code == "7"


# §12.6: cancelling a task awaiting wait_code() propagates asyncio.CancelledError,
# not SmsProviderError - invariant #6 in BaseSmsProvider's docstring.
async def test_cancelling_wait_code_task_propagates_cancelled_error(monkeypatch):
    # Restore real sleeping for this test only: the autouse no_sleep stub has no
    # suspension point of its own, so _poll would never yield back to the loop
    # for the cancellation to be delivered.
    monkeypatch.setattr("sms_providers.base.asyncio.sleep", asyncio.sleep)

    def endless_wait_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="STATUS_WAIT_CODE")

    provider = make_provider(endless_wait_handler)
    task = asyncio.ensure_future(provider.wait_code("1", timeout=100, poll_interval=1))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# §12.6: a provider constructed outside a running event loop works correctly when
# later used inside asyncio.run(...).
def test_provider_constructed_outside_event_loop_works_in_asyncio_run():
    recorder = responses(httpx.Response(200, text="ACCESS_BALANCE:1.000"))
    provider = make_provider(recorder)  # no running loop here

    async def main() -> Decimal:
        return await provider.get_balance()

    assert asyncio.run(main()) == Decimal("1.000")


# "maxPrice" via **extra (the API's native spelling) must not raise a duplicate
# kwarg TypeError; it feeds the maxPrice query parameter.
async def test_get_number_accepts_native_maxprice_spelling():
    recorder = responses(httpx.Response(200, text="ACCESS_NUMBER:123:79001112233"))
    provider = make_provider(recorder)
    number = await provider.get_number(service="tg", maxPrice="0.5")
    assert number.phone == "79001112233"
    assert recorder.requests[0].url.params["maxPrice"] == "0.5"
