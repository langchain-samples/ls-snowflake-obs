from collections.abc import Sequence
from pathlib import Path
from typing import Any

from snowflake_obs.bridge import BridgeOptions, run
from snowflake_obs.config import Settings, load_settings
from snowflake_obs.cursor import EPOCH_CURSOR, load_cursor

# Rows whose IDs are unusable: the loop still advances the cursor, but nothing is
# handed to the exporter, so these tests never touch the network.
MALFORMED_ROWS: list[dict[str, Any]] = [{"TRACE_ID": None, "TIMESTAMP": "2026-08-04 12:00:00"}]


class FakeSource:
    def __init__(self, batches: list[Sequence[dict[str, Any]] | BaseException]) -> None:
        self.batches = batches
        self.cursors: list[str] = []

    def fetch_new_spans(self, cursor_timestamp: str) -> Sequence[dict[str, Any]]:
        self.cursors.append(cursor_timestamp)
        batch = self.batches.pop(0)
        if isinstance(batch, BaseException):
            raise batch
        return batch


def settings_for(tmp_path: Path) -> Settings:
    return load_settings(
        {
            "SNOWFLAKE_ACCOUNT": "acct",
            "SNOWFLAKE_USER": "user",
            "SNOWFLAKE_WAREHOUSE": "wh",
            "LANGSMITH_API_KEY": "lsv2_dummy",
            "LANGSMITH_PROJECT": "proj",
            "SNOWFLAKE_OBS_CURSOR_PATH": str(tmp_path / "cursor"),
        }
    )


def test_once_with_no_rows_leaves_cursor_untouched(tmp_path: Path) -> None:
    source = FakeSource([[]])
    settings = settings_for(tmp_path)

    assert run(settings, BridgeOptions(poll_interval=1, once=True), source=source) == 0
    assert source.cursors == [EPOCH_CURSOR]
    assert load_cursor(settings.cursor_path) == EPOCH_CURSOR


def test_cursor_advances_to_last_row_timestamp(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    source = FakeSource([MALFORMED_ROWS])

    assert run(settings, BridgeOptions(poll_interval=1, once=True), source=source) == 0
    assert load_cursor(settings.cursor_path) == "2026-08-04 12:00:00"


def test_query_error_in_once_mode_exits_nonzero(tmp_path: Path) -> None:
    source = FakeSource([RuntimeError("boom")])

    assert (
        run(settings_for(tmp_path), BridgeOptions(poll_interval=1, once=True), source=source) == 1
    )


def test_continuous_mode_retries_after_a_query_error(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    source = FakeSource([RuntimeError("boom"), MALFORMED_ROWS, KeyboardInterrupt()])
    slept: list[float] = []

    run(settings, BridgeOptions(poll_interval=7, once=False), source=source, sleep=slept.append)

    assert slept == [7, 7]
    assert source.cursors == [EPOCH_CURSOR, EPOCH_CURSOR, "2026-08-04 12:00:00"]
