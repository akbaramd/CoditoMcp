from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from urllib.parse import urlparse

import django_stubs_ext
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured

django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://codito.akbaramd.ir").rstrip("/")
DESKTOP_RESOURCE = f"{PUBLIC_BASE_URL}/device-api"
DESKTOP_OAUTH_CLIENT_ID = os.getenv("DESKTOP_OAUTH_CLIENT_ID", "codito-windows-agent")
parsed_public_url = urlparse(PUBLIC_BASE_URL)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", parsed_public_url.hostname or "localhost")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", PUBLIC_BASE_URL)


def oidc_rsa_private_key() -> str:
    encoded = os.getenv("OIDC_RSA_PRIVATE_KEY_B64", "").strip()
    direct = os.getenv("OIDC_RSA_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if encoded and direct:
        raise ImproperlyConfigured(
            "Set only OIDC_RSA_PRIVATE_KEY_B64 (production) or OIDC_RSA_PRIVATE_KEY (local)"
        )
    if direct and not DEBUG:
        raise ImproperlyConfigured(
            "Direct OIDC_RSA_PRIVATE_KEY is permitted only with DJANGO_DEBUG=true; "
            "use OIDC_RSA_PRIVATE_KEY_B64 in production"
        )
    if encoded:
        try:
            key_text = base64.b64decode(encoded, validate=True).decode("ascii").strip()
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ImproperlyConfigured("OIDC_RSA_PRIVATE_KEY_B64 is not strict base64 PEM") from exc
    else:
        key_text = direct
    if not key_text:
        return ""
    try:
        key = serialization.load_pem_private_key(key_text.encode("ascii"), password=None)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured(
            "OIDC signing key is not an unencrypted PEM private key"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ImproperlyConfigured(
            "OIDC signing key must be an RSA private key of at least 2048 bits"
        )
    return key_text + "\n"


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "oauth2_provider",
    "codito_relay.core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *([] if DEBUG else ["whitenoise.middleware.WhiteNoiseMiddleware"]),
    "codito_relay.core.middleware.PublicRateLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "codito_relay.core.middleware.SessionIdleTimeoutMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "codito_relay.urls"
WSGI_APPLICATION = "codito_relay.wsgi.application"
ASGI_APPLICATION = "codito_relay.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

database_url = os.getenv("DATABASE_URL", "")
if database_url:
    parsed_db = urlparse(database_url)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed_db.path.lstrip("/"),
            "USER": parsed_db.username,
            "PASSWORD": parsed_db.password,
            "HOST": parsed_db.hostname,
            "PORT": parsed_db.port or 5432,
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "prefer")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(BASE_DIR / "db.sqlite3"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
SESSION_ABSOLUTE_TIMEOUT_SECONDS = 12 * 60 * 60
SESSION_COOKIE_AGE = SESSION_ABSOLUTE_TIMEOUT_SECONDS
SESSION_IDLE_TIMEOUT_SECONDS = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

CHATGPT_CLIENT_METADATA_HOSTS = env_list("CHATGPT_CLIENT_METADATA_HOSTS", "chatgpt.com,openai.com")
MCP_TOOL_SCOPES = (
    "projects:read",
    "projects:write",
    "files:read",
    "files:write",
    "shell:execute",
)

OAUTH2_PROVIDER = {
    "OAUTH2_VALIDATOR_CLASS": "codito_relay.core.oauth.CoditoOAuth2Validator",
    "PKCE_REQUIRED": True,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 15 * 60,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 60 * 60,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
    "SCOPES": {
        "openid": "Authenticate the signed-in account",
        "profile": "Read the signed-in account profile",
        "projects:read": "List projects and project metadata",
        "projects:write": "Request, rename, or unregister projects with local confirmation",
        "files:read": "Read files in registered projects",
        "files:write": "Apply anchored file patches in registered projects",
        "shell:execute": "Run bounded commands in registered projects",
        "device:manage": "Enroll and reconnect the signed-in user's Windows devices",
    },
    "RESOURCE_SERVER_INTROSPECTION_URL": f"{PUBLIC_BASE_URL}/o/introspect/",
    "RESOURCE_SERVER_TOKEN_RESOURCE_VALIDATOR": "codito_relay.core.oauth.exact_resource_match",
    # DOT applies RFC 8252 host/path matching and permits only dynamic ports for loopback IPs.
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http"],
    "CIMD_ENABLED": True,
    "CIMD_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.cimd.HostAllowlistCIMDPermission",),
    "CIMD_ALLOWED_HOSTS": CHATGPT_CLIENT_METADATA_HOSTS,
    "CIMD_METADATA_FETCHER": "codito_relay.core.oauth.CoditoCIMDMetadataFetcher",
    "OIDC_ENABLED": True,
    "OIDC_ISS_ENDPOINT": PUBLIC_BASE_URL,
    "OIDC_USERINFO_ENDPOINT": f"{PUBLIC_BASE_URL}/oidc/userinfo",
    "OIDC_RSA_PRIVATE_KEY": oidc_rsa_private_key(),
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
    "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
    "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
    "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
    # RFC 8252 requires an HTTP loopback redirect for the installed desktop client.
    # All non-loopback redirects remain HTTPS through DOT's URI validator.
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": False,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
    "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
}
OAUTH2_PROVIDER_ACCESS_TOKEN_MODEL = "oauth2_provider.AccessToken"  # noqa: S105

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RATE_LIMIT_ENABLED = env_bool("RATE_LIMIT_ENABLED", not DEBUG)
RATE_LIMIT_REDIS_URL = os.getenv("RATE_LIMIT_REDIS_URL", REDIS_URL)
RATE_LIMIT_REDIS_TIMEOUT_SECONDS = float(os.getenv("RATE_LIMIT_REDIS_TIMEOUT_SECONDS", "1.0"))
RATE_LIMIT_KEY_PREFIX = os.getenv("RATE_LIMIT_KEY_PREFIX", "codito:ratelimit:v1")
PUBLIC_RATE_LIMITS = {
    "login": (int(os.getenv("RATE_LIMIT_LOGIN_REQUESTS", "10")), 5 * 60),
    "invitation": (int(os.getenv("RATE_LIMIT_INVITATION_REQUESTS", "20")), 60 * 60),
    "password_reset": (int(os.getenv("RATE_LIMIT_PASSWORD_RESET_REQUESTS", "20")), 60 * 60),
    "oauth_authorize": (int(os.getenv("RATE_LIMIT_OAUTH_AUTHORIZE_REQUESTS", "60")), 60),
    "oauth_token": (int(os.getenv("RATE_LIMIT_OAUTH_TOKEN_REQUESTS", "30")), 60),
    "device_enroll": (int(os.getenv("RATE_LIMIT_DEVICE_ENROLL_REQUESTS", "10")), 60),
    "device_ticket": (int(os.getenv("RATE_LIMIT_DEVICE_TICKET_REQUESTS", "30")), 60),
}
DEVICE_OFFLINE_AFTER_SECONDS = int(os.getenv("DEVICE_OFFLINE_AFTER_SECONDS", "75"))
DEVICE_QUEUE_LIMIT = int(os.getenv("DEVICE_QUEUE_LIMIT", "32"))
DEVICE_STREAM_MAXLEN = int(os.getenv("DEVICE_STREAM_MAXLEN", "4096"))
OPERATION_TIMEOUT_SECONDS = int(os.getenv("OPERATION_TIMEOUT_SECONDS", "45"))
WS_TICKET_TTL_SECONDS = int(os.getenv("WS_TICKET_TTL_SECONDS", "60"))
DEVICE_PROTOCOL_VERSION = 1

METRICS_BEARER_TOKEN = os.getenv("METRICS_BEARER_TOKEN", "")
HEALTH_READY_CHECK_REDIS = env_bool("HEALTH_READY_CHECK_REDIS", True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "format": (
                '{{"time":"{asctime}","level":"{levelname}",'
                '"logger":"{name}","message":"{message}"}}'
            ),
            "style": "{",
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
