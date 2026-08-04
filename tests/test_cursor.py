from pathlib import Path

import pytest

from snowflake_obs.cursor import EPOCH_CURSOR, load_cursor, save_cursor


def test_missing_file_returns_epoch(tmp_path: Path) -> None:
    assert load_cursor(tmp_path / "absent") == EPOCH_CURSOR


def test_empty_file_returns_epoch(tmp_path: Path) -> None:
    path = tmp_path / "cursor"
    path.write_text("   \n")
    assert load_cursor(path) == EPOCH_CURSOR


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cursor"
    save_cursor(path, "2026-08-04 12:00:00.123 +0000")
    assert load_cursor(path) == "2026-08-04 12:00:00.123 +0000"


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "cursor"
    save_cursor(path, "ts")
    assert load_cursor(path) == "ts"


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    path = tmp_path / "cursor"
    save_cursor(path, "a")
    save_cursor(path, "b")
    assert [p.name for p in tmp_path.iterdir()] == ["cursor"]


def test_relative_path_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_cursor(Path(".cursor-state"), "ts")
    assert load_cursor(Path(".cursor-state")) == "ts"
