"""The polling loop: Snowflake events → converted spans → LangSmith."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from snowflake_obs.config import Settings
from snowflake_obs.cursor import load_cursor, save_cursor
from snowflake_obs.otel import (
    FixedIdGenerator,
    build_exporter,
    build_tracer_provider,
    emit_spans,
)
from snowflake_obs.snowflake_source import SnowflakeSpanSource, SpanSource
from snowflake_obs.transform import rows_to_spans


@dataclass(frozen=True, slots=True)
class BridgeOptions:
    poll_interval: int
    once: bool
    debug: bool = False


def run(
    settings: Settings,
    options: BridgeOptions,
    *,
    source: SpanSource | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll until interrupted (or once). Returns a process exit code."""
    span_source = SnowflakeSpanSource(settings.snowflake) if source is None else source
    exporter = build_exporter(settings.langsmith, debug=options.debug)
    id_generator = FixedIdGenerator()
    provider = build_tracer_provider(exporter, id_generator)

    print(f"[bridge] LangSmith project : {settings.langsmith.project}")
    print(f"[bridge] Poll interval     : {options.poll_interval}s")
    print(f"[bridge] Mode              : {'once' if options.once else 'continuous'}")
    print(f"[bridge] Cursor file       : {settings.cursor_path}")
    print()

    exit_code = 0
    try:
        while True:
            cursor_timestamp = load_cursor(settings.cursor_path)
            print(f"[bridge] Polling events after {cursor_timestamp} ...")

            try:
                rows = span_source.fetch_new_spans(cursor_timestamp)
            except Exception as exc:
                print(f"[bridge] Snowflake query error: {exc}")
                exit_code = 1
                if options.once:
                    break
                sleep(options.poll_interval)
                continue

            if rows:
                conversion = rows_to_spans(rows, settings.langsmith.project, now_ns=time.time_ns())
                emitted = emit_spans(conversion.spans, provider, id_generator)
                latest_timestamp = str(rows[-1]["TIMESTAMP"])
                save_cursor(settings.cursor_path, latest_timestamp)
                notes = "".join(
                    [
                        f" ({conversion.rewritten_span_ids} colliding span_ids rewritten)"
                        if conversion.rewritten_span_ids
                        else "",
                        f" ({conversion.skipped_rows} malformed rows skipped)"
                        if conversion.skipped_rows
                        else "",
                    ]
                )
                print(
                    f"[bridge] Exported {emitted} spans. "
                    f"Cursor advanced to {latest_timestamp}{notes}"
                )
            else:
                print("[bridge] No new spans.")

            if options.once:
                break

            sleep(options.poll_interval)
    except KeyboardInterrupt:
        print("\n[bridge] Interrupted.")
    finally:
        # Flush whatever the BatchSpanProcessor still holds.
        provider.shutdown()

    print("[bridge] Done.")
    return exit_code
