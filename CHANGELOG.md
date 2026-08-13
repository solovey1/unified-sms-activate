# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- Optional auto-discovery of third-party providers via `entry_points`
  (`sms_providers.providers` group).
- `verificationType` 1/2 (call / voice verification) support in
  `SmsActivateCompatibleProvider`.
- Verify `OnlineSimProvider.cancel()` refund semantics on a live operation.

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
- `proxy` constructor parameter on `SmsActivateCompatibleProvider` and
  `OnlineSimProvider` for routing requests through an HTTP(S)/SOCKS proxy.
- Discovery: `get_services()`/`get_countries()` on `BaseSmsProvider` (opt-in,
  raise `NotImplementedError` by default), with `Service`/`Country` DTOs,
  implemented for `SmsActivateCompatibleProvider` and `OnlineSimProvider`.
