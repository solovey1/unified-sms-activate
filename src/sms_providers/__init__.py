"""Unified Python interface for SMS activation services."""

__version__ = "0.1.0"

from .base import (
    AccountBlocked,
    ActivationCancelled,
    ActivationNotFound,
    ActivationStatus,
    ActivationTimeout,
    BaseSmsProvider,
    Country,
    CountryPrice,
    InsufficientBalance,
    InvalidApiKey,
    NoNumbersAvailable,
    OperationNotAllowed,
    PhoneNumber,
    ProviderAPIError,
    ProviderNotRegistered,
    ProviderUnavailable,
    RateLimited,
    Service,
    SmsCode,
    SmsProviderError,
)
from .manager import SmsProviderManager
from .providers import SmsActivateCompatibleProvider

__all__ = [
    "AccountBlocked",
    "ActivationCancelled",
    "ActivationNotFound",
    "ActivationStatus",
    "ActivationTimeout",
    "BaseSmsProvider",
    "Country",
    "CountryPrice",
    "InsufficientBalance",
    "InvalidApiKey",
    "NoNumbersAvailable",
    "OperationNotAllowed",
    "PhoneNumber",
    "ProviderAPIError",
    "ProviderNotRegistered",
    "ProviderUnavailable",
    "RateLimited",
    "Service",
    "SmsActivateCompatibleProvider",
    "SmsCode",
    "SmsProviderError",
    "SmsProviderManager",
    "__version__",
]

from . import providers as providers
