"""Typed application configuration, sourced exclusively from the environment.

This module is the single place in the codebase that reads environment
variables. Every field below corresponds one-to-one with a variable documented
in ``.env.example``; adding a field here without documenting it there is a
defect.

Settings are grouped by the concern that owns them, so a component receives only
what it needs: the API client is handed :class:`MetaApiSettings` and cannot
reach database credentials. There is no module-level settings instance and no
accessor that lazily creates one. :func:`load_settings` is called exactly once,
in the CLI composition root, and the result is injected downwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.exceptions import ConfigurationError

# Loaded when present, for running outside Compose. Inside Compose the variables
# are already in the environment via `env_file`, and real environment variables
# always take precedence over file contents.
_ENV_FILE: Final[str] = ".env"

# Meta identifies ad accounts with an `act_` prefix. Passing the bare numeric ID
# yields a confusing 400 from the Graph API, so it is rejected up front.
_AD_ACCOUNT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^act_\d+$")

# Graph API versions are always `v<major>.<minor>`, e.g. `v23.0`.
_API_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v\d+\.\d+$")

_URL_SCHEME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://")

_ALLOWED_API_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

# Matches the credentials segment of a SQLAlchemy URL so the password can be
# masked before a connection string reaches a log record.
_URL_CREDENTIALS_PATTERN: Final[re.Pattern[str]] = re.compile(r"://(?P<user>[^:/@]+):[^@]*@")


class LogLevel(StrEnum):
    """Severity threshold applied to the root logger."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormat(StrEnum):
    """Rendering style for emitted log records."""

    CONSOLE = "console"
    JSON = "json"


class MetaApiSettings(BaseSettings):
    """Credentials and transport policy for the Meta Marketing API.

    Field names map to ``META_``-prefixed environment variables: ``access_token``
    is read from ``META_ACCESS_TOKEN``, ``request_timeout_seconds`` from
    ``META_REQUEST_TIMEOUT_SECONDS``, and so on.

    Secrets are held as :class:`~pydantic.SecretStr` so that an accidental
    ``repr`` of the settings object, whether in a traceback or a log record,
    prints ``**********`` instead of a token with access to live ad spend.
    """

    model_config = SettingsConfigDict(
        env_prefix="META_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    access_token: SecretStr
    app_id: str
    app_secret: SecretStr
    ad_account_id: str
    api_version: str = "v23.0"
    api_base_url: str = "https://graph.facebook.com"
    request_timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=1.0, gt=0.0)
    rate_limit_pause_seconds: float = Field(default=60.0, gt=0.0)

    @field_validator("access_token", "app_secret")
    @classmethod
    def _reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            message = "must not be empty; fill it in after copying .env.example to .env"
            raise ValueError(message)
        return value

    @field_validator("app_id")
    @classmethod
    def _validate_app_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.isdigit():
            message = f"must be the numeric Meta app ID, got {value!r}"
            raise ValueError(message)
        return stripped

    @field_validator("ad_account_id")
    @classmethod
    def _validate_ad_account_id(cls, value: str) -> str:
        stripped = value.strip()
        if not _AD_ACCOUNT_ID_PATTERN.fullmatch(stripped):
            message = f"must look like 'act_123456789012345', got {value!r}"
            raise ValueError(message)
        return stripped

    @field_validator("api_version")
    @classmethod
    def _validate_api_version(cls, value: str) -> str:
        stripped = value.strip()
        if not _API_VERSION_PATTERN.fullmatch(stripped):
            message = f"must look like 'v23.0', got {value!r}"
            raise ValueError(message)
        return stripped

    @field_validator("api_base_url")
    @classmethod
    def _validate_api_base_url(cls, value: str) -> str:
        stripped = value.strip().rstrip("/")
        match = _URL_SCHEME_PATTERN.match(stripped)
        if match is None or match.group("scheme").lower() not in _ALLOWED_API_URL_SCHEMES:
            message = f"must be an http(s) URL, got {value!r}"
            raise ValueError(message)
        return stripped

    @property
    def graph_url(self) -> str:
        """Versioned Graph API root that every request path is appended to."""
        return f"{self.api_base_url}/{self.api_version}"


class DatabaseSettings(BaseSettings):
    """Connection and pooling policy for PostgreSQL.

    Field names map to ``DATABASE_``-prefixed environment variables, so ``url``
    is read from ``DATABASE_URL``.
    """

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    url: str
    echo_sql: bool = False
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        stripped = value.strip()
        match = _URL_SCHEME_PATTERN.match(stripped)
        if match is None:
            message = f"must be a SQLAlchemy URL such as 'postgresql+psycopg://...', got {value!r}"
            raise ValueError(message)
        return stripped

    @property
    def safe_url(self) -> str:
        """Connection URL with the password masked, safe to log or display."""
        return _URL_CREDENTIALS_PATTERN.sub(r"://\g<user>:***@", self.url)


class LoggingSettings(BaseSettings):
    """Verbosity and rendering style for application logs.

    These variables carry no common prefix, so each field names its environment
    variable explicitly. ``output_format`` reads ``LOG_FORMAT``; the field is not
    called ``format`` because that would shadow the builtin.

    ``populate_by_name`` is enabled so the class can also be constructed
    directly, as ``LoggingSettings(level=LogLevel.DEBUG)``. Without it a
    validation alias becomes the *only* accepted key, and passing the field name
    is silently ignored rather than rejected — which would make every explicit
    construction quietly fall back to the ambient environment.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    level: LogLevel = Field(default=LogLevel.INFO, validation_alias="LOG_LEVEL")
    output_format: LogFormat = Field(default=LogFormat.CONSOLE, validation_alias="LOG_FORMAT")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Every setting the application has, grouped by owning concern.

    Constructed once by :func:`load_settings` and injected from there. It is
    frozen because configuration that changes while the process runs would make
    behaviour depend on when a component happened to read it.
    """

    meta: MetaApiSettings
    database: DatabaseSettings
    logging: LoggingSettings


def load_settings() -> AppSettings:
    """Read and validate all configuration from the environment.

    Returns:
        A fully validated :class:`AppSettings`.

    Raises:
        ConfigurationError: If any required variable is missing or any value
            fails validation. The message names every offending variable, so a
            misconfigured deployment is diagnosable from one line of output
            rather than from a stack trace.
    """
    # Pydantic marks its metaclass with `dataclass_transform`, so a type checker
    # synthesizes an `__init__` demanding every required field as a keyword
    # argument. `BaseSettings` fills those from the environment during
    # construction, which no static analysis can see. The ignores are narrow and
    # deliberate: they suppress exactly this synthesized signature and nothing
    # else, and the runtime validation below is what actually enforces presence.
    try:
        return AppSettings(
            meta=MetaApiSettings(),  # type: ignore[call-arg]
            database=DatabaseSettings(),  # type: ignore[call-arg]
            logging=LoggingSettings(),
        )
    except ValidationError as exc:
        raise ConfigurationError(
            "Invalid configuration; see .env.example for the documented variables",
            context={"problems": _describe_validation_errors(exc)},
        ) from exc


def _describe_validation_errors(exc: ValidationError) -> list[str]:
    """Convert a Pydantic validation failure into operator-facing descriptions.

    Pydantic reports the offending *field* name; an operator sets *environment
    variables*. This translates one to the other so the message points at the
    thing they can actually change.
    """
    model_name = exc.title
    prefix = _ENV_PREFIX_BY_MODEL_NAME.get(model_name, "")
    described: list[str] = []
    for error in exc.errors():
        location = error["loc"]
        field_name = str(location[0]) if location else "<unknown>"
        variable = _EXPLICIT_ENV_VARIABLE_BY_FIELD.get(field_name, f"{prefix}{field_name.upper()}")
        described.append(f"{variable}: {error['msg']}")
    return described


# LoggingSettings names its variables explicitly rather than by prefix, so the
# field-name-to-variable mapping cannot be derived and is stated here instead.
_EXPLICIT_ENV_VARIABLE_BY_FIELD: Final[dict[str, str]] = {
    "level": "LOG_LEVEL",
    "output_format": "LOG_FORMAT",
}

_ENV_PREFIX_BY_MODEL_NAME: Final[dict[str, str]] = {
    MetaApiSettings.__name__: "META_",
    DatabaseSettings.__name__: "DATABASE_",
    LoggingSettings.__name__: "",
}
