"""Snowflake connection and the AI Observability events query."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import snowflake.connector
from cryptography.hazmat.primitives import serialization
from snowflake.connector.connection import SnowflakeConnection

from snowflake_obs.config import SnowflakeSettings

EVENTS_QUERY = """
SELECT
    TIMESTAMP,
    TRACE['trace_id']::STRING            AS trace_id,
    TRACE['span_id']::STRING             AS span_id,
    RECORD['parent_span_id']::STRING     AS parent_span_id,
    RECORD_TYPE,
    RECORD['name']::STRING               AS span_name,
    START_TIMESTAMP,
    RECORD['status']['code']::STRING     AS status_code,
    RECORD_ATTRIBUTES,
    RESOURCE_ATTRIBUTES
FROM
    SNOWFLAKE.LOCAL.AI_OBSERVABILITY_EVENTS
WHERE
    RECORD_TYPE = 'SPAN'
    AND TIMESTAMP > %(cursor)s
ORDER BY
    TIMESTAMP ASC
"""


class SpanSource(Protocol):
    """What the bridge loop needs from Snowflake; lets tests inject a fake."""

    def fetch_new_spans(self, cursor_timestamp: str) -> Sequence[dict[str, Any]]: ...


def load_private_key_der(settings: SnowflakeSettings) -> bytes:
    """Load the PKCS#8 .p8 key and return DER bytes for the Snowflake connector."""
    with settings.private_key_path.open("rb") as f:
        key = serialization.load_pem_private_key(
            f.read(),
            password=(
                settings.private_key_passphrase.encode()
                if settings.private_key_passphrase
                else None
            ),
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class SnowflakeSpanSource:
    """Opens a short-lived connection per poll."""

    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings

    def _connect(self) -> SnowflakeConnection:
        s = self._settings
        return snowflake.connector.connect(
            account=s.account,
            user=s.user,
            private_key=load_private_key_der(s),
            database=s.database,
            schema=s.schema,
            warehouse=s.warehouse,
        )

    def fetch_new_spans(self, cursor_timestamp: str) -> Sequence[dict[str, Any]]:
        # Bind parameter, never string interpolation: the cursor comes off disk.
        connection = self._connect()
        try:
            cursor = connection.cursor(snowflake.connector.DictCursor)
            cursor.execute(EVENTS_QUERY, {"cursor": cursor_timestamp})
            rows: Sequence[dict[str, Any]] = cursor.fetchall()
            return rows
        finally:
            connection.close()
