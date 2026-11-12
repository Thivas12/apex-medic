# Contributing

Fork the repository if you do not have write access. Follow the
[local setup](README.md#run-locally) using your fork's clone URL, then create a
branch from `main`. Open your pull request against `Thivas12/apex-medic:main`.
Use synthetic cases during development. SQLite needs no external services;
Node 24 runs the JavaScript tests without npm dependencies.

Run [the test commands](README.md#tests) and `git diff --check` before opening a
pull request. Describe the behavior change, how it was tested and any migration
or configuration steps. Use UTF-8 and LF line endings; `.editorconfig` and
`.gitattributes` contain the shared settings.

## Changes to policy or guidance

Add scenarios for new policy branches. Check the selected action, conditions
and recording instructions, including missing or contradictory evidence. A
policy call must not mutate the case.

Guidance cards need the correct action mapping and source attribution. Project
policy must stay separate from externally sourced advice. If the model adapter
changes, test rejected responses as well as valid selections. See
[evaluation](evaluation/README.md) for live checks and content review.

## Storage and API changes

Check exact retries, conflicting event IDs, concurrent writes and rollback on
both storage engines. API changes also need ownership and invalid-input checks.
Frontend changes need a browser walkthrough as well as the JavaScript tests.

PostgreSQL tests drop the schema in a disposable local `apexmedic_test_*`
database. Never use production credentials for them. Applied migrations are
checksum-checked; add a migration rather than editing one already in use.
See [database setup](docs/postgres_setup.md).

Keep credentials, session cookies and user case data out of commits and test
fixtures. CI mocks providers and uses disposable storage. Live evaluation uses
provider quota and runs separately. Deployment is a manual step after checks
pass; see [the release procedure](docs/render_deployment.md).

For bugs, include a minimal synthetic example, expected and actual behavior,
and the commit you tested. Keep production logs and credentials out of issues.
For suspected vulnerabilities, follow [SECURITY.md](SECURITY.md).
