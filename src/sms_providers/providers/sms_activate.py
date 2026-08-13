"""SMS-Activate provider (the original sms-activate protocol service)."""

from __future__ import annotations

from sms_providers.manager import SmsProviderManager

from .sms_activate_compatible import SmsActivateCompatibleProvider


@SmsProviderManager.register()
class SmsActivateProvider(SmsActivateCompatibleProvider):
    """SMS-Activate (https://sms-activate.ae) - the original service behind the protocol.

    The default host is the one from the official API docs. SMS-Activate is
    known to be unreachable from some regions/networks - if the default host
    times out for you, pass one of the mirrors explicitly::

        SmsActivateProvider(api_key="...", base_url="https://api.sms-activate.io/stubs/handler_api.php")

    Any other sms-activate-protocol host works the same way - passing
    ``base_url`` (plus optionally ``name``) to this class or to
    :class:`SmsActivateCompatibleProvider` integrates it without subclassing.
    """

    name = "sms-activate"
    base_url = "https://api.sms-activate.ae/stubs/handler_api.php"
