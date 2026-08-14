# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `get_messages(activation_id) -> list[SmsCode]` on `BaseSmsProvider` (opt-in,
  `NotImplementedError` by default): every SMS of an activation, not just the
  active one. `SmsActivateCompatibleProvider` implements it via
  `action=getAllSms` with full text and `received_at`; `OnlineSimProvider` via
  `getState&msg_list=1` (codes only - the API reports no timestamp).
- `SmsCode.received_at` is now filled from `getStatusV2`'s `sms.dateTime`.

### Changed

- `OnlineSimProvider.get_messages()` returns `list[SmsCode]`; the previous
  raw-list behaviour moved to `get_raw_messages()` unchanged.
- OnlineSim `msg` entries are parsed as objects (`{"service", "msg"}`), the
  form the live API actually returns with `msg_list=1`; the code list is read
  first-entry-first, matching the entry the API itself returns as the active
  message.

### Planned

- Optional auto-discovery of third-party providers via `entry_points`
  (`sms_providers.providers` group).
- `verificationType` 1/2 (call / voice verification) support in
  `SmsActivateCompatibleProvider`.
- Verify `OnlineSimProvider.cancel()` refund semantics on a live operation.
- Optional generated sync facade (unasync) if demand appears.

## [0.1.0] - 2026-08-12

### Added

- Base contract: `BaseSmsProvider`, `PhoneNumber`, `SmsCode`, `ActivationStatus`,
  and the package exception hierarchy (`base.py`).
- `SmsProviderManager` with instance and class registries, `register_provider`,
  `register` decorator, and `from_config`.
- `SmsActivateCompatibleProvider` — generic client for sms-activate-protocol
  (`handler_api.php`) services.
- `HeroSmsProvider` — thin `SmsActivateCompatibleProvider` subclass for
  [hero-sms.com](https://hero-sms.com).
- `OnlineSimProvider` — dedicated client for the OnlineSim API.
- `VakSmsProvider` — thin `SmsActivateCompatibleProvider` subclass for
  [vak-sms.com](https://vak-sms.com).
- `SmsActivateProvider` — thin subclass for the original
  [SMS-Activate](https://sms-activate.ae) service; any other
  sms-activate-protocol host can be used without subclassing by passing
  `base_url`/`name` to `SmsActivateCompatibleProvider` (also via `from_config`).
- `proxy` constructor parameter on `SmsActivateCompatibleProvider` and
  `OnlineSimProvider` for routing requests through an HTTP(S)/SOCKS proxy.
- Discovery: `get_services()`/`get_countries()` on `BaseSmsProvider` (opt-in,
  raise `NotImplementedError` by default), with `Service`/`Country` DTOs,
  implemented for `SmsActivateCompatibleProvider` and `OnlineSimProvider`.
- Async-first API built on `httpx.AsyncClient`.
- `PhoneNumber.country_phone_code`; opt-in `getNumberV2` for
  sms-activate-compatible providers (`use_get_number_v2`).
- `get_prices()` on `BaseSmsProvider` (opt-in, `NotImplementedError` by
  default) with the `CountryPrice` DTO, implemented for
  `SmsActivateCompatibleProvider` (`getPrices`) and `OnlineSimProvider`
  (`getTariffs`, requires `service=`).
- `search=` parameter on `get_services()` (client-side substring filter for
  `SmsActivateCompatibleProvider`, server-side `filter=` for `OnlineSimProvider`);
  `SmsActivateCompatibleProvider.get_services()` now also fills in
  `Service.count`/`Service.price` when the API returns them.
- `find_service(name, country=None)` on `BaseSmsProvider` - looks up a
  service by code or name, tolerating a trailing domain suffix
  (e.g. `"bybit"` matches a `"bybit.com"` listing).
- `SpanchSmsProvider` (spanch-projects.com) - own protocol; requires
  `gateway=`/`default_gateway=` for `get_number()`, adds a provider-specific
  `get_gateways()`/`get_prices()`.
