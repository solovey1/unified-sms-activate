"""Built-in providers.

Importing this package registers ``SmsActivateCompatibleProvider``,
``HeroSmsProvider`` and ``OnlineSimProvider`` with
:class:`~sms_providers.manager.SmsProviderManager`.
"""

from .hero_sms import HeroSmsProvider
from .online_sim import OnlineSimProvider
from .sms_activate_compatible import SmsActivateCompatibleProvider

__all__ = ["HeroSmsProvider", "OnlineSimProvider", "SmsActivateCompatibleProvider"]
