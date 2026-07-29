"""Credential handling for outbound Meta Marketing API calls.

Isolated from the HTTP client so that how a request is authenticated can change
without touching how it is sent, retried, or parsed. The client asks for headers
and proof parameters; it never sees the raw token.

Two mechanisms are used together. The access token is sent as an ``Authorization:
Bearer`` header rather than as a query parameter, keeping it out of request URLs
that intermediaries and access logs routinely record. Alongside it goes
``appsecret_proof``, an HMAC of the token keyed by the app secret, which proves
the caller possesses the secret and not merely a token someone leaked. Meta
requires it on server-side calls for apps configured to demand proof, and
sending it always is simpler than tracking which apps do.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Final

from pydantic import SecretStr

from app.config.settings import MetaApiSettings
from app.utils.exceptions import ConfigurationError

_AUTHORIZATION_HEADER: Final[str] = "Authorization"
_BEARER_SCHEME: Final[str] = "Bearer"
_APPSECRET_PROOF_PARAMETER: Final[str] = "appsecret_proof"


class MetaCredentials:
    """Holds Meta API credentials and derives the values a request needs.

    Secrets stay wrapped in :class:`~pydantic.SecretStr` for as long as
    possible; they are unwrapped only inside the two methods that must transmit
    or hash them. The object has no ``__repr__`` exposing them, so it can appear
    in a traceback without leaking a token.
    """

    def __init__(self, *, access_token: SecretStr, app_secret: SecretStr, app_id: str) -> None:
        if not access_token.get_secret_value():
            raise ConfigurationError("META_ACCESS_TOKEN must not be empty")
        if not app_secret.get_secret_value():
            raise ConfigurationError("META_APP_SECRET must not be empty")
        self._access_token = access_token
        self._app_secret = app_secret
        self._app_id = app_id

    @classmethod
    def from_settings(cls, settings: MetaApiSettings) -> MetaCredentials:
        """Build credentials from validated configuration.

        Args:
            settings: Meta API settings loaded from the environment.

        Returns:
            Credentials ready to authenticate requests.
        """
        return cls(
            access_token=settings.access_token,
            app_secret=settings.app_secret,
            app_id=settings.app_id,
        )

    @property
    def app_id(self) -> str:
        """The Meta app ID the token was issued for. Not a secret."""
        return self._app_id

    def authorization_headers(self) -> dict[str, str]:
        """Return the headers authenticating a request.

        Returns:
            A mapping carrying the bearer token, ready to merge into a request.
        """
        return {_AUTHORIZATION_HEADER: f"{_BEARER_SCHEME} {self._access_token.get_secret_value()}"}

    def appsecret_proof(self) -> str:
        """Compute the HMAC-SHA256 proof of possession of the app secret.

        The proof is the hex digest of the access token keyed by the app secret,
        exactly as Meta specifies. ``hmac.new`` is used rather than a plain hash
        so the construction is not vulnerable to length extension.

        Returns:
            Lower-case hex digest.
        """
        return hmac.new(
            key=self._app_secret.get_secret_value().encode("utf-8"),
            msg=self._access_token.get_secret_value().encode("utf-8"),
            digestmod=sha256,
        ).hexdigest()

    def proof_parameters(self) -> dict[str, str]:
        """Return the query parameters proving possession of the app secret.

        Returns:
            A mapping ready to merge into a request's query string.
        """
        return {_APPSECRET_PROOF_PARAMETER: self.appsecret_proof()}

    def __repr__(self) -> str:
        """Return a representation that cannot leak a secret."""
        return f"MetaCredentials(app_id={self._app_id!r})"
