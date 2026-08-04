from pathlib import Path

import pytest

from snowflake_obs.config import (
    DEFAULT_DATABASE,
    DEFAULT_OTEL_ENDPOINT,
    DEFAULT_SCHEMA,
    ConfigError,
    load_settings,
)

MINIMAL_ENV = {
    "SNOWFLAKE_ACCOUNT": "xy12345.us-east-1",
    "SNOWFLAKE_USER": "svc_bridge",
    "SNOWFLAKE_WAREHOUSE": "WH",
    "LANGSMITH_API_KEY": "lsv2_dummy",
    "LANGSMITH_PROJECT": "cortex",
}


def test_defaults_are_applied() -> None:
    settings = load_settings(MINIMAL_ENV)
    assert settings.snowflake.database == DEFAULT_DATABASE
    assert settings.snowflake.schema == DEFAULT_SCHEMA
    assert settings.langsmith.otel_endpoint == DEFAULT_OTEL_ENDPOINT
    assert settings.snowflake.private_key_passphrase is None


def test_private_key_path_is_expanded() -> None:
    settings = load_settings(MINIMAL_ENV | {"SNOWFLAKE_PRIVATE_KEY_PATH": "~/keys/rsa.p8"})
    assert settings.snowflake.private_key_path == Path.home() / "keys/rsa.p8"


def test_empty_passphrase_is_treated_as_unset() -> None:
    settings = load_settings(MINIMAL_ENV | {"SNOWFLAKE_PK_PASSPHRASE": ""})
    assert settings.snowflake.private_key_passphrase is None


@pytest.mark.parametrize("key", sorted(MINIMAL_ENV))
def test_missing_required_value_raises(key: str) -> None:
    env = MINIMAL_ENV | {key: "  "}
    with pytest.raises(ConfigError, match=key):
        load_settings(env)


def test_secrets_are_kept_out_of_repr() -> None:
    settings = load_settings(MINIMAL_ENV | {"SNOWFLAKE_PK_PASSPHRASE": "s3cret-pass"})
    rendered = repr(settings)
    assert "lsv2_dummy" not in rendered
    assert "s3cret-pass" not in rendered
    assert "cortex" in rendered
