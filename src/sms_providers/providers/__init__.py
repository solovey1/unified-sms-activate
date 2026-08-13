"""Built-in providers.

Importing this package registers ``SmsActivateCompatibleProvider``,
``HeroSmsProvider``, ``OnlineSimProvider``, ``VakSmsProvider`` and
``SpanchSmsProvider`` with :class:`~sms_providers.manager.SmsProviderManager`.
"""

from .hero_sms import HeroSmsProvider
from .online_sim import OnlineSimProvider
from .sms_activate import SmsActivateProvider
from .sms_activate_compatible import SmsActivateCompatibleProvider
from .spanch_sms import SpanchSmsProvider
from .vak_sms import VakSmsProvider

__all__ = [
    "HeroSmsProvider",
    "OnlineSimProvider",
    "SmsActivateCompatibleProvider",
    "SmsActivateProvider",
    "SpanchSmsProvider",
    "VakSmsProvider",
]
