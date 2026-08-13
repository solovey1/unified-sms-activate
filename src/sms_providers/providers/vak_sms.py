"""VAK SMS provider."""

from __future__ import annotations

from sms_providers.manager import SmsProviderManager

from .sms_activate_compatible import SmsActivateCompatibleProvider


@SmsProviderManager.register()
class VakSmsProvider(SmsActivateCompatibleProvider):
    """VAK SMS (https://vak-sms.com) - sms-activate-compatible API.

    A vanilla plain-text clone: every response, including errors, comes as
    plain text with HTTP 200. Quirks verified with live requests (2026-08-13):
    * An invalid api_key is answered with ``BAD_ACTION`` rather than
      ``BAD_KEY``, so a wrong key raises :class:`ProviderAPIError` instead of
      :class:`InvalidApiKey` - the API itself does not distinguish the two.
    * ``getStatus`` with an unknown activation id also returns ``BAD_ACTION``;
      ``setStatus`` returns the expected ``NO_ACTIVATION``.
    * ``getCountries`` returns a JSON list (not a dict keyed by id) and does
      not require a valid api_key; ``getServicesList`` matches HeroSMS's shape.
    """

    name = "vak-sms"
    base_url = "https://vak-sms.com/stubs/handler_api.php"
