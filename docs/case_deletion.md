# Case deletion

Players can remove a case they own using `/ui/manage.html`. The main page gains a
"Manage or delete a saved case" link. The management page is independent of the
existing troubleshooting JavaScript and uses the same browser cookie.

## User flow

1. Enter a case ID and select **Review case**.
2. Review its description, origin, status, observation count, test-result count
   and history count. History may include classification answers, so these counts
   are not expected to be identical.
3. Type the full case ID. **Delete this case** becomes available only on an exact
   match. Cancel discards the preview and performs no deletion.
4. Submit deletion. A successful response removes the preview and confirmation.

Editing the case ID cancels an outstanding lookup. A late response cannot populate
the new selection. Case descriptions use `textContent`, never HTML insertion.
Inputs are disabled during deletion to prevent duplicate submissions or changing
the selected case. A failed or uncertain response requires a fresh lookup. The
interface never treats a network error as proof that deletion failed or succeeded.

## API

| Method and route | Purpose |
| --- | --- |
| `GET /cases/{case_id}/deletion-preview` | Owner-only snapshot and deletion token |
| `DELETE /cases/{case_id}` | Delete after full-ID and snapshot confirmation |

The DELETE request requires `application/json`:

```json
{
  "confirm_case_id": "the-full-case-id",
  "deletion_token": "the-64-character-token-from-the-preview"
}
```

A successful response is 200 with `case_id` and `deleted: true`. A mismatched ID
returns 422, a changed snapshot returns 409, and unavailable/foreign/deleted cases
return the same 404. Existing request-rate, body-size, origin and ownership checks
apply. A full storage quota does not prevent deletion. Rate limits still apply.

The token hashes the stored case payload and the complete ordered event history.
It is a change detector, not authorization. Anyone lacking the owning active
browser session is denied even if they know the token. No cookie is accepted from
the JSON body. Both routes use `Cache-Control: no-store`.

## Atomic deletion and quota accounting

The storage operation starts `BEGIN IMMEDIATE`, then rechecks ownership, session
expiry and the snapshot while holding the write lock. New evidence arriving since
the preview causes 409 before deletion. Exact evidence retries do not change the
snapshot. Concurrent append/delete operations either preserve the newer evidence
and reject deletion, or delete the case and reject the append.

Within the same transaction it removes the case's history, ownership and case row.
Existing quota triggers subtract case count and UTF-8 payload bytes. A failure at
any point rolls back these changes, including accounting. Other cases and their
evidence remain intact.

If that browser session has no remaining cases, its session row is removed too,
freeing a session slot. Other cases in the session keep their access. The DELETE
response intentionally does not clear the browser cookie: a new case in another
tab may just have issued a replacement cookie, which a delayed delete response
must not erase. An obsolete cookie has no authority after its session row is gone;
creating the next case issues a new session as needed.

Repeating a completed deletion returns 404, without changing counters again.
No tombstone is stored. The API does not recreate deleted cases. Network request
budgets are not reset by deletion. There is no bulk-delete or trusted local CLI
delete command in this step; unowned legacy CLI cases cannot be claimed or deleted
through the browser.

## Meaning of deletion

Deletion removes records from the active application database. It is not a promise
of forensic erasure from SQLite free pages, journals, backups, logs, browser memory
or previously returned responses. In-flight reads can already have read a record.
The UI states that existing backup copies are not removed and that the action has
no in-app undo. Payload capacity is released immediately on commit; the physical
SQLite file need not shrink.

No automatic deletion, backup purging, session expiry changes or retention schedule
is added. Before public deployment, choose the retention period and backup policy.
Restoring a pre-deletion backup can restore removed records; a production restore
procedure must account for deletions made after the backup.

## Verification

`tests/test_case_deletion.py` adds 23 tests covering ownership, expiry, stale
snapshots, confirmation, rollback, quota release, concurrent writes/deletes,
session reuse and deletion during model generation. They use temporary databases.
An explanation deleted during generation is discarded when the existing route
rechecks the case; its generation lock is released.

`tests/test_manage_page.cjs` adds 8 JavaScript tests using Node's built-in test
runner and a simulated DOM/fetch layer, with no npm dependencies. They cover
confirmation, cancellation, stale lookup responses, double submission, error
handling and text-only insertion. Optional command:

```powershell
node --test tests/test_manage_page.cjs
```

The isolated harness passed 100 Python tests (23 new plus 77 earlier tests) and
8 JavaScript tests. Its policy/knowledge/model modules are substitutes, not the
complete project. Run the full Windows suite. Browser rendering was not verified
in the remote environment; the local smoke check in the README covers the page.

References:

- https://www.sqlite.org/lang_transaction.html — write transactions and rollback.
- https://www.sqlite.org/pragma.html#pragma_secure_delete — logical deletion and
  the limits of removing traces from database storage.
