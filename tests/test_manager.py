from __future__ import annotations

import pytest

from sms_providers.base import BaseSmsProvider, ProviderNotRegistered
from sms_providers.manager import SmsProviderManager
from sms_providers.providers import HeroSmsProvider, OnlineSimProvider


class _RecordingProvider(BaseSmsProvider):
    name = "test-manager-recording"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    async def get_balance(self):
        raise NotImplementedError

    async def get_number(self, service, country=None, **options):
        raise NotImplementedError

    async def get_status(self, activation_id):
        raise NotImplementedError

    async def wait_code(self, activation_id, *, timeout=None, poll_interval=None):
        raise NotImplementedError

    async def cancel(self, activation_id):
        raise NotImplementedError

    async def finish(self, activation_id):
        raise NotImplementedError

    async def aclose(self):
        self.closed = True


# 37. register_provider + get; duplicate name -> ValueError; replace=True allows it
def test_register_provider_and_get():
    manager = SmsProviderManager()
    provider = _RecordingProvider()
    manager.register_provider("svc", provider)
    assert manager.get("svc") is provider
    assert manager["svc"] is provider
    assert "svc" in manager
    assert manager.names() == ["svc"]
    assert len(manager) == 1


def test_register_provider_duplicate_name_raises_value_error():
    manager = SmsProviderManager()
    manager.register_provider("svc", _RecordingProvider())
    with pytest.raises(ValueError):
        manager.register_provider("svc", _RecordingProvider())


def test_register_provider_replace_true_allows_overwrite():
    manager = SmsProviderManager()
    first = _RecordingProvider()
    second = _RecordingProvider()
    manager.register_provider("svc", first)
    manager.register_provider("svc", second, replace=True)
    assert manager.get("svc") is second


def test_get_unregistered_raises_provider_not_registered():
    manager = SmsProviderManager()
    with pytest.raises(ProviderNotRegistered):
        manager.get("missing")


# 38. @SmsProviderManager.register("x") registers a class; without an argument it uses cls.name
def test_register_decorator_with_explicit_name():
    @SmsProviderManager.register("test-manager-explicit")
    class _Explicit(_RecordingProvider):
        name = "unused"

    assert SmsProviderManager.provider_class("test-manager-explicit") is _Explicit


def test_register_decorator_without_name_uses_class_attribute():
    @SmsProviderManager.register()
    class _FromClassName(_RecordingProvider):
        name = "test-manager-from-class-name"

    assert SmsProviderManager.provider_class("test-manager-from-class-name") is _FromClassName


# 39. from_config with two providers creates the right types and passes kwargs
def test_from_config_creates_expected_types_and_passes_kwargs():
    @SmsProviderManager.register("test-manager-conf-a")
    class _ConfA(_RecordingProvider):
        name = "test-manager-conf-a"

    @SmsProviderManager.register("test-manager-conf-b")
    class _ConfB(_RecordingProvider):
        name = "test-manager-conf-b"

    manager = SmsProviderManager.from_config(
        {
            "svc-a": {"provider": "test-manager-conf-a", "api_key": "test-key"},
            "svc-b": {"provider": "test-manager-conf-b", "api_key": "test-key", "timeout": 20.0},
        }
    )
    assert isinstance(manager["svc-a"], _ConfA)
    assert isinstance(manager["svc-b"], _ConfB)
    assert manager["svc-a"].kwargs == {"api_key": "test-key"}
    assert manager["svc-b"].kwargs == {"api_key": "test-key", "timeout": 20.0}


# 40. from_config with a "provider" key creates two instances of the same class under
#     different names
def test_from_config_provider_key_creates_two_instances_of_same_class():
    @SmsProviderManager.register("test-manager-conf-shared")
    class _ConfShared(_RecordingProvider):
        name = "test-manager-conf-shared"

    manager = SmsProviderManager.from_config(
        {
            "inst-1": {"provider": "test-manager-conf-shared", "api_key": "key-1"},
            "inst-2": {"provider": "test-manager-conf-shared", "api_key": "key-2"},
        }
    )
    assert type(manager["inst-1"]) is _ConfShared
    assert type(manager["inst-2"]) is _ConfShared
    assert manager["inst-1"] is not manager["inst-2"]


# 41. Unknown name -> ProviderNotRegistered with a list of available names
def test_from_config_unknown_provider_raises_with_available_names():
    with pytest.raises(ProviderNotRegistered) as exc_info:
        SmsProviderManager.from_config({"svc": {"provider": "test-manager-does-not-exist"}})
    assert "sms-activate-compatible" in str(exc_info.value)


# 42. A user's own provider goes through from_config without touching the package
def test_from_config_works_with_user_defined_provider_class():
    class _UserProvider(_RecordingProvider):
        name = "test-manager-user-defined"

    SmsProviderManager.register("test-manager-user-defined")(_UserProvider)
    manager = SmsProviderManager.from_config(
        {"user-svc": {"provider": "test-manager-user-defined", "api_key": "test-key"}}
    )
    assert isinstance(manager["user-svc"], _UserProvider)


def test_from_config_wraps_constructor_typeerror_as_valueerror():
    @SmsProviderManager.register("test-manager-badinit")
    class _BadInit(_RecordingProvider):
        name = "test-manager-badinit"

        def __init__(self, required_arg):
            super().__init__()

    with pytest.raises(ValueError, match="bad-section"):
        SmsProviderManager.from_config({"bad-section": {"provider": "test-manager-badinit"}})


# from_config with the real built-in provider classes, including the "provider" alias
async def test_from_config_with_real_provider_classes():
    manager = SmsProviderManager.from_config(
        {
            "hero-sms": {"api_key": "test-key"},
            "online-sim": {"api_key": "test-key"},
            "hero-backup": {"provider": "hero-sms", "api_key": "test-key"},
        }
    )
    assert isinstance(manager["hero-sms"], HeroSmsProvider)
    assert isinstance(manager["online-sim"], OnlineSimProvider)
    assert isinstance(manager["hero-backup"], HeroSmsProvider)
    assert manager["hero-backup"] is not manager["hero-sms"]
    await manager.aclose_all()


# 43. aclose_all closes every registered provider
async def test_close_all_closes_every_provider():
    manager = SmsProviderManager()
    first = _RecordingProvider()
    second = _RecordingProvider()
    manager.register_provider("p1", first)
    manager.register_provider("p2", second)
    await manager.aclose_all()
    assert first.closed
    assert second.closed
