"""Credentials and endpoints of real providers.

- The API key is read from the environment variable NAMED in the configuration, when a run
  starts. It is held in a `SecretValue` (milestone 4), whose `repr` and `str` never show it
  and which cannot be pickled, and it is never written to the configuration, the database,
  logs, events or exports. The variable is registered for redaction as well.
- SDKs are always given the key and the base URL explicitly, so they never fall back to their
  own default environment variables (`OPENAI_API_KEY`, `ANTHROPIC_BASE_URL`, ...) behind the
  configuration's back.
- An endpoint must use HTTPS. Plain HTTP is accepted only for a loopback host. User names,
  passwords, query strings and fragments are rejected: credentials belong in the environment.
- The endpoint identity (scheme, host, port and base path) is part of the provider identity,
  so results and authorizations of one service never apply to another.
"""

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from whatsapp_analyzer.config import ENV_NAME_PATTERN
from whatsapp_analyzer.llm.redaction import SecretValue, register_credential_env

_LOOPBACK_NAMES = {"localhost"}
# An ASCII host name (internationalized names are written in their xn-- form), so the identity
# shown when authorizing is exactly the host that is contacted.
_HOST_NAME = re.compile(
    r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*"
)


class ProviderSetupError(Exception):
    """A provider cannot be built from the configuration. `code` is a stable identifier;
    `details` holds names and limits only (never a key)."""

    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def resolve_api_key(env_name: str, environ: Mapping[str, str] | None = None) -> SecretValue:
    if ENV_NAME_PATTERN.fullmatch(env_name) is None:
        raise ProviderSetupError("API_KEY_ENV_INVALID", env=env_name)
    value = (os.environ if environ is None else environ).get(env_name)
    if value is None:
        raise ProviderSetupError("API_KEY_MISSING", env=env_name)
    if not value.strip():
        raise ProviderSetupError("API_KEY_EMPTY", env=env_name)
    if value != value.strip() or any(ord(c) < 0x21 or ord(c) == 0x7F for c in value):
        # spaces or control characters would corrupt the Authorization header
        raise ProviderSetupError("API_KEY_MALFORMED", env=env_name)
    register_credential_env(env_name)
    return SecretValue(value)


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int
    path: str  # base path without a trailing slash, "" for the root

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}"

    @property
    def loopback(self) -> bool:
        return is_loopback(self.host)


def is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return _HOST_NAME.fullmatch(host) is not None
    return True


def parse_endpoint(url: str) -> Endpoint:
    """Validate a base URL. Raises ProviderSetupError with a code that says what to fix."""
    if not url.isascii() or "\\" in url or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise ProviderSetupError("BASE_URL_INVALID")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ProviderSetupError("BASE_URL_INVALID") from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"https", "http"} or not host or not _valid_host(host):
        raise ProviderSetupError("BASE_URL_INVALID")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ProviderSetupError("BASE_URL_HAS_CREDENTIALS")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ProviderSetupError("BASE_URL_HAS_QUERY")
    if scheme == "http" and not is_loopback(host):
        raise ProviderSetupError("BASE_URL_INSECURE", host=host)
    path = parts.path.rstrip("/")
    if "/../" in f"{path}/" or "/./" in f"{path}/":
        raise ProviderSetupError("BASE_URL_INVALID")
    return Endpoint(scheme, host, port or (443 if scheme == "https" else 80), path)
