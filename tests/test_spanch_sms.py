from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import make_client, responses

from sms_providers.base import (
    ActivationCancelled,
    ActivationNotFound,
    ActivationStatus,
    InsufficientBalance,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    ProviderUnavailable,
    RateLimited,
    SmsCode,
)
from sms_providers.manager import SmsProviderManager
from sms_providers.providers import SpanchSmsProvider


def make_provider(handler, **kwargs):
    return SpanchSmsProvider(api_key="Sponge:test", client=make_client(handler), **kwargs)


# 73. The key is wrong -> InvalidApiKey (recorded live response)
async def test_invalid_key_raises_invalid_api_key():
    recorder = responses(
        httpx.Response(400, json={"status": "error", "message": "The key is wrong"})
    )
    provider = make_provider(recorder, default_gateway="bob")
    with pytest.raises(InvalidApiKey):
        await provider.get_balance()


# 74. Substring matching, not exact string comparison - live text vs. documented text
async def test_missing_params_matches_both_live_and_documented_text():
    recorder = responses(
        httpx.Response(
            400,
            json={"status": "error", "message": "Missing required parameters: action and api_key"},
        ),
        httpx.Response(
            400,
            json={"status": "error", "message": "Missing required parameters: action and key"},
        ),
    )
    provider = make_provider(recorder, default_gateway="bob")
    with pytest.raises(ProviderAPIError):
        await provider.get_balance()
    with pytest.raises(ProviderAPIError):
        await provider.get_balance()


# 75. get_number -> id/phone as numbers -> PhoneNumber with string id/phone
async def test_get_number_success():
    recorder = responses(
        httpx.Response(
            200, json={"status": "success", "id": 12345, "phone": 79005553535, "price": 1.5}
        )
    )
    provider = make_provider(recorder, default_gateway="crabbs")
    number = await provider.get_number("tg", "RU")
    assert isinstance(number, PhoneNumber)
    assert number.id == "12345"
    assert number.phone == "79005553535"
    assert number.cost == Decimal("1.5")
    assert number.country == "RU"
    assert number.country_phone_code is None


# 76. get_number without gateway and without default_gateway -> ValueError, no request made
async def test_get_number_without_gateway_raises_value_error():
    recorder = responses()
    provider = make_provider(recorder)
    with pytest.raises(ValueError):
        await provider.get_number("tg", "RU")
    assert recorder.call_count == 0


# 77. get_number(gateway=...) overrides default_gateway
async def test_get_number_gateway_override():
    recorder = responses(
        httpx.Response(
            200, json={"status": "success", "id": 1, "phone": 79000000000, "price": "0.1"}
        )
    )
    provider = make_provider(recorder, default_gateway="crabbs")
    await provider.get_number("tg", "RU", gateway="bob")
    assert recorder.requests[0].url.params["gateway"] == "bob"


# 78. NO NUMBERS -> NoNumbersAvailable; too many active -> RateLimited; insufficient funds -> InsufficientBalance with price in raw
async def test_get_number_error_mapping():
    recorder = responses(
        httpx.Response(400, json={"status": "error", "message": "NO NUMBERS"}),
        httpx.Response(
            400,
            json={
                "status": "error",
                "message": "Too many active activations. Cancel the previous ones",
            },
        ),
        httpx.Response(
            400, json={"status": "error", "message": "Insufficient funds", "price": 1.5}
        ),
    )
    provider = make_provider(recorder, default_gateway="crabbs")
    with pytest.raises(NoNumbersAvailable):
        await provider.get_number("tg", "RU")
    with pytest.raises(RateLimited):
        await provider.get_number("tg", "RU")
    with pytest.raises(InsufficientBalance) as excinfo:
        await provider.get_number("tg", "RU")
    assert excinfo.value.raw["price"] == 1.5


# 79. get_code with a received code -> SmsCode
async def test_get_code_received():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "status": "success",
                "message": "received",
                "code": "12345",
                "full": "Ваш код: 12345",
            },
        )
    )
    provider = make_provider(recorder)
    code = await provider.get_code("1")
    assert code == SmsCode(code="12345", text="Ваш код: 12345", raw=code.raw)


# 80. get_code tolerant parsing: "Wait code" (live), "waiting", and empty code -> None
async def test_get_code_no_code_yet_returns_none():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": "Wait code"}),
        httpx.Response(200, json={"status": "success", "message": "waiting"}),
        httpx.Response(200, json={"status": "success", "message": "received", "code": ""}),
    )
    provider = make_provider(recorder)
    assert await provider.get_code("1") is None
    assert await provider.get_code("1") is None
    assert await provider.get_code("1") is None


# 81. wait_code: two "no code yet" then received -> SmsCode, exactly 3 requests
async def test_wait_code_polls_until_received():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": "Wait code"}),
        httpx.Response(200, json={"status": "success", "message": "Wait code"}),
        httpx.Response(
            200, json={"status": "success", "message": "received", "code": "999", "full": None}
        ),
    )
    provider = make_provider(recorder, poll_interval=0.01)
    code = await provider.wait_code("1", timeout=5)
    assert code.code == "999"
    assert recorder.call_count == 3


# 82. wait_code on "no longer active" -> ActivationCancelled(status=EXPIRED)
async def test_wait_code_no_longer_active_raises_activation_cancelled():
    recorder = responses(
        httpx.Response(
            400, json={"status": "error", "message": "This number is no longer active"}
        )
    )
    provider = make_provider(recorder)
    with pytest.raises(ActivationCancelled) as excinfo:
        await provider.wait_code("1", timeout=5)
    assert excinfo.value.status == ActivationStatus.EXPIRED


# 83. cancel sends action=getCancel&id=; live early-cancel error (broken escaping) -> OperationNotAllowed, seconds in raw
async def test_cancel_sends_correct_action_and_id():
    recorder = responses(httpx.Response(200, json={"status": "success", "message": "Order canceled"}))
    provider = make_provider(recorder)
    await provider.cancel("777")
    params = recorder.requests[0].url.params
    assert params["action"] == "getCancel"
    assert params["id"] == "777"


async def test_cancel_too_early_raises_operation_not_allowed():
    recorder = responses(
        httpx.Response(
            400,
            json={
                "status": "error",
                "message": 'You can"t cancel an order so quickly',
                "seconds": 120,
            },
        )
    )
    provider = make_provider(recorder)
    with pytest.raises(OperationNotAllowed) as excinfo:
        await provider.cancel("1")
    assert excinfo.value.raw["seconds"] == 120


# 84. finish makes no request and never raises
async def test_finish_is_a_noop():
    recorder = responses()
    provider = make_provider(recorder)
    assert recorder.call_count == 0
    await provider.finish("1")
    assert recorder.call_count == 0


# 85. request_retry sends action=getNewCode&id= and swallows the success message
async def test_request_retry():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": "Waiting for new code"})
    )
    provider = make_provider(recorder)
    await provider.request_retry("1")
    params = recorder.requests[0].url.params
    assert params["action"] == "getNewCode"
    assert params["id"] == "1"


# 86. get_countries / get_services; get_services(country=...) does not put country in the query
async def test_get_countries_and_get_services():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": ["RU", "UA"]}),
        httpx.Response(200, json={"status": "success", "message": '["tg", "wa"]'}),
    )
    provider = make_provider(recorder)
    countries = await provider.get_countries()
    assert [c.code for c in countries] == ["RU", "UA"]
    assert all(c.name is None for c in countries)
    services = await provider.get_services(country="RU")
    assert [s.code for s in services] == ["tg", "wa"]
    assert all(s.name is None for s in services)
    assert "country" not in recorder.requests[1].url.params


# 87. get_gateways on a comma-separated string, not a list
async def test_get_gateways_parses_comma_separated_string():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": "crabbs, bob, ocean"})
    )
    provider = make_provider(recorder)
    assert await provider.get_gateways() == ["crabbs", "bob", "ocean"]


# 88. get_prices(service=, country=) -> raw list; both params required
async def test_get_prices_raw_list():
    recorder = responses(
        httpx.Response(
            200, json={"status": "success", "prices": [{"price": 3.36, "route": "G"}]}
        )
    )
    provider = make_provider(recorder)
    prices = await provider.get_prices(service="tg", country="RU")
    assert prices == [{"price": 3.36, "route": "G"}]
    params = recorder.requests[0].url.params
    assert params["service"] == "tg"
    assert params["country"] == "RU"


# 89. Class registered as spanch-sms, constructible via from_config with default_gateway
def test_registered_and_constructible_via_from_config():
    manager = SmsProviderManager.from_config(
        {
            "spanch": {
                "provider": "spanch-sms",
                "api_key": "Sponge:test",
                "default_gateway": "crabbs",
            }
        }
    )
    provider = manager["spanch"]
    assert isinstance(provider, SpanchSmsProvider)
    assert provider.name == "spanch-sms"
    assert provider.default_gateway == "crabbs"


# 90. get_balance on live string message and documented numeric message
async def test_get_balance_parses_string_and_number():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": "2.03"}),
        httpx.Response(200, json={"status": "success", "message": 100.5}),
    )
    provider = make_provider(recorder)
    assert await provider.get_balance() == Decimal("2.03")
    assert await provider.get_balance() == Decimal("100.5")


# 91. get_services on live double-encoded JSON string - codes with embedded commas must not be split
async def test_get_services_double_encoded_message_not_split_on_comma():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "status": "success",
                "message": '["any other", "google,youtube,gmail", "telegram"]',
            },
        )
    )
    provider = make_provider(recorder)
    services = await provider.get_services()
    assert [s.code for s in services] == ["any other", "google,youtube,gmail", "telegram"]


# 92. get_countries on a plain array - same parser handles both shapes
async def test_get_countries_plain_array():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": ["RU", "UA", "KZ"]})
    )
    provider = make_provider(recorder)
    countries = await provider.get_countries()
    assert [c.code for c in countries] == ["RU", "UA", "KZ"]


# 93. get_gateways on the live 14-gateway response, including undocumented ones - no local validation
async def test_get_gateways_live_14_gateways():
    live = (
        "crabbs, bob, gary, larry, patrick, plankton, sandy, karen, ocean, "
        "squidward, jellyfish, pearl, whale, dolphin"
    )
    recorder = responses(httpx.Response(200, json={"status": "success", "message": live}))
    provider = make_provider(recorder)
    gateways = await provider.get_gateways()
    assert len(gateways) == 14
    assert "jellyfish" in gateways
    assert "whale" in gateways


# 94. "Unknown error from gateway" (undocumented) -> ProviderUnavailable, no auto-retry
async def test_unknown_error_from_gateway_raises_provider_unavailable():
    recorder = responses(
        httpx.Response(400, json={"status": "error", "message": "Unknown error from gateway"})
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderUnavailable):
        await provider.get_prices(service="telegram", country="RU", gateway="crabbs")
    assert recorder.call_count == 1


# 95. get_code on a nonexistent id -> ActivationNotFound (recorded live response)
async def test_get_code_order_not_found():
    recorder = responses(
        httpx.Response(400, json={"status": "error", "message": "Order not found"})
    )
    provider = make_provider(recorder)
    with pytest.raises(ActivationNotFound):
        await provider.get_code("999999999")


# 96. get_number on the live response: price as a string, 14-digit id
async def test_get_number_live_string_price_and_large_id():
    recorder = responses(
        httpx.Response(
            200,
            json={
                "status": "success",
                "id": 17866245486334,
                "phone": 639553325264,
                "price": "0.02",
            },
        )
    )
    provider = make_provider(recorder, default_gateway="bob")
    number = await provider.get_number("tiktok", "PH")
    assert number.cost == Decimal("0.02")
    assert number.id == "17866245486334"
    assert number.phone == "639553325264"


# 97. Successful cancel, then a repeated cancel on the same id - both succeed, idempotent on the API side
async def test_cancel_is_idempotent():
    recorder = responses(
        httpx.Response(200, json={"status": "success", "message": "Order canceled"}),
        httpx.Response(200, json={"status": "success", "message": "Order canceled"}),
    )
    provider = make_provider(recorder)
    await provider.cancel("1")
    await provider.cancel("1")
    assert recorder.call_count == 2


# 98. After cancel, get_code raises ActivationCancelled(EXPIRED); get_status returns EXPIRED without raising
async def test_get_code_and_get_status_after_cancel():
    recorder = responses(
        httpx.Response(
            400, json={"status": "error", "message": "This number is no longer active"}
        ),
        httpx.Response(
            400, json={"status": "error", "message": "This number is no longer active"}
        ),
    )
    provider = make_provider(recorder)
    with pytest.raises(ActivationCancelled) as excinfo:
        await provider.get_code("1")
    assert excinfo.value.status == ActivationStatus.EXPIRED
    assert await provider.get_status("1") == ActivationStatus.EXPIRED
