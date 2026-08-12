"""HeroSMS provider."""

from __future__ import annotations

from sms_providers.manager import SmsProviderManager

from .sms_activate_compatible import SmsActivateCompatibleProvider


@SmsProviderManager.register()
class HeroSmsProvider(SmsActivateCompatibleProvider):
    """HeroSMS (https://hero-sms.com) - sms-activate-compatible API.

    Differences from vanilla sms-activate (verified with live requests and
    the official OpenAPI spec):
    * Errors arrive as a JSON object {"title": "BAD_KEY", "details": "Unauthorized"}
      with HTTP status 400/401/402/403/404/409/422/500, not as plain-text codes.
      Some responses remain plain text though: NO_NUMBERS is returned with HTTP 200.
    * setStatus only accepts 3 (resend SMS), 6 (finish), 8 (cancel);
      status 1 (ACCESS_READY) does not exist - the number is ready to
      receive SMS immediately after getNumber.
    * There are additionally cancelActivation/finishActivation (HTTP 204) and
      getStatusV2 with verificationType; this package does not use them, for
      portability across sms-activate-compatible clones.
    * Authorization: api_key in the query (used here) or the
      Authorization: ApiKey <token> header.
    """

    name = "hero-sms"
    base_url = "https://hero-sms.com/stubs/handler_api.php"
