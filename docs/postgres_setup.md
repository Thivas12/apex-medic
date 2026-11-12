# PostgreSQL and Neon setup

ApexMedic supports persistent PostgreSQL storage alongside local SQLite. This
guide covers configuration, explicit migrations and disposable integration
tests. No database credentials belong in source code or browser JavaScript.

## Configuration

The API and admin CLI select PostgreSQL when `DATABASE_URL` is present. They read
the project's `.env.local` automatically, regardless of the terminal's current
directory. Process environment variables take precedence. Variable interpolation
is disabled so a password containing `$` is preserved literally.

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon pooled PostgreSQL URL for normal application traffic |
| `DATABASE_URL_UNPOOLED` | Matching direct URL, used only by the migration command |
| `APEX_DATABASE_BACKEND` | `auto` (default), `postgres` (require it), or `sqlite` |

Remote PostgreSQL URLs require `sslmode=require`, `verify-ca`, or `verify-full`.
Use the TLS settings supplied by Neon. Missing or invalid PostgreSQL settings
fail explicitly; a connection failure never silently switches to SQLite.

## Run on your Windows PC

Stop the running Uvicorn process with Ctrl+C, then run these blocks from the
project directory. Your existing ignored `.env.local` stays in place.

```powershell
# Run from the root of your apex-medic checkout.

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

.\.venv\Scripts\python.exe -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "Fix the test failure before continuing." }

node --test tests/test_manage_page.cjs
if ($LASTEXITCODE -ne 0) { throw "Fix the browser-page test failure before continuing." }
```

The PostgreSQL integration tests are skipped on an ordinary PC test run unless
a separate disposable local test database is explicitly selected. The existing
tests use isolated SQLite files, even with a valid `.env.local` present.

Check connectivity without displaying credentials:

```powershell
.\.venv\Scripts\python.exe -m backend.app.db.manage check
if ($LASTEXITCODE -ne 0) { throw "The database check failed." }
```

Expected first-time output is `PostgreSQL connection: OK` followed by a missing
schema notice. Now initialize the `apexmedic` schema explicitly:

```powershell
.\.venv\Scripts\python.exe -m backend.app.db.manage migrate
if ($LASTEXITCODE -ne 0) { throw "The migration failed." }

.\.venv\Scripts\python.exe -m backend.app.db.manage check
if ($LASTEXITCODE -ne 0) { throw "The database check failed." }
```

Expected final output: `PostgreSQL connection: OK` and `Schema: current`.
Running the migration again is safe: it verifies the version and checksum and
preserves existing data. It rejects an unknown existing `apexmedic` schema.
Application startup checks the schema and never applies migrations itself.

Start the local application:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend --no-proxy-headers
```

Open `http://127.0.0.1:8000/ui/`, create a **new synthetic case**, and save one
reading. Keep its case ID. Restart Uvicorn, reopen that case in the **same browser**,
and verify the reading and history remain available. Clearing the session cookie
removes browser access; knowing a case ID alone does not grant ownership.

Old SQLite case IDs such as `dev_002` are not copied to Neon. They remain intact
in `data/private/cases.sqlite3`. To inspect them with the admin CLI:

```powershell
.\.venv\Scripts\python.exe -m backend.app.cli --sqlite show dev_002
```

The CLI is an administrator tool. It bypasses browser ownership, as before, and
imports unowned cases. Create browser-owned cases through the UI.

## If a check fails

- **PostgreSQL is not selected:** the project `.env.local` must contain the Neon
  variables, or they must be set in the process environment.
- **Direct URL required:** obtain `DATABASE_URL_UNPOOLED` from the same Neon
  branch/database/role as `DATABASE_URL`. Do not substitute the pooler URL.
- **Storage unavailable:** check the Neon branch is available, the credentials
  are current, and your network permits PostgreSQL. The error deliberately omits
  driver details and secrets.
- **Schema mismatch:** use the matching application version and migration file;
  do not delete tables or edit migration checksums to suppress the error.

Do not paste `.env.local`, connection URLs, session cookies, or passwords into
chat, issues, logs, screenshots, or Git commits. Both `.env` and `.env.*` are
ignored. Neon CLI/config files remain separate from this Python integration.

## Transaction behavior

All case, session, evidence, and deletion writes take one transaction-scoped
advisory lock before checking quotas or reading mutable state. This serializes
short writes across processes, matching the original SQLite semantics. Request
counters use a separate transaction lock. The migration takes both in a fixed
order. Readers use a repeatable, read-only snapshot.

Every operation closes its connection. Automatic prepared statements are disabled;
SQL uses schema-qualified names and parameter binding. No session-level advisory
locks or persistent search-path settings are used with the Neon pooler.

Case and event payloads remain canonical JSON text so exact retries retain their
original comparison semantics. SQL triggers account for UTF-8 payload bytes in
the same transaction. Failed writes and failed deletion roll back case state,
history, ownership and accounting together. The existing capacity and request
limits apply to PostgreSQL too.

Connection attempts, individual statements, lock waits and idle transactions
have finite timeouts. Database failures become a generic 503 for web requests;
request-budget failures never bypass rate limiting.

## Developer integration tests

Use a disposable local PostgreSQL database with a name starting with
`apexmedic_test_`, then set `APEX_TEST_POSTGRES_URL` to its direct local URL and run
`python -m unittest discover -s tests`. These tests **drop the `apexmedic` schema**
in that dedicated database before each test. They never use `.env.local`,
`DATABASE_URL`, or `DATABASE_URL_UNPOOLED`. Do not point them at production.

Covered behavior includes the full HTTP workflow, cross-browser denial, restart
persistence, exact retries, competing updates/answers, rollback after a history
failure, global/session quotas, UTF-8 byte accounting, stale deletion, deletion
rollback, and shared concurrent rate budgets.

## Cloud deployment

The hosted API needs `APEX_DATABASE_BACKEND=postgres`, `DATABASE_URL`, and its
exact HTTPS `APEX_PUBLIC_ORIGIN`. Store these in the host's environment settings.
The direct migration URL can stay on the operator's PC. The Python app does not
need Node or the Neon CLI at runtime.

Follow [Render deployment](render_deployment.md) for the hosted startup command,
trusted proxy handling and cloud model settings. A hosted service cannot reach
your laptop through the local Ollama address.
