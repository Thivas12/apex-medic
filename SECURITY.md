# Security

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting form](https://github.com/Thivas12/apex-medic/security/advisories/new)
when it is enabled. If the form is unavailable, open an issue titled
"Security contact request" with no vulnerability details. The maintainer can
then arrange a private reporting channel.

Include the affected commit, the behavior you expected, what happened instead,
and a minimal reproduction using synthetic data on a local instance. Remove
API keys, database URLs, browser cookies and real case data from all attachments.
Do not put exploit details or credentials in a public issue or pull request.

## Scope

Security fixes target the current `main` branch. Older snapshots and experimental
branches do not have a separate maintenance policy. This is a personal project;
there is no guaranteed response time or bug bounty.

Use a local instance and disposable data for testing. Do not probe other users'
cases or run load tests against the hosted demo.

The [architecture](docs/architecture.md) and [case access](docs/case_access.md)
documents describe the current controls and their limits. Anonymous sessions
expire after 30 days; clearing a cookie loses access, and expiry does not delete
stored cases. The management page provides explicit case deletion.
