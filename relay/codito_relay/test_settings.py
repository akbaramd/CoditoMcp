"""Deterministic, non-production Django settings used by the relay test suite."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .settings import *  # noqa: F403 - Django test settings intentionally extend base settings.

DEBUG = True
SECURE_SSL_REDIRECT = False
HEALTH_READY_CHECK_REDIS = False
RATE_LIMIT_ENABLED = False
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MIDDLEWARE = [
    item
    for item in MIDDLEWARE  # noqa: F405
    if item != "whitenoise.middleware.WhiteNoiseMiddleware"
]
STORAGES = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}

_test_oidc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_oidc_private_pem = _test_oidc_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
OAUTH2_PROVIDER = {
    **OAUTH2_PROVIDER,  # noqa: F405
    "OIDC_RSA_PRIVATE_KEY": _test_oidc_private_pem,
}
