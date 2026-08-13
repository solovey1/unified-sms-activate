# sms-providers

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

A unified Python interface for SMS activation services. One contract
(`BaseSmsProvider`) covers buying a number, waiting for the code, and
closing the activation, so your code doesn't change when you switch or add
a provider. Ships with clients for [HeroSMS](https://hero-sms.com) and
[OnlineSim](https://onlinesim.io), plus a base class for any service that
speaks the sms-activate `handler_api.php` protocol.

## Installation

```bash
pip install sms-providers
```

## Quickstart

```python
from sms_providers.providers import HeroSmsProvider

with HeroSmsProvider(api_key="...") as provider:
    number = provider.get_number(service="tg", country=0)
    print(number.phone)
    code = provider.wait_code(number.id, timeout=180)
    print(code.code)
    provider.finish(number.id)
```

## Supported services

- **HeroSMS** (`sms_providers.providers.HeroSmsProvider`) - sms-activate-compatible.
- **OnlineSim** (`sms_providers.providers.OnlineSimProvider`) - own protocol.

Service and country codes (`service="tg"`, `country=0`, ...) are passed
through untouched to each provider's native API. The package does **not**
normalize service/country codes between providers (e.g. HeroSMS's `tg` vs.
another provider's own code for Telegram) - such a mapping table would go
stale faster than releases ship. Consult each provider's own documentation
for its codes, or discover them at runtime:

```python
for service in provider.get_services():
    print(service.code, service.name)
```

`Service.code`/`Country.code` are exactly the values `get_number()` accepts
for `service=`/`country=` on that same provider.

## Adding your own service

You don't need to fork this repository to add a service — implement the
contract in your own code and register it.

### sms-activate-compatible protocol

If the service speaks the classic `handler_api.php` protocol (`action=...`
GET requests, `KEY:value:value` / JSON responses), subclass
`SmsActivateCompatibleProvider` and set `name` and `base_url`:

```python
from sms_providers import SmsActivateCompatibleProvider, SmsProviderManager

@SmsProviderManager.register()
class MyClone(SmsActivateCompatibleProvider):
    name = "my-clone"
    base_url = "https://my-clone.example/stubs/handler_api.php"
```

That's the whole class. If your clone's error codes or status codes differ
from the vanilla protocol, override `extra_error_map` / `extra_status_map`
(they're merged on top of the built-in tables).

### Fully custom protocol

Subclass `BaseSmsProvider` directly and implement its six abstract methods,
raising the package's exceptions instead of leaking transport/JSON errors:

```python
from decimal import Decimal
from sms_providers import ActivationStatus, BaseSmsProvider, PhoneNumber, SmsCode, SmsProviderManager

class MyProvider(BaseSmsProvider):
    name = "my-service"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def get_balance(self) -> Decimal: ...
    def get_number(self, service: str, country=None, **options) -> PhoneNumber: ...
    def get_status(self, activation_id: str) -> ActivationStatus: ...
    def wait_code(self, activation_id: str, *, timeout=None, poll_interval=None) -> SmsCode: ...
    def cancel(self, activation_id: str) -> None: ...
    def finish(self, activation_id: str) -> None: ...
    # get_services()/get_countries() are optional - the base class already
    # raises NotImplementedError for both, override only if your API supports them.

# register without touching sms_providers itself:
SmsProviderManager.register("my-service")(MyProvider)
# or use it directly:
manager = SmsProviderManager()
manager.register_provider("my-service", MyProvider(api_key="..."))
```

## `SmsProviderManager` and `from_config`

`SmsProviderManager` keeps two registries: a class registry (provider
*types*, used by `from_config`) and an instance registry (configured,
ready-to-use providers).

```python
from sms_providers import SmsProviderManager

manager = SmsProviderManager.from_config({
    "hero-sms":   {"api_key": "..."},
    "online-sim": {"api_key": "...", "timeout": 20.0},
    # a second instance of the same provider class under its own name:
    "hero-backup": {"provider": "hero-sms", "api_key": "..."},
})

provider = manager["hero-sms"]
number = provider.get_number(service="tg")
...
manager.close_all()
```

Registering a class (built-in or your own) is done with the
`SmsProviderManager.register()` decorator or by calling
`SmsProviderManager.register(name)(YourClass)` directly - see "Adding your
own service" above. `from_config` doesn't know anything about specific
provider classes; any registered class works without touching `manager.py`.

## Proxy support

Pass `proxy=` to route every request through an HTTP(S) or SOCKS proxy
(`httpx`'s built-in proxy support, nothing extra to install for HTTP/HTTPS
proxies; SOCKS needs the `httpx[socks]` extra):

```python
provider = HeroSmsProvider(
    api_key="...",
    proxy="http://user:pass@proxy.example:8080",  # or "socks5://proxy.example:1080"
)
```

`proxy` and `client` are mutually exclusive — if you pass your own
`httpx.Client`, configure the proxy on it directly instead.

## Exceptions

All exceptions raised by providers subclass `SmsProviderError`.

| Exception | Raised when |
|---|---|
| `InvalidApiKey` | API key is missing or invalid |
| `AccountBlocked` | account blocked / not activated / banned / IP access denied |
| `InsufficientBalance` | not enough funds |
| `NoNumbersAvailable` | no numbers available for the service/country |
| `RateLimited` | channel or request-rate limit exceeded |
| `ActivationNotFound` | unknown `activation_id` / `tzid` |
| `ActivationCancelled` | activation ended without a code, on the service's side (has `.status`) |
| `ActivationTimeout` | local `wait_code` deadline was reached |
| `OperationNotAllowed` | service refused cancel/finish (protective interval, OTP already received) |
| `ProviderUnavailable` | network error, transport timeout, or HTTP 5xx after retries |
| `ProviderAPIError` | anything else, or an unrecognized API error code |
| `ProviderNotRegistered` | unknown provider name passed to the manager |

## Known limitations

- `verificationType` 1/2 (call / voice verification) is not supported in
  `SmsActivateCompatibleProvider`; a response of that shape raises
  `NotImplementedError`. Planned for a future release.
- `OnlineSimProvider.cancel()` is implemented as `setOperationOk` (there is
  no dedicated cancel endpoint in the single-service-activation API) - its
  refund semantics have not been verified against a live operation. See the
  `# TODO(v0.2)` in `providers/online_sim.py`.
- Automatic discovery of third-party providers via `entry_points`
  (`sms_providers.providers` group) is deferred to 0.2.0; register your
  provider explicitly for now (see "Adding your own service").
- No normalization of service/country codes between providers - each
  provider's codes are its own, native ones.

API keys are only ever taken from constructor/config parameters - this
repository and its tests contain no real keys, only the literal `"test-key"`.
