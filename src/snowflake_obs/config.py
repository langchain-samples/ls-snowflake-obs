"""Configuration loaded from the environment.

Nothing here reads a default from a source file: credentials and endpoints come
from environment variables only, so no secret ever lands in version control.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE = "OBSERVABILITY_DB"
DEFAULT_SCHEMA = "OBSERVABILITY_SCHEMA"
DEFAULT_PRIVATE_KEY_PATH = "~/.snowflake/rsa_key.p8"
DEFAULT_OTEL_ENDPOINT = "https://api.smith.langchain.com/otel/v1/traces"
DEFAULT_CURSOR_PATH = ".snowflake_bridge_cursor"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class SnowflakeSettings:
    account: str
    user: str
    warehouse: str
    database: str
    schema: str
    private_key_path: Path
    # repr=False on both secrets: keeps them out of tracebacks and any log line
    # that stringifies a Settings object.
    private_key_passphrase: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class LangSmithSettings:
    api_key: str = field(repr=False)
    project: str
    otel_endpoint: str


@dataclass(frozen=True, slots=True)
class Settings:
    snowflake: SnowflakeSettings
    langsmith: LangSmithSettings
    cursor_path: Path


def _required(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {key}")
    return value


def _optional(env: Mapping[str, str], key: str, default: str) -> str:
    return (env.get(key) or "").strip() or default


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``env`` (defaults to the process environment)."""
    env = os.environ if env is None else env

    return Settings(
        snowflake=SnowflakeSettings(
            account=_required(env, "SNOWFLAKE_ACCOUNT"),
            user=_required(env, "SNOWFLAKE_USER"),
            warehouse=_required(env, "SNOWFLAKE_WAREHOUSE"),
            database=_optional(env, "SNOWFLAKE_DATABASE", DEFAULT_DATABASE),
            schema=_optional(env, "SNOWFLAKE_SCHEMA", DEFAULT_SCHEMA),
            private_key_path=Path(
                _optional(env, "SNOWFLAKE_PRIVATE_KEY_PATH", DEFAULT_PRIVATE_KEY_PATH)
            ).expanduser(),
            # Unset means the key was generated with -nocrypt; empty string is
            # not a valid passphrase, so treat it as unset too.
            private_key_passphrase=(env.get("SNOWFLAKE_PK_PASSPHRASE") or "") or None,
        ),
        langsmith=LangSmithSettings(
            api_key=_required(env, "LANGSMITH_API_KEY"),
            project=_required(env, "LANGSMITH_PROJECT"),
            otel_endpoint=_optional(env, "LANGSMITH_OTEL_ENDPOINT", DEFAULT_OTEL_ENDPOINT),
        ),
        cursor_path=Path(
            _optional(env, "SNOWFLAKE_OBS_CURSOR_PATH", DEFAULT_CURSOR_PATH)
        ).expanduser(),
    )
