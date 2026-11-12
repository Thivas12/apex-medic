# Anonymous case ownership

This milestone associates cases created through the API with a browser session.
Knowing a case ID alone does not grant access. The server checks ownership for
case reads, next steps, history, evidence updates and explanation requests.

## Storage and migration

- `visitor_sessions` stores SHA-256 hashes of random 256-bit bearer tokens,
  creation times and server-enforced expiry times. Raw tokens are not stored.
- `case_owners` associates a case with its session hash.
- Session issuance, case creation and ownership assignment use one SQLite
  transaction. A failure rolls back all three.
- Startup adds the two tables idempotently. Existing cases and evidence history
  are preserved and are not assigned to the first visitor who requests them.
- Existing unowned cases, including CLI imports, remain accessible through the
  local CLI and are unavailable through the web API. There is no public claim
  endpoint. Create new synthetic cases in the UI to test browser ownership.

## Browser behavior

A successful POST /cases issues or reuses an HttpOnly, SameSite=Strict cookie.
Sessions have a fixed 30-day lifetime from issuance; creating another case does
not extend it. Clearing cookies, switching browsers or reaching expiry loses
web access to cases tied to that session. Recovery is not implemented in this
milestone. Stored cases are not deleted when a session expires.

Local development uses http://127.0.0.1:8000 and the apexmedic_session cookie.
HTTPS uses a Secure, host-only, Path=/ cookie named __Host-apexmedic_session.
The frontend and API share one origin; no cross-origin CORS access is enabled.

## Request protection

All API mutations require application/json. When Origin is supplied, it must
exactly match APEX_PUBLIC_ORIGIN. Cross-site Fetch Metadata requests are also
rejected. JSON-only requests prevent ordinary cross-site form submissions;
the API must not later accept form/text content types without adding suitable
CSRF protection. Non-browser clients may omit Origin, but protected case
requests still require the owning session cookie. SameSite is additional
protection, not the only check.

Missing, forged, expired and other-owner cookies receive the same 404 response
for case resources. Owner fields supplied in case-creation JSON are rejected.
Case responses use Cache-Control: no-store.
The low-level repository and CLI remain trusted local administrative interfaces.

## Configuration

APEX_PUBLIC_ORIGIN defaults to http://127.0.0.1:8000. Use that exact origin in
the browser. For an HTTPS deployment, configure the actual public origin, for
example https://apex.example. Non-loopback HTTP origins are rejected.

## Validation

tests/test_case_ownership.py adds 20 tests covering ownership on all current
case endpoints, rejected writes, model-call prevention, cookie handling,
expiry, restart behavior, legacy data preservation, rollback, concurrent
creation, cross-site requests and origin configuration.

The existing app-restart test must preserve the original client's cookies.
A new client without those cookies is a different visitor and should get 404.

This is an access-control milestone, not deployment completion. Hosting,
resource limits, retention/recovery, and broader model evaluation remain
separate work. The reviewed-report behavior and Ollama prompt are unchanged.
