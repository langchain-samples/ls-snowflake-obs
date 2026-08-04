# snowflake-obs

Bridges Snowflake AI Observability spans into LangSmith.

The bridge polls `SNOWFLAKE.LOCAL.AI_OBSERVABILITY_EVENTS` for `RECORD_TYPE = 'SPAN'`
rows, reconstructs each one as an OpenTelemetry span (preserving the original
trace/span IDs, parent links and timestamps), maps Snowflake's Cortex Agent
attributes onto LangSmith's canonical `input.value` / `output.value` /
`gen_ai.usage.*` keys, and ships them to LangSmith's OTLP/HTTP ingest endpoint.

A cursor file records the last exported event timestamp so restarts don't
re-export spans.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill it in
```

### Snowflake key-pair auth

The bridge authenticates with an RSA key pair, not a password:

```bash
mkdir -p ~/.snowflake
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out ~/.snowflake/rsa_key.p8 -nocrypt
openssl rsa -in ~/.snowflake/rsa_key.p8 -pubout -out ~/.snowflake/rsa_key.pub
chmod 600 ~/.snowflake/rsa_key.p8
```

Register the public key on the Snowflake user (`ALTER USER <user> SET RSA_PUBLIC_KEY='...'`).
If you omit `-nocrypt`, set `SNOWFLAKE_PK_PASSPHRASE`.

## Configuration

All configuration comes from the environment — see `.env.example`. Required:
`SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_WAREHOUSE`, `LANGSMITH_API_KEY`,
`LANGSMITH_PROJECT`. Everything else has a default.

Secrets belong in `.env` (git-ignored) or your process manager's secret store —
never in source.

## Usage

```bash
uv run --env-file .env snowflake-obs                # poll every 30s
uv run --env-file .env snowflake-obs --once         # single poll, then exit
uv run --env-file .env snowflake-obs --interval 60  # poll every 60s
uv run --env-file .env snowflake-obs --debug        # dump rejected batches for inspection
```

`--debug` writes the raw protobuf payload and the LangSmith response of any
non-2xx batch to a private (mode 0700) temp directory, whose path is printed at
startup. Those dumps contain observability payloads — user questions, SQL, model
output — so delete them when you're done.

Exit codes: `0` success, `1` a Snowflake poll failed, `2` bad configuration.

## Layout

| Module | Role |
| --- | --- |
| `config.py` | Environment → frozen `Settings` |
| `snowflake_source.py` | Connection, key loading, events query |
| `transform.py` | Pure row → `BridgeSpan` conversion and attribute mapping |
| `otel.py` | Exporter, tracer provider, ID pinning, span emission |
| `cursor.py` | Atomic cursor-file read/write |
| `bridge.py` | Polling loop |
| `cli.py` | Argument parsing |

## Development

```bash
uv run pytest
uv run ruff check
uv run ruff format
uv run ty check src tests
```

## Known limitations

- The cursor query uses `TIMESTAMP > cursor`, so spans sharing the exact end
  timestamp of the last exported row in a batch are not picked up on the next
  poll.
- The cursor advances as soon as spans are queued. Export happens
  asynchronously in the `BatchSpanProcessor`, so a batch that LangSmith rejects
  is not retried on the next poll.
