"""Sign in to Azure as the person running the tool.

No role definition or subscription ID to configure: whatever your account can see and
start/stop is what weekend-saver can. An existing `az login` session is used first; if
there is none, a browser sign-in (cached on this PC) is used instead.
"""

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AuthenticationRecord,
    AzureCliCredential,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

from weekend_saver.errors import WeekendSaverError
from weekend_saver.files import write_text_atomically

logger = logging.getLogger(__name__)

ARM_SCOPE = "https://management.azure.com/.default"
AUTH_RECORD_FILE = "auth-record.json"
TOKEN_CACHE_NAME = "weekend-saver"
# `az` is slow to start on Windows; the SDK's 10 s default times out on a cold start
CLI_TIMEOUT_SECONDS = 30
# Ask for a new token a little before the old one stops working
EXPIRY_MARGIN_SECONDS = 300

_ACCOUNT_CLAIMS = ("upn", "unique_name", "preferred_username", "email", "appid")


class NotSignedInError(WeekendSaverError):
    """Neither the Azure CLI nor a cached browser sign-in can produce a token."""


@dataclass(frozen=True)
class SignInStatus:
    signed_in: bool
    account: str = ""
    tenant_id: str = ""
    source: str = ""


class SignedInCredential:
    """A TokenCredential backed by `az login`, then by a cached browser sign-in."""

    def __init__(
        self,
        record_path: Path,
        *,
        cli_factory: Callable[..., TokenCredential] = AzureCliCredential,
        browser_factory: Callable[..., Any] = InteractiveBrowserCredential,
        primary_name: str = "azure-cli",
    ) -> None:
        """`cli_factory` builds the first source tried.

        The dashboard uses the Azure CLI so it always acts as the person at the keyboard.
        The command line passes DefaultAzureCredential instead, which also covers managed
        identities and service principals for unattended runs.
        """
        self._record_path = record_path
        self._cli = cli_factory(process_timeout=CLI_TIMEOUT_SECONDS)
        self._primary_name = primary_name
        self._browser_factory = browser_factory
        self._browser: TokenCredential | None = None
        # The SDK calls get_token from worker threads; `az` is too slow to call per request
        self._lock = threading.Lock()
        self._cached: dict[tuple[Any, ...], tuple[AccessToken, str]] = {}

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return self._token_and_source(scopes, kwargs)[0]

    def status(self) -> SignInStatus:
        """Report who is signed in without raising when nobody is."""
        try:
            token, source = self._token_and_source((ARM_SCOPE,), {})
        except NotSignedInError:
            return SignInStatus(signed_in=False)
        claims = _unverified_claims(token.token)
        account = next((claims[name] for name in _ACCOUNT_CLAIMS if claims.get(name)), "")
        # Personal Microsoft accounts arrive as "live.com#name@example.com"
        display_name = str(account).rsplit("#", 1)[-1]
        return SignInStatus(True, display_name, str(claims.get("tid", "")), source)

    def login(self) -> SignInStatus:
        """Open a browser sign-in and remember the account for later silent use."""
        interactive = self._browser_factory(cache_persistence_options=_cache_options())
        record = interactive.authenticate(scopes=[ARM_SCOPE])
        write_text_atomically(self._record_path, record.serialize())
        with self._lock:
            self._browser = None
            self._cached.clear()
        return self.status()

    def _token_and_source(
        self, scopes: tuple[str, ...], kwargs: dict[str, Any]
    ) -> tuple[AccessToken, str]:
        if kwargs.get("claims"):
            # A claims challenge asks for a different token; a cached one would fail again
            with self._lock:
                return self._acquire(scopes, kwargs)
        key = (scopes, kwargs.get("tenant_id"), kwargs.get("enable_cae"))
        with self._lock:
            cached = self._cached.get(key)
            if cached and cached[0].expires_on - time.time() > EXPIRY_MARGIN_SECONDS:
                return cached
            fresh = self._acquire(scopes, kwargs)
            self._cached[key] = fresh
            return fresh

    def _acquire(self, scopes: tuple[str, ...], kwargs: dict[str, Any]) -> tuple[AccessToken, str]:
        try:
            return self._cli.get_token(*scopes, **kwargs), self._primary_name
        except ClientAuthenticationError:
            logger.debug("No token from %s", self._primary_name, exc_info=True)
        try:
            return self._silent_browser().get_token(*scopes, **kwargs), "browser"
        except ClientAuthenticationError as exc:
            logger.debug("No usable cached browser sign-in", exc_info=True)
            raise NotSignedInError(
                "Not signed in to Azure. Run `az login`, or `weekend-saver login` to sign in "
                "through a browser (the dashboard has a Sign in button for the same thing)."
            ) from exc

    def _silent_browser(self) -> TokenCredential:
        if self._browser is None:
            self._browser = self._browser_factory(
                cache_persistence_options=_cache_options(),
                authentication_record=self._load_record(),
                # A scheduled run must fail with "sign in again", never pop a browser
                disable_automatic_authentication=True,
            )
        return self._browser

    def _load_record(self) -> AuthenticationRecord | None:
        try:
            return AuthenticationRecord.deserialize(self._record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError):
            logger.warning("Ignoring unreadable sign-in record %s", self._record_path)
            return None


def _cache_options() -> TokenCachePersistenceOptions:
    return TokenCachePersistenceOptions(name=TOKEN_CACHE_NAME)


def _unverified_claims(token: str) -> dict[str, Any]:
    """Read display fields from a token. Not a security check: Azure verifies the token."""
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}
