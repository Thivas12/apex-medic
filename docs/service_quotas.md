# Request budgets and service capacity

These are initial limits for a small prototype. Configure public hosting and a
data retention/deletion policy before opening an unrestricted service.

## Request budgets

All HTTP requests under `/cases` share the general budget, including failures,
unauthorized attempts and evidence retries. POST routes also use their category
budget. Both the network and global budgets must allow a request.

| Category | Per client network | Whole service |
| --- | --- | --- |
| All case requests | 120 per minute | 600 per minute |
| Case creation attempts | 5 per 10 minutes | 30 per hour |
| Evidence write attempts | 30 per minute | 180 per minute |
| Explanation attempts | 6 per minute | 20 per minute |

An explanation request is counted even if it returns reviewed text or is rejected
by the existing generation lock. These are request budgets, not model token billing.

The limiter runs before body receipt, JSON parsing, ownership lookup and model
generation. It never grants case access. Earlier ownership, origin and body-size
checks still apply to admitted requests. Health and static UI paths are excluded.

Each budget has a fixed window starting at its first admitted request. Bursts near
a window boundary are possible; this is not a rolling-window guarantee. All counters
for a request update together in one SQLite transaction. A denied request does not
consume the other category budgets. Counters survive restarts and are shared by
processes using the same rate database. Multiple replicas with separate databases
would not share them.

Counters live in `data/private/cases.rates.sqlite3`, alongside the case database.
The table has at most 4,096 live counter rows. Expired rows are removed during
request processing. When counter capacity or storage is unavailable, requests
receive 503 instead of bypassing the limit. Do not delete this database to work
around a limit.

## Network identity and proxies

The application uses the ASGI client address, never client-supplied `Forwarded`
or `X-Forwarded-For` headers directly. IPv4 clients share a bucket per address;
IPv6 clients share a /64 bucket, including temporary addresses in that subnet.
IPv4-mapped IPv6 addresses normalize to IPv4. Missing/invalid peer addresses share
one conservative bucket. Browsers on the same public network can share a budget.

Only a hash of the normalized network is stored in the rate table. This is a
pseudonymous identifier, not a guarantee of anonymity. Keep this database private.
Cookies and case IDs are not rate-limiter keys; a new cookie does not reset a budget.
Global budgets still apply across different networks.

The Render Free profile uses a platform-specific ingress adapter before rate
limiting; see [Render deployment](render_deployment.md#hosting-boundary).

For direct local testing, start Uvicorn with `--no-proxy-headers`. Uvicorn can rewrite
the ASGI client address when proxy-header support is enabled. A public reverse proxy
therefore needs an explicit trusted-proxy configuration and an origin server that
untrusted clients cannot reach directly. Do not blindly trust `*`. Hosting-specific
configuration is deliberately deferred until the deployment provider is chosen.

## Storage capacity

| Item | Service limit |
| --- | --- |
| All cases, including imported and legacy cases | 500 |
| Stored browser sessions, including expired sessions | 500 |
| Cases owned by one browser session | 20 |
| Combined case and event JSON payloads | 100 MiB |

The byte budget counts UTF-8 bytes of every `cases.payload` and
`case_events.payload`, including the duplicate evidence retained for history.
It is not a filesystem or physical SQLite-file quota: table/index overhead,
metadata, journals, rate counters, backups, logs and model files are excluded.
The previous 1 MiB per-case limit also remains active.

Startup adds a `storage_totals` row, accounting triggers and a session-owner index.
It backfills totals from existing cases/history, including legacy data, without
rewriting evidence or deleting records. Triggers keep totals synchronized on
insert/update/delete, and startup reconciles totals again. New-write quota checks
and their writes use the same `BEGIN IMMEDIATE` transaction; usage counters roll
back with a failed case/history update. CLI imports obey case and byte capacity too.

The browser-session checks happen before creating a session or its case, so quota
failures create no orphan session. New cookies cannot bypass global case, session
or payload capacity. Reaching the global case count still allows evidence additions
to existing cases, subject to their own limits and the global byte budget.

Existing over-quota records remain readable by their authorized owners or the
trusted local CLI. Expired sessions and cases are not automatically deleted. Capacity
does not free itself merely because a browser cookie expires. Owners can remove a
case at `/ui/manage.html`; deleting their last case also removes the unused session
row and frees a session slot. See `case_deletion.md`. Automated retention and backup
purging remain future work. Do not silently discard player evidence to make room.

## Responses and retries

- 429 plus `Retry-After`: a request budget is exhausted. The JSON `detail` also
  states the wait in seconds. Retrying immediately keeps failing.
- 503 plus `Retry-After`: the request counter store is unavailable or full.
- 503 without `Retry-After`: service case/session/byte capacity is full; operator
  action is required, so no automatic recovery time is promised.
- 409: the browser session already owns 20 cases; reopen a saved case.

All these responses use `Cache-Control: no-store`. Existing 409 evidence conflicts
and validation/body errors keep their earlier meanings. Ownership still applies.
Replaying a saved event after a request window recovers returns `added: false`.
Exact retries also bypass new storage allocation checks, but count as HTTP attempts.
Case creation does not gain idempotency in this patch.

## Local verification and deployment boundary

`tests/test_service_quotas.py` adds 27 tests for persistence, counter expiry,
network/global budgets, concurrent admission, bounded counter tables, transaction
rollback, UTF-8 accounting, legacy reconciliation, quota responses and safe retries.
Tests use temporary databases and a mocked explanation provider.

The 27 new tests and 50 ownership/resource-limit/reviewed-explanation tests passed
in the isolated check harness. Its policy, knowledge and model modules are substitutes
for the full project. Run the full Windows project suite before committing.

The existing model concurrency lock is process-local. Keep one Uvicorn worker for
this local Ollama prototype. A reverse proxy still needs connection/concurrency
limits: an application limiter is not a complete defense against traffic saturation.
SQLite rate limiting is intended for this small single-host application, not a
claim of horizontally scalable infrastructure.

References:

- https://uvicorn.dev/settings/ — proxy-header and trusted-peer settings.
- https://www.sqlite.org/lang_transaction.html — immediate write transactions.
- https://www.starlette.io/middleware/ — ASGI middleware composition.
