# Request and case limits

This step bounds individual HTTP requests and newly saved cases. Request-rate
limits, total case/session quotas, storage retention and public hosting controls
are separate work. This patch alone does not make the prototype ready for an
unrestricted public deployment.

## Limits

| Item | Maximum |
| --- | --- |
| HTTP request body | 65,536 bytes (64 KiB) |
| Time to receive an HTTP body | 15 seconds |
| Player description | 4,000 characters |
| Data centre name | 200 characters |
| Onset description | 500 characters |
| Observation context | 2,000 characters |
| Test ID | 100 characters |
| Test conditions | 2,000 characters |
| Test notes | 4,000 characters |
| Confirmed cause on a trusted local import | 4,000 characters |
| Observations per case | 100 |
| Test results per case | 100 |
| Serialized case payload | 1,048,576 UTF-8 bytes (1 MiB) |

Text limits count Python characters after existing model whitespace normalization.
Byte limits count bytes, including JSON syntax. Numeric validation, including the
0–100 packet-loss range and rejection of non-finite values, remains in Case models.
The confirmed-cause limit does not add an HTTP diagnosis or case-closing feature.

## Enforcement

`RequestBodyLimit` checks a declared Content-Length when present and also counts
actual received bytes. Missing or understated Content-Length cannot bypass the
body cap. The middleware buffers at most the allowed body before handing it to
FastAPI, so rejected bodies never reach JSON parsing, database writes or model
generation. Buffering is bounded per request, not across all simultaneous clients.
Compressed request bodies are rejected; the UI sends ordinary JSON.

New field and case limits are checked at the storage boundary. Both CLI imports
and browser case creation use the same checks. Existing schemas remain readable.
OpenAPI still describes the persistent evidence models; the additional write-only
text and capacity limits are listed here and enforced by storage.

Evidence writes check the event ID before new-write limits. An identical replay
returns `added: false`, even when the case is full. A changed payload with the same
event ID is still a conflict. The HTTP body limit always applies, including retries.

Capacity checks and evidence/history changes share the existing `BEGIN IMMEDIATE`
transaction. Two writers competing for the last slot cannot both succeed. A failed
write leaves both saved case and event history unchanged.

## Existing data

No database migration or data cleanup is performed. Existing cases can be read
even if they exceed a new limit. New additions must leave the case within count
and byte limits; cases already beyond those limits therefore reject additions.
Legacy text that is longer than a new field limit is not rewritten or truncated.
Only new entries are field-checked during an append. New imports must satisfy all
limits. Never delete evidence to make room automatically.

## HTTP responses

| Status | Meaning |
| --- | --- |
| 400 | Invalid or duplicate Content-Length |
| 408 | Body receipt deadline exceeded |
| 409 | Case capacity exceeded, or an existing evidence conflict |
| 413 | HTTP body exceeds 64 KiB |
| 415 | Compressed body, or an existing unsupported content-type rejection |
| 422 | New field too long, or an existing request validation error |

Errors contain a `detail` value and use `Cache-Control: no-store`. Successful retries
still return 200 with `added: false`. No response claims the failed evidence was saved.

## Verification

`tests/test_resource_limits.py` adds 23 tests for boundaries, UTF-8 byte counting,
streamed and falsely sized requests, receive deadlines, rollback, concurrent writes,
legacy reads, exact retries, and HTTP behavior. Temporary databases are used.

The patch was checked with those 23 tests, the 20 ownership tests and 7 reviewed
explanation tests in an isolated harness (Python 3.12, FastAPI 0.141.1, Pydantic 2.13.4,
Starlette 1.6.0). The harness uses substitute policy/knowledge/model components;
it is not a full checkout of the local project. Run the complete suite on the PC.

References:

- https://www.starlette.io/middleware/ — pure ASGI middleware and receive handling.
- https://sqlite.org/lang_transaction.html — immediate write transactions.
