from __future__ import annotations

import sms_providers
from sms_providers.providers import HeroSmsProvider, OnlineSimProvider


# 46. All names in __all__ import from sms_providers; HeroSmsProvider/OnlineSimProvider
#     come from sms_providers.providers, not the top-level package.
def test_all_public_names_importable_from_top_level():
    for name in sms_providers.__all__:
        assert hasattr(sms_providers, name), f"{name} is in __all__ but not importable"


def test_all_matches_spec():
    expected = {
        "ActivationStatus", "PhoneNumber", "SmsCode",
        "BaseSmsProvider", "SmsActivateCompatibleProvider", "SmsProviderManager",
        "SmsProviderError", "InvalidApiKey", "AccountBlocked", "InsufficientBalance",
        "NoNumbersAvailable", "RateLimited", "ActivationNotFound", "ActivationCancelled",
        "ActivationTimeout", "OperationNotAllowed", "ProviderUnavailable", "ProviderAPIError",
        "ProviderNotRegistered", "__version__",
    }
    assert set(sms_providers.__all__) == expected


def test_hero_sms_and_online_sim_are_not_in_top_level_namespace():
    assert not hasattr(sms_providers, "HeroSmsProvider")
    assert not hasattr(sms_providers, "OnlineSimProvider")


def test_hero_sms_and_online_sim_importable_from_providers_subpackage():
    assert HeroSmsProvider.name == "hero-sms"
    assert OnlineSimProvider.name == "online-sim"
