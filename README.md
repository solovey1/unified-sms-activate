# sms-providers

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

A unified Python interface for SMS activation services. One contract
(`BaseSmsProvider`) covers buying a number, waiting for the code, and
closing the activation, so your code doesn't change when you switch or add
a provider. Ships with clients for [HeroSMS](https://hero-sms.com) and
[OnlineSim](https://onlinesim.io), plus a base class for any service that
speaks the sms-activate `handler_api.php` protocol.

The package is async-only, built on `httpx.AsyncClient`; a generated
sync facade is planned on demand — see CHANGELOG.

## Installation

```bash
pip install sms-providers
```

## Quickstart

```python
import asyncio
from sms_providers.providers import HeroSmsProvider

async def main() -> None:
    async with HeroSmsProvider(api_key="...") as provider:
        number = await provider.get_number(service="tg", country=0)
        print(number.phone)
        print(number.country_phone_code)  # "62" or None
        code = await provider.wait_code(number.id, timeout=180)
        print(code.code)
        await provider.finish(number.id)

asyncio.run(main())
```

## Supported services

- **HeroSMS** (`sms_providers.providers.HeroSmsProvider`) - sms-activate-compatible.
- **SMS-Activate** (`sms_providers.providers.SmsActivateProvider`) - the original
  service behind the protocol. Note: unreachable from some regions/networks;
  pass a mirror via `base_url=` if the default host times out.
- **VAK SMS** (`sms_providers.providers.VakSmsProvider`) - sms-activate-compatible.
- **OnlineSim** (`sms_providers.providers.OnlineSimProvider`) - own protocol.
- **Spanch SMS** (`sms_providers.providers.SpanchSmsProvider`) - own protocol.
  `country` is a two-letter ISO code (`"RU"`), not numeric like the other
  providers. `gateway` is required for `get_number()` and has no default -
  pass `gateway=` per call or `default_gateway=` in the constructor; see
  `get_gateways()` for the current list.

`PhoneNumber.country_phone_code` (the E.164 phone prefix, no `+`) is only
filled in for sms-activate-compatible providers when `use_get_number_v2 =
True` (on by default for HeroSMS, since plain-text `getNumber` doesn't carry
it) - it's always filled in for OnlineSim, whose `country` parameter already
is the phone prefix.

Service and country codes (`service="tg"`, `country=0`, ...) are passed
through untouched to each provider's native API. The package does **not**
normalize service/country codes between providers (e.g. HeroSMS's `tg` vs.
another provider's own code for Telegram) - such a mapping table would go
stale faster than releases ship. Consult each provider's own documentation
for its codes, or discover them at runtime:

```python
for service in await provider.get_services():
    print(service.code, service.name)
```

`Service.code`/`Country.code` are exactly the values `get_number()` accepts
for `service=`/`country=` on that same provider. `get_services(search=...)`
narrows the listing by a case-insensitive substring on code/name; providers
describe the same service differently (HeroSMS: `"Bybit"`, VAK SMS:
`"bybit.com"`), so prefer `find_service(name)` over guessing a code by hand
- it tries an exact code, then an exact name, then a name with a trailing
domain suffix stripped from both sides, and returns the first match or
`None`:

```python
service = await provider.find_service("bybit")  # -> Service(code="bybit", ...)
```

## Prices

`get_prices(service=None, country=None)` answers "where and for how much is
a service available", in one call - `Decimal` cost and `int` count per
country, as a list of `CountryPrice`:

```python
for price in await provider.get_prices(service="tg"):
    print(price.country, price.cost, price.count)
```

Support and exact behavior vary by provider - see each provider's
docstring. `count == 0` is a real, meaningful value (out of stock) and is
never filtered out; it's up to the caller to decide what to do with it.

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

No subclass is needed at all if you just want to point the package at an
sms-activate-protocol host (a mirror, a self-hosted clone, a service we
don't ship a class for) - `base_url` and `name` are constructor parameters:

```python
from sms_providers import SmsActivateCompatibleProvider

provider = SmsActivateCompatibleProvider(
    api_key="...",
    name="my-host",
    base_url="https://my-host.example/stubs/handler_api.php",
)
```

The same works through `from_config` - the base class is registered as
`"sms-activate-compatible"`:

```python
manager = SmsProviderManager.from_config({
    "my-host": {
        "provider": "sms-activate-compatible",
        "api_key": "...",
        "base_url": "https://my-host.example/stubs/handler_api.php",
    },
})
```

### Fully custom protocol

Subclass `BaseSmsProvider` directly and implement its six abstract methods
as `async def`, raising the package's exceptions instead of leaking
transport/JSON errors:

```python
from decimal import Decimal
from sms_providers import ActivationStatus, BaseSmsProvider, PhoneNumber, SmsCode, SmsProviderManager

class MyProvider(BaseSmsProvider):
    name = "my-service"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def get_balance(self) -> Decimal: ...
    async def get_number(self, service: str, country=None, **options) -> PhoneNumber: ...
    async def get_status(self, activation_id: str) -> ActivationStatus: ...
    async def wait_code(self, activation_id: str, *, timeout=None, poll_interval=None) -> SmsCode: ...
    async def cancel(self, activation_id: str) -> None: ...
    async def finish(self, activation_id: str) -> None: ...
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
number = await provider.get_number(service="tg")
...
await manager.aclose_all()
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
`httpx.AsyncClient`, configure the proxy on it directly instead.

## One provider instance = one event loop

A provider owns an `httpx.AsyncClient`, and that client is bound to the
event loop it first runs in. Reusing a provider across separate
`asyncio.run()` calls (e.g. one per menu action or per job) fails with
`RuntimeError: Event loop is closed` — create the provider *inside* the
loop that uses it, or keep one long-lived loop for the whole app:

```python
async def handle_action():
    async with HeroSmsProvider(api_key="...") as provider:  # per-run
        ...

asyncio.run(handle_action())
```

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
- `SpanchSmsProvider` does not support rental or email-verification
  activations (only regular SMS activations, v1 scope).
- No normalization of service/country codes between providers - each
  provider's codes are its own, native ones.

API keys are only ever taken from constructor/config parameters - this
repository and its tests contain no real keys, only the literal `"test-key"`.
