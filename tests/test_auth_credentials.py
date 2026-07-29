"""Tests for Meta credential handling."""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest
from pydantic import SecretStr

from app.auth.credentials import MetaCredentials
from app.config.settings import MetaApiSettings
from app.utils.exceptions import ConfigurationError


def test_authorization_header_uses_the_bearer_scheme(credentials: MetaCredentials) -> None:
    headers = credentials.authorization_headers()

    # A bearer header keeps the token out of URLs, which intermediaries and
    # access logs routinely record.
    assert headers == {"Authorization": "Bearer test-access-token"}


def test_appsecret_proof_is_the_hmac_of_the_token_keyed_by_the_secret(
    credentials: MetaCredentials,
) -> None:
    expected = hmac.new(
        key=b"test-app-secret",
        msg=b"test-access-token",
        digestmod=sha256,
    ).hexdigest()

    assert credentials.appsecret_proof() == expected
    assert credentials.proof_parameters() == {"appsecret_proof": expected}


def test_appsecret_proof_is_stable_across_calls(credentials: MetaCredentials) -> None:
    assert credentials.appsecret_proof() == credentials.appsecret_proof()


def test_from_settings_carries_the_app_id(meta_settings: MetaApiSettings) -> None:
    credentials = MetaCredentials.from_settings(meta_settings)

    assert credentials.app_id == meta_settings.app_id


def test_repr_cannot_leak_a_secret(credentials: MetaCredentials) -> None:
    rendered = repr(credentials)

    assert "test-access-token" not in rendered
    assert "test-app-secret" not in rendered
    assert "1234567890" in rendered


@pytest.mark.parametrize(
    ("access_token", "app_secret"),
    [("", "secret"), ("token", "")],
)
def test_empty_credentials_are_rejected_at_construction(
    access_token: str,
    app_secret: str,
) -> None:
    with pytest.raises(ConfigurationError):
        MetaCredentials(
            access_token=SecretStr(access_token),
            app_secret=SecretStr(app_secret),
            app_id="1234567890",
        )
