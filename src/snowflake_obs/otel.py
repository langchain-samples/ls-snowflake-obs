"""OTEL exporter wiring and emission of reconstructed spans."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import NonRecordingSpan, SpanKind, StatusCode, TraceFlags
from opentelemetry.trace.span import SpanContext

from snowflake_obs.config import LangSmithSettings
from snowflake_obs.transform import BridgeSpan

SERVICE_NAME = "snowflake-cortex-agent"
TRACER_NAME = "snowflake-bridge"


class FixedIdGenerator(IdGenerator):
    """OTel IdGenerator that lets us pin the next trace_id/span_id explicitly.

    The OTel SDK ignores SpanContext fields passed to start_span(); the only
    public hook for supplying the integers it uses is the TracerProvider's
    id_generator. We queue the IDs from each Snowflake row immediately before
    calling start_span() so the exported span ends up with our IDs, which lets
    LangSmith link parent → child by matching child.parent_span_id against the
    real span_id of the parent it already received.
    """

    def __init__(self) -> None:
        self._next_span_id: int | None = None
        self._next_trace_id: int | None = None
        self._fallback = RandomIdGenerator()

    def set_next(self, span_id: int, trace_id: int) -> None:
        self._next_span_id = span_id
        self._next_trace_id = trace_id

    def generate_span_id(self) -> int:
        queued, self._next_span_id = self._next_span_id, None
        return queued or self._fallback.generate_span_id()

    def generate_trace_id(self) -> int:
        queued, self._next_trace_id = self._next_trace_id, None
        return queued or self._fallback.generate_trace_id()


def build_exporter(settings: LangSmithSettings, *, debug: bool = False) -> OTLPSpanExporter:
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_endpoint,
        headers={
            "x-api-key": settings.api_key,
            # LangSmith uses this header to route to the right project.
            "langsmith-project": settings.project,
        },
    )
    if debug:
        install_debug_hook(exporter)
    return exporter


def install_debug_hook(exporter: OTLPSpanExporter) -> None:
    """Wrap the exporter's HTTP session so failed batches dump payload + response.

    The OTLP exporter swallows response details and returns only SUCCESS/FAILURE,
    so when LangSmith rejects with 422 we can't see why. Dumps land in a
    per-run 0700 directory: the payloads contain observability data (user
    questions, SQL, model output) and must not be world-readable in /tmp.
    """
    dump_dir = Path(tempfile.mkdtemp(prefix="snowflake-obs-debug-"))
    print(f"[bridge:debug] failed batches will be dumped to {dump_dir}")
    original_post = exporter._session.post

    def debug_post(url: str, data: Any = None, json: Any = None, **kwargs: Any) -> Any:
        size = len(data) if data else 0
        print(f"[bridge:debug] POST {url} payload={size:,} bytes")
        response = original_post(url, data=data, json=json, **kwargs)
        if not 200 <= response.status_code < 300:
            stamp = int(time.time() * 1000)
            payload_path = dump_dir / f"failed_batch_{stamp}.bin"
            body_path = dump_dir / f"failed_response_{stamp}.txt"
            payload_path.write_bytes(data or b"")
            body_path.write_text(
                f"status: {response.status_code}\n"
                f"headers: {dict(response.headers)}\n"
                f"body:\n{response.text}\n",
                encoding="utf-8",
            )
            print(
                f"[bridge:debug] FAIL status={response.status_code} "
                f"body_preview={response.text[:300]!r} "
                f"dumped payload->{payload_path} response->{body_path}"
            )
        return response

    exporter._session.post = debug_post  # ty: ignore[invalid-assignment]


def build_tracer_provider(
    exporter: OTLPSpanExporter,
    id_generator: IdGenerator,
    max_export_batch_size: int = 50,
) -> TracerProvider:
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource, id_generator=id_generator)
    provider.add_span_processor(
        BatchSpanProcessor(exporter, max_export_batch_size=max_export_batch_size)
    )
    return provider


def emit_spans(
    spans: Iterable[BridgeSpan],
    tracer_provider: TracerProvider,
    id_generator: FixedIdGenerator,
) -> int:
    """Replay each converted span through the SDK. Returns the number emitted."""
    tracer = tracer_provider.get_tracer(TRACER_NAME)
    emitted = 0

    for bridge_span in spans:
        # Synthesize the parent so the new span inherits trace_id and points
        # parent_span_id at the right place.
        context = None
        if bridge_span.parent_span_id is not None:
            parent_context = SpanContext(
                trace_id=bridge_span.trace_id,
                span_id=bridge_span.parent_span_id,
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            context = otel_trace.set_span_in_context(NonRecordingSpan(parent_context))

        # Queue our Snowflake IDs so OTel uses them instead of random ones. Root
        # spans consume the trace_id; children inherit it from the parent context.
        id_generator.set_next(bridge_span.span_id, bridge_span.trace_id)

        # Reconstructing historical spans — use start_span (no context manager)
        # so we can call end() exactly once with the original end timestamp.
        span = tracer.start_span(
            name=bridge_span.name,
            context=context,
            kind=SpanKind.INTERNAL,
            attributes=bridge_span.attributes,
            start_time=bridge_span.start_ns,
            record_exception=False,
            set_status_on_exception=False,
        )
        try:
            span.set_status(StatusCode.ERROR if bridge_span.is_error else StatusCode.OK, "")
        finally:
            span.end(end_time=bridge_span.end_ns)

        emitted += 1

    return emitted
