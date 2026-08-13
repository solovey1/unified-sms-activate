from __future__ import annotations

from decimal import Decimal

import httpx
from conftest import make_client, responses

from sms_providers.manager import SmsProviderManager
from sms_providers.providers import SmsActivateCompatibleProvider, SmsActivateProvider


def test_sms_activate_name_and_base_url():
    assert SmsActivateProvider.name == "sms-activate"
    assert SmsActivateProvider.base_url == "https://api.sms-activate.ae/stubs/handler_api.php"
    assert SmsProviderManager.provider_class("sms-activate") is SmsActivateProvider


async def test_sms_activate_mirror_host_via_base_url():
    recorder = responses(httpx.Response(200, text="ACCESS_BALANCE:1.5"))
    provider = SmsActivateProvider(
        api_key="test-key",
        base_url="https://api.sms-activate.io/stubs/handler_api.php",
        client=make_client(recorder),
    )
    assert await provider.get_balance() == Decimal("1.5")
    assert recorder.requests[0].url.host == "api.sms-activate.io"


# Any sms-activate-protocol host works through from_config without a subclass.
async def test_arbitrary_host_via_from_config():
    manager = SmsProviderManager.from_config(
        {
            "my-host": {
                "provider": "sms-activate-compatible",
                "api_key": "test-key",
                "base_url": "https://my-host.example/stubs/handler_api.php",
            },
        }
    )
    provider = manager["my-host"]
    assert isinstance(provider, SmsActivateCompatibleProvider)
    assert provider.base_url == "https://my-host.example/stubs/handler_api.php"
    await manager.aclose_all()
