from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import make_client, responses

from sms_providers.base import ActivationNotFound, InvalidApiKey, ProviderAPIError
from sms_providers.manager import SmsProviderManager
from sms_providers.providers import HeroSmsProvider


def make_provider(handler):
    return HeroSmsProvider(api_key="test-key", client=make_client(handler))


# 22. base_url and name match the spec; class is registered with SmsProviderManager
def test_hero_sms_name_and_base_url_match_spec():
    assert HeroSmsProvider.name == "hero-sms"
    assert HeroSmsProvider.base_url == "https://hero-sms.com/stubs/handler_api.php"
    assert SmsProviderManager.provider_class("hero-sms") is HeroSmsProvider


# 23. Recorded real responses
async def test_hero_sms_balance_from_recorded_response():
    recorder = responses(httpx.Response(200, text="ACCESS_BALANCE:2.225"))
    provider = make_provider(recorder)
    assert await provider.get_balance() == Decimal("2.225")


async def test_hero_sms_bad_key_from_recorded_response():
    recorder = responses(
        httpx.Response(401, json={"title": "BAD_KEY", "details": "Unauthorized"})
    )
    provider = make_provider(recorder)
    with pytest.raises(InvalidApiKey):
        await provider.get_balance()


async def test_hero_sms_not_found_from_recorded_response():
    recorder = responses(
        httpx.Response(404, json={"title": "NOT_FOUND", "details": "Activation Not Found"})
    )
    provider = make_provider(recorder)
    with pytest.raises(ActivationNotFound):
        await provider.get_status("1")


async def test_hero_sms_bad_action_from_recorded_response():
    recorder = responses(
        httpx.Response(422, json={"title": "BAD_ACTION", "details": "Method Not Found"})
    )
    provider = make_provider(recorder)
    with pytest.raises(ProviderAPIError):
        await provider.request_retry("1")


async def test_hero_sms_getnumbersstatus_empty_json_is_not_an_error():
    recorder = responses(httpx.Response(200, json={}))
    provider = make_provider(recorder)
    assert await provider._request("getNumbersStatus", country=0) == {}
