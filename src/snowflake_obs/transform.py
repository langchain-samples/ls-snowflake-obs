"""Pure conversion of Snowflake AI Observability event rows into OTEL span data.

Everything in this module is side-effect free: rows in, `BridgeSpan` values out.
The OTEL emission and Snowflake I/O live in `otel.py` / `snowflake_source.py`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

AttributeValue = str | int | float | bool
Attributes = dict[str, AttributeValue]

TRACE_ID_BITS = 128
SPAN_ID_BITS = 64


@dataclass(frozen=True, slots=True)
class BridgeSpan:
    """A Snowflake span already normalized to what the OTEL SDK needs."""

    trace_id: int
    span_id: int
    parent_span_id: int | None
    name: str
    start_ns: int
    end_ns: int
    is_error: bool
    attributes: Attributes


@dataclass(frozen=True, slots=True)
class Conversion:
    spans: list[BridgeSpan] = field(default_factory=list)
    rewritten_span_ids: int = 0
    skipped_rows: int = 0


# Snowflake → LangSmith attribute name mapping. LangSmith populates its
# Input/Output/Token panels from these canonical keys; we copy values across
# so the original Snowflake-named attributes stay visible too.
LS_ATTR_MAP: dict[str, str] = {
    "ai.observability.record_root.input": "input.value",
    "ai.observability.record_root.output": "output.value",
    "snow.ai.observability.agent.planning.token_count.input": "gen_ai.usage.input_tokens",
    "snow.ai.observability.agent.planning.token_count.output": "gen_ai.usage.output_tokens",
    "snow.ai.observability.agent.planning.token_count.total": "gen_ai.usage.total_tokens",
    "snow.ai.observability.agent.planning.model": "gen_ai.request.model",
}

INPUT_SOURCES = (
    "snow.ai.observability.agent.planning.query",
    "snow.ai.observability.agent.tool.semantic_context.user_question",
    "snow.ai.observability.agent.tool.sql_execution.query",
    "snow.ai.observability.agent.tool.custom_tool.argument.value",
    "snow.ai.observability.agent.tool.chart_generation.input_chart_spec",
    "snow.ai.observability.agent.tool.server_skill.skill_name",
)
OUTPUT_SOURCES = (
    "snow.ai.observability.agent.planning.response",
    "snow.ai.observability.agent.tool.semantic_context.tables",
    "snow.ai.observability.agent.tool.sql_execution.result",
    "snow.ai.observability.agent.tool.custom_tool.results",
    "snow.ai.observability.agent.tool.chart_generation.response",
)

ROOT_INPUT_KEY = "ai.observability.record_root.input"
ROOT_OUTPUT_KEY = "ai.observability.record_root.output"


def hex_to_int(hex_str: str | None, bits: int) -> int:
    """Convert a hex trace/span ID to the int OTEL expects (0 when unusable)."""
    if not hex_str:
        return 0
    digits = bits // 4
    cleaned = hex_str.replace("-", "")[:digits]
    try:
        return int(cleaned.ljust(digits, "0"), 16)
    except ValueError:
        return 0


def parse_timestamp_ns(value: object, *, now_ns: int) -> int:
    """Return nanoseconds since epoch for a Snowflake timestamp value."""
    if value is None:
        return now_ns
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace(" +0000", "+00:00"))
        except ValueError:
            return now_ns
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1e9)


def _scalar(value: object) -> AttributeValue:
    return value if isinstance(value, str | int | float | bool) else str(value)


def flatten_attributes(raw: object) -> Attributes:
    """Normalize RECORD_ATTRIBUTES, which Snowflake returns as a JSON string or dict."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, Mapping):
        return {}

    # Flatten one level of nesting (e.g. {"gen_ai": {"system": "snowflake"}}).
    flat: Attributes = {}
    for key, value in raw.items():
        if isinstance(value, Mapping):
            for inner_key, inner_value in value.items():
                flat[f"{key}.{inner_key}"] = _scalar(inner_value)
        else:
            flat[str(key)] = value if isinstance(value, str | int | float | bool) else str(value)
    return flat


def span_kind_for(name: str) -> str:
    """Map Snowflake span names to LangSmith span kinds for UI rendering."""
    if name.startswith("ReasoningAgentStep"):
        return "llm"
    if "Tool" in name or name.startswith("SqlExecution"):
        return "tool"
    return "chain"


def _mime_type_for(value: object) -> str:
    return "application/json" if str(value).lstrip().startswith(("{", "[")) else "text/plain"


def to_langsmith_attrs(attrs: Attributes, span_name: str, project: str) -> Attributes:
    """Add LangSmith-canonical keys alongside the Snowflake-native ones."""
    out: Attributes = dict(attrs)

    for sf_key, ls_key in LS_ATTR_MAP.items():
        if sf_key in out and ls_key not in out:
            out[ls_key] = out[sf_key]

    if "input.value" not in out:
        for src in INPUT_SOURCES:
            if src in out:
                out["input.value"] = str(out[src])
                break
    if "output.value" not in out:
        for src in OUTPUT_SOURCES:
            if src in out:
                out["output.value"] = str(out[src])
                break

    if "input.value" in out:
        out.setdefault("input.mime_type", "text/plain")
    if "output.value" in out:
        out.setdefault("output.mime_type", _mime_type_for(out["output.value"]))

    # Group spans into LangSmith threads by Snowflake thread_id.
    thread = out.get("snow.ai.observability.agent.thread_id")
    if thread is not None:
        thread_str = str(thread)
        out.setdefault("session.id", thread_str)
        out.setdefault("langsmith.metadata.session_id", thread_str)

    out.setdefault("langsmith.span.kind", span_kind_for(span_name))
    out.setdefault("snowflake.agent_type", "external_agent")
    out.setdefault("langsmith.project", project)
    return out


def trace_io_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, AttributeValue]]:
    """Collect per-trace input/output text from whichever span carries record_root.*.

    A trace's root span carries no I/O of its own, so LangSmith's trace landing
    page shows "No inputs/outputs" unless we hoist this onto it.
    """
    index: dict[str, dict[str, AttributeValue]] = {}
    for row in rows:
        trace_id = row.get("TRACE_ID")
        if not trace_id:
            continue
        attrs = flatten_attributes(row.get("RECORD_ATTRIBUTES"))
        io = index.setdefault(str(trace_id), {})
        if "input" not in io and ROOT_INPUT_KEY in attrs:
            io["input"] = attrs[ROOT_INPUT_KEY]
        if "output" not in io and ROOT_OUTPUT_KEY in attrs:
            io["output"] = attrs[ROOT_OUTPUT_KEY]
    return index


def _rewritten_span_id(raw_span_id: str, row: Mapping[str, Any]) -> str:
    seed = f"{raw_span_id}|{row.get('SPAN_NAME') or ''}|{row.get('START_TIMESTAMP')}"
    return hashlib.sha1(seed.encode(), usedforsecurity=False).hexdigest()[:16]


def is_error_status(raw_status: object) -> bool:
    """Snowflake emits STATUS_CODE_OK / STATUS_CODE_ERROR / STATUS_CODE_UNSET."""
    status = str(raw_status or "").upper().removeprefix("STATUS_CODE_")
    return status not in ("OK", "UNSET", "")


def rows_to_spans(
    rows: Sequence[Mapping[str, Any]],
    project: str,
    *,
    now_ns: int,
) -> Conversion:
    """Convert Snowflake event rows into `BridgeSpan`s, dropping malformed rows."""
    trace_io = trace_io_index(rows)

    # Snowflake sometimes emits sibling spans sharing a span_id within one trace
    # (e.g. ReasoningAgentStepPlanning-0 and SemanticContextTool reusing the same
    # id). OTel requires unique span_ids per trace, and LangSmith dedupes by
    # (trace_id, span_id), so collisions silently drop one of the spans. Track
    # seen pairs and deterministically rewrite the second+ occurrence.
    seen: set[tuple[str, str]] = set()

    spans: list[BridgeSpan] = []
    rewritten = 0
    skipped = 0

    for row in rows:
        raw_trace_id = str(row.get("TRACE_ID") or "")
        raw_span_id = str(row.get("SPAN_ID") or "")

        if raw_span_id and (raw_trace_id, raw_span_id) in seen:
            raw_span_id = _rewritten_span_id(raw_span_id, row)
            rewritten += 1
        seen.add((raw_trace_id, raw_span_id))

        trace_id = hex_to_int(raw_trace_id, TRACE_ID_BITS)
        span_id = hex_to_int(raw_span_id, SPAN_ID_BITS)
        parent_span_id = hex_to_int(str(row.get("PARENT_SPAN_ID") or ""), SPAN_ID_BITS) or None

        if trace_id == 0 or span_id == 0:
            skipped += 1
            continue

        name = str(row.get("SPAN_NAME") or "snowflake.agent.span")
        attributes = to_langsmith_attrs(
            flatten_attributes(row.get("RECORD_ATTRIBUTES")), name, project
        )

        if parent_span_id is None:
            io = trace_io.get(raw_trace_id, {})
            if "input" in io and "input.value" not in attributes:
                attributes["input.value"] = io["input"]
                attributes.setdefault("input.mime_type", "text/plain")
            if "output" in io and "output.value" not in attributes:
                attributes["output.value"] = io["output"]
                attributes.setdefault("output.mime_type", "text/plain")

        spans.append(
            BridgeSpan(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                name=name,
                start_ns=parse_timestamp_ns(row.get("START_TIMESTAMP"), now_ns=now_ns),
                # TIMESTAMP is the event-table end time.
                end_ns=parse_timestamp_ns(row.get("TIMESTAMP"), now_ns=now_ns),
                is_error=is_error_status(row.get("STATUS_CODE")),
                attributes=attributes,
            )
        )

    return Conversion(spans=spans, rewritten_span_ids=rewritten, skipped_rows=skipped)
