"""Tests for environment-sourced configuration."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config.settings import (
    DatabaseSettings,
    LogFormat,
    LoggingSettings,
    LogLevel,
    MetaApiSettings,
    load_settings,
)
from app.utils.exceptions import ConfigurationError

_REQUIRED_META_ENVIRONMENT = {
    "META_ACCESS_TOKEN": "a-token",
    "META_APP_ID": "1234567890",
    "META_APP_SECRET": "a-secret",
    "META_AD_ACCOUNT_ID": "act_9876543210",
}


def test_meta_settings_read_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _REQUIRED_META_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("META_REQUEST_TIMEOUT_SECONDS", "12.5")

    # Pydantic's dataclass_transform makes a type checker demand every required
    # field; BaseSettings fills them from the environment, which is the whole
    # point of this test.
    settings = MetaApiSettings()  # type: ignore[call-arg]

    assert settings.access_token.get_secret_value() == "a-token"
    assert settings.ad_account_id == "act_9876543210"
    assert settings.request_timeout_seconds == 12.5


def test_graph_url_joins_base_and_version() -> None:
    settings = MetaApiSettings(
        access_token=SecretStr("t"),
        app_id="1",
        app_secret=SecretStr("s"),
        ad_account_id="act_1",
        api_base_url="https://graph.facebook.com/",
        api_version="v23.0",
    )

    # The trailing slash on the base URL must not produce a doubled separator.
    assert settings.graph_url == "https://graph.facebook.com/v23.0"


@pytest.mark.parametrize("bad_account_id", ["1234567890", "act-123", "", "ACT_123"])
def test_ad_account_id_must_carry_the_act_prefix(bad_account_id: str) -> None:
    with pytest.raises(ValidationError):
        MetaApiSettings(
            access_token=SecretStr("t"),
            app_id="1",
            app_secret=SecretStr("s"),
            ad_account_id=bad_account_id,
        )


@pytest.mark.parametrize("bad_version", ["23.0", "v23", "latest", ""])
def test_api_version_must_look_like_a_graph_version(bad_version: str) -> None:
    with pytest.raises(ValidationError):
        MetaApiSettings(
            access_token=SecretStr("t"),
            app_id="1",
            app_secret=SecretStr("s"),
            ad_account_id="act_1",
            api_version=bad_version,
        )


def test_blank_secrets_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MetaApiSettings(
            access_token=SecretStr("   "),
            app_id="1",
            app_secret=SecretStr("s"),
            ad_account_id="act_1",
        )


def test_app_id_must_be_numeric() -> None:
    with pytest.raises(ValidationError):
        MetaApiSettings(
            access_token=SecretStr("t"),
            app_id="not-a-number",
            app_secret=SecretStr("s"),
            ad_account_id="act_1",
        )


def test_secrets_are_not_exposed_by_repr() -> None:
    settings = MetaApiSettings(
        access_token=SecretStr("super-secret-token"),
        app_id="1",
        app_secret=SecretStr("super-secret-app-secret"),
        ad_account_id="act_1",
    )

    rendered = repr(settings)

    assert "super-secret-token" not in rendered
    assert "super-secret-app-secret" not in rendered


def test_safe_url_masks_the_database_password() -> None:
    settings = DatabaseSettings(
        url="postgresql+psycopg://meta:hunter2@postgres:5432/meta_optimizer"
    )

    assert settings.safe_url == "postgresql+psycopg://meta:***@postgres:5432/meta_optimizer"
    assert "hunter2" not in settings.safe_url


def test_database_url_must_have_a_scheme() -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(url="just-a-database-name")


def test_logging_settings_use_explicit_variable_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = LoggingSettings()

    assert settings.level is LogLevel.DEBUG
    assert settings.output_format is LogFormat.JSON


def test_load_settings_reports_the_environment_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _REQUIRED_META_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    # Invalid: the prefix is missing, so validation must fail.
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "9876543210")

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    problems = failure.value.context["problems"]
    # The operator sets environment variables, not Pydantic field names, so the
    # message must name the variable they can actually change.
    assert any("META_AD_ACCOUNT_ID" in problem for problem in problems)


def test_load_settings_succeeds_with_a_complete_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _REQUIRED_META_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    settings = load_settings()

    assert settings.meta.ad_account_id == "act_9876543210"
    assert settings.logging.level is LogLevel.WARNING
    assert settings.database.url.endswith("/d")
