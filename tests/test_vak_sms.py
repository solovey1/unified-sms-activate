from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import make_client, responses

from sms_providers.base import ActivationNotFound, NoNumbersAvailable, ProviderAPIError
from sms_providers.manager import SmsProviderManager
from sms_providers.providers import VakSmsProvider


def make_provider(handler):
    return VakSmsProvider(api_key="test-key", client=make_client(handler))


def test_vak_sms_name_and_base_url_match_spec():
    assert VakSmsProvider.name == "vak-sms"
    assert VakSmsProvider.base_url == "https://vak-sms.com/stubs/handler_api.php"
    assert SmsProviderManager.provider_class("vak-sms") is VakSmsProvider


# Recorded real responses (2026-08-13). All plain text, all HTTP 200.
async def test_vak_sms_balance_from_recorded_response():
    recorder = responses(httpx.Response(200, text="ACCESS_BALANCE:0.9125"))
    provider = make_provider(recorder)
    assert await provider.get_balance() == Decimal("0.9125")


async def test_vak_sms_no_numbers_from_recorded_response():
    recorder = responses(httpx.Response(200, text="NO_NUMBERS"))
    provider = make_provider(recorder)
    with pytest.raises(NoNumbersAvailable):
        await provider.get_number(service="tg", country=187)


async def test_vak_sms_unknown_activation_on_set_status():
    recorder = responses(httpx.Response(200, text="NO_ACTIVATION"))
    provider = make_provider(recorder)
    with pytest.raises(ActivationNotFound):
        await provider.cancel("1")


async def test_vak_sms_bad_key_surfaces_as_bad_action():
    # vak-sms answers BAD_ACTION to an invalid api_key; the API does not
    # distinguish it from an unknown action, so ProviderAPIError is expected.
    recorder = responses(httpx.Response(200, text="BAD_ACTION"))
    provider = make_provider(recorder)
    with pytest.raises(ProviderAPIError):
        await provider.get_balance()


async def test_vak_sms_getcountries_list_form():
    recorder = responses(
        httpx.Response(
            200,
            json=[
                {"id": "95", "rus": "ОАЭ", "eng": "United Arab Emirates", "visible": 1},
                {"id": "74", "rus": "Афганистан", "eng": "Afghanistan", "visible": 1},
            ],
        )
    )
    provider = make_provider(recorder)
    countries = await provider.get_countries()
    assert [c.code for c in countries] == ["95", "74"]
    assert countries[0].name == "United Arab Emirates"
