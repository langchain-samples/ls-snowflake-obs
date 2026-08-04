from datetime import UTC, datetime

import pytest

from snowflake_obs.transform import (
    SPAN_ID_BITS,
    TRACE_ID_BITS,
    Attributes,
    flatten_attributes,
    hex_to_int,
    is_error_status,
    parse_timestamp_ns,
    rows_to_spans,
    span_kind_for,
    to_langsmith_attrs,
    trace_io_index,
)

NOW_NS = 1_700_000_000_000_000_000
PROJECT = "test-project"


def row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "TRACE_ID": "a" * 32,
        "SPAN_ID": "b" * 16,
        "PARENT_SPAN_ID": None,
        "RECORD_TYPE": "SPAN",
        "SPAN_NAME": "Agent",
        "START_TIMESTAMP": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
        "TIMESTAMP": datetime(2026, 8, 4, 12, 0, 1, tzinfo=UTC),
        "STATUS_CODE": "STATUS_CODE_OK",
        "RECORD_ATTRIBUTES": {},
    }
    return base | overrides


class TestHexToInt:
    def test_truncates_and_pads_to_width(self) -> None:
        assert hex_to_int("ff", SPAN_ID_BITS) == 0xFF00000000000000
        assert hex_to_int("a" * 40, TRACE_ID_BITS) == int("a" * 32, 16)

    @pytest.mark.parametrize("value", [None, "", "not-hex"])
    def test_unusable_input_is_zero(self, value: str | None) -> None:
        assert hex_to_int(value, SPAN_ID_BITS) == 0

    def test_strips_dashes(self) -> None:
        assert hex_to_int("aaaa-bbbb-cccc-dddd", SPAN_ID_BITS) == 0xAAAABBBBCCCCDDDD


class TestParseTimestamp:
    def test_naive_datetime_is_treated_as_utc(self) -> None:
        naive = datetime(2026, 8, 4, 12, 0, 0)
        aware = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
        assert parse_timestamp_ns(naive, now_ns=NOW_NS) == parse_timestamp_ns(aware, now_ns=NOW_NS)

    def test_snowflake_string_offset_format(self) -> None:
        expected = int(datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC).timestamp() * 1e9)
        assert parse_timestamp_ns("2026-08-04 12:00:00.000 +0000", now_ns=NOW_NS) == expected

    @pytest.mark.parametrize("value", [None, "garbage"])
    def test_falls_back_to_now(self, value: str | None) -> None:
        assert parse_timestamp_ns(value, now_ns=NOW_NS) == NOW_NS


class TestFlattenAttributes:
    def test_parses_json_string(self) -> None:
        assert flatten_attributes('{"a": 1}') == {"a": 1}

    def test_flattens_one_level(self) -> None:
        assert flatten_attributes({"gen_ai": {"system": "snowflake"}}) == {
            "gen_ai.system": "snowflake"
        }

    def test_non_scalar_leaves_are_stringified(self) -> None:
        assert flatten_attributes({"a": [1, 2]}) == {"a": "[1, 2]"}
        assert flatten_attributes({"outer": {"inner": [1]}}) == {"outer.inner": "[1]"}

    @pytest.mark.parametrize("value", [None, "not json", 42, []])
    def test_unusable_input_is_empty(self, value: object) -> None:
        assert flatten_attributes(value) == {}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ReasoningAgentStepPlanning-0", "llm"),
        ("SemanticContextTool", "tool"),
        ("SqlExecution", "tool"),
        ("Agent", "chain"),
        ("Whatever", "chain"),
    ],
)
def test_span_kind_for(name: str, expected: str) -> None:
    assert span_kind_for(name) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("STATUS_CODE_OK", False),
        ("STATUS_CODE_UNSET", False),
        (None, False),
        ("", False),
        ("STATUS_CODE_ERROR", True),
        ("error", True),
    ],
)
def test_is_error_status(status: str | None, expected: bool) -> None:
    assert is_error_status(status) is expected


class TestToLangsmithAttrs:
    def test_maps_canonical_keys_and_keeps_originals(self) -> None:
        attrs: Attributes = {
            "ai.observability.record_root.input": "hi",
            "snow.ai.observability.agent.planning.token_count.total": 12,
        }
        out = to_langsmith_attrs(attrs, "Agent", PROJECT)
        assert out["input.value"] == "hi"
        assert out["ai.observability.record_root.input"] == "hi"
        assert out["gen_ai.usage.total_tokens"] == 12

    def test_falls_back_to_span_specific_input_sources(self) -> None:
        out = to_langsmith_attrs(
            {"snow.ai.observability.agent.tool.sql_execution.query": "SELECT 1"},
            "SqlExecutionTool",
            PROJECT,
        )
        assert out["input.value"] == "SELECT 1"
        assert out["input.mime_type"] == "text/plain"

    def test_json_shaped_output_gets_json_mime_type(self) -> None:
        out = to_langsmith_attrs(
            {"snow.ai.observability.agent.tool.custom_tool.results": ' [{"a": 1}]'},
            "CustomTool",
            PROJECT,
        )
        assert out["output.mime_type"] == "application/json"

    def test_thread_id_becomes_session_id(self) -> None:
        out = to_langsmith_attrs({"snow.ai.observability.agent.thread_id": 99}, "Agent", PROJECT)
        assert out["session.id"] == "99"
        assert out["langsmith.metadata.session_id"] == "99"

    def test_project_and_kind_defaults(self) -> None:
        out = to_langsmith_attrs({}, "ReasoningAgentStep", PROJECT)
        assert out["langsmith.span.kind"] == "llm"
        assert out["langsmith.project"] == PROJECT
        assert out["snowflake.agent_type"] == "external_agent"

    def test_does_not_mutate_input(self) -> None:
        attrs: Attributes = {"ai.observability.record_root.input": "hi"}
        to_langsmith_attrs(attrs, "Agent", PROJECT)
        assert attrs == {"ai.observability.record_root.input": "hi"}


def test_trace_io_index_collects_first_record_root_per_trace() -> None:
    rows = [
        row(RECORD_ATTRIBUTES={}),
        row(
            SPAN_ID="c" * 16,
            RECORD_ATTRIBUTES={
                "ai.observability.record_root.input": "q",
                "ai.observability.record_root.output": "a",
            },
        ),
        row(SPAN_ID="d" * 16, RECORD_ATTRIBUTES={"ai.observability.record_root.input": "later"}),
    ]
    assert trace_io_index(rows) == {"a" * 32: {"input": "q", "output": "a"}}


class TestRowsToSpans:
    def test_parent_and_status_are_carried_over(self) -> None:
        rows = [row(PARENT_SPAN_ID="e" * 16, STATUS_CODE="STATUS_CODE_ERROR")]
        (span,) = rows_to_spans(rows, PROJECT, now_ns=NOW_NS).spans
        assert span.parent_span_id == int("e" * 16, 16)
        assert span.is_error is True
        assert span.start_ns < span.end_ns

    def test_root_span_inherits_trace_level_io(self) -> None:
        rows = [
            row(),
            row(
                SPAN_ID="c" * 16,
                PARENT_SPAN_ID="b" * 16,
                RECORD_ATTRIBUTES={
                    "ai.observability.record_root.input": "q",
                    "ai.observability.record_root.output": "a",
                },
            ),
        ]
        root, child = rows_to_spans(rows, PROJECT, now_ns=NOW_NS).spans
        assert root.parent_span_id is None
        assert root.attributes["input.value"] == "q"
        assert root.attributes["output.value"] == "a"
        assert child.attributes["input.value"] == "q"

    def test_colliding_sibling_span_ids_are_rewritten_deterministically(self) -> None:
        rows = [row(SPAN_NAME="First"), row(SPAN_NAME="Second")]
        result = rows_to_spans(rows, PROJECT, now_ns=NOW_NS)
        first, second = result.spans
        assert result.rewritten_span_ids == 1
        assert first.span_id != second.span_id
        assert rows_to_spans(rows, PROJECT, now_ns=NOW_NS).spans[1].span_id == second.span_id

    def test_malformed_rows_are_skipped(self) -> None:
        rows = [row(TRACE_ID=None), row(SPAN_ID=""), row()]
        result = rows_to_spans(rows, PROJECT, now_ns=NOW_NS)
        assert result.skipped_rows == 2
        assert len(result.spans) == 1

    def test_missing_name_gets_a_default(self) -> None:
        (span,) = rows_to_spans([row(SPAN_NAME=None)], PROJECT, now_ns=NOW_NS).spans
        assert span.name == "snowflake.agent.span"
