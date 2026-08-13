from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sms_providers.base import (
    ActivationStatus,
    ActivationTimeout,
    BaseSmsProvider,
    Country,
    PhoneNumber,
    Service,
    SmsCode,
)


class _Dummy(BaseSmsProvider):
    name = "dummy"

    def get_balance(self):
        raise NotImplementedError

    def get_number(self, service, country=None, **options):
        raise NotImplementedError

    def get_status(self, activation_id):
        raise NotImplementedError

    def wait_code(self, activation_id, *, timeout=None, poll_interval=None):
        raise NotImplementedError

    def cancel(self, activation_id):
        raise NotImplementedError

    def finish(self, activation_id):
        raise NotImplementedError


def test_poll_returns_first_non_none_result():
    provider = _Dummy()
    calls = []

    def fetch():
        calls.append(1)
        return "done"

    result = provider._poll(fetch, timeout=10, interval=1)
    assert result == "done"
    assert len(calls) == 1


def test_poll_sleeps_n_minus_1_times(monkeypatch):
    provider = _Dummy()
    sleeps = []
    monkeypatch.setattr("sms_providers.base.time.sleep", lambda s: sleeps.append(s))

    results = iter([None, None, "ok"])

    def fetch():
        return next(results)

    result = provider._poll(fetch, timeout=100, interval=2)
    assert result == "ok"
    assert len(sleeps) == 2  # n=3 calls, n-1 sleeps


def test_poll_raises_activation_timeout_on_deadline(monkeypatch):
    provider = _Dummy()
    times = iter([0.0, 0.05, 0.2])
    monkeypatch.setattr(
        "sms_providers.base.time.monotonic", lambda: next(times, 999.0)
    )
    monkeypatch.setattr("sms_providers.base.time.sleep", lambda s: None)

    with pytest.raises(ActivationTimeout):
        provider._poll(lambda: None, timeout=0.1, interval=0.01)


def test_activation_status_string_equality():
    assert ActivationStatus.WAITING == "waiting"
    assert ActivationStatus.CODE_RECEIVED == "code_received"


def test_phone_number_is_frozen():
    number = PhoneNumber(id="1", phone="123", provider="dummy")
    with pytest.raises(FrozenInstanceError):
        number.phone = "456"  # type: ignore[misc]


def test_sms_code_is_frozen():
    code = SmsCode(code="1234")
    with pytest.raises(FrozenInstanceError):
        code.code = "9999"  # type: ignore[misc]


def test_phone_number_str_and_sms_code_str():
    assert str(PhoneNumber(id="1", phone="79001112233", provider="dummy")) == "79001112233"
    assert str(SmsCode(code="4242")) == "4242"


def test_dto_raw_defaults_to_empty_dict():
    assert PhoneNumber(id="1", phone="1", provider="dummy").raw == {}
    assert SmsCode(code="1").raw == {}


# 47. Service/Country are immutable, str(x) == x.code, raw defaults to {}.
def test_service_and_country_are_frozen():
    service = Service(code="tg")
    with pytest.raises(FrozenInstanceError):
        service.code = "wa"  # type: ignore[misc]

    country = Country(code="7")
    with pytest.raises(FrozenInstanceError):
        country.code = "1"  # type: ignore[misc]


def test_service_and_country_str_is_code():
    assert str(Service(code="tg")) == "tg"
    assert str(Country(code="7")) == "7"


def test_service_and_country_raw_defaults_to_empty_dict():
    assert Service(code="tg").raw == {}
    assert Country(code="7").raw == {}


# 48. A provider that doesn't override get_services/get_countries raises
# NotImplementedError on both.
def test_get_services_and_get_countries_raise_not_implemented_by_default():
    provider = _Dummy()
    with pytest.raises(NotImplementedError):
        provider.get_services()
    with pytest.raises(NotImplementedError):
        provider.get_countries()
